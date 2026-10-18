"""Keep short-lived failed-request receipts and serialize explicitly authorized recovery work."""

from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4


class RecoveryError(RuntimeError):
    """Report a safe failure to claim or validate a one-use recovery receipt."""

    def __init__(
        self, message: str, *, status: int = 409,
        code: str = "context_recovery_unavailable",
    ) -> None:
        """Store an HTTP status without exposing the failed request's private inputs."""
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class RecoveryTicket:
    """Retain one exact failed request privately until expiry or a single attempt."""

    id: str
    expires: float
    data: dict = field(repr=False)
    weight: int = 0
    used: bool = False
    operation_id: str | None = None


@dataclass
class ChatOperation:
    """Release a conversation's request slot even after cancellation or disconnect."""

    registry: "ContextRecoveryRegistry" = field(repr=False)
    conversation_id: str
    ticket: RecoveryTicket | None = field(default=None, repr=False)
    closed: bool = False
    release_workspace: Callable[[], None] | None = field(default=None, repr=False)
    release_attachments: Callable[[], None] | None = field(default=None, repr=False)

    def close(self) -> None:
        """Release ownership once and discard private inputs after a recovery attempt."""
        if self.closed:
            return
        self.closed = True
        self.registry._busy.discard(self.conversation_id)
        if self.release_workspace is not None:
            self.release_workspace()
            self.release_workspace = None
        if self.ticket is not None:
            self.ticket.data = {}
            self.ticket.weight = 0
        if self.release_attachments is not None:
            self.release_attachments()
            self.release_attachments = None


class ContextRecoveryRegistry:
    """Bound transient snapshots and prevent duplicate paid work in one server process."""

    def __init__(
        self, *, ttl: float = 300, max_tickets: int = 32,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        """Set receipt lifetime and memory bounds independently of model capacity."""
        self.ttl, self.max_tickets, self.max_bytes = ttl, max_tickets, max_bytes
        self._tickets: dict[str, RecoveryTicket] = {}
        self._busy: set[str] = set()

    def _prune(self) -> None:
        """Drop expired snapshots while active operations retain their own references."""
        now = monotonic()
        self._tickets = {
            key: ticket for key, ticket in self._tickets.items()
            if ticket.expires > now
        }

    @staticmethod
    def _weight(value) -> int:
        """Bound retained text conservatively without serializing binary attachment strings."""
        if isinstance(value, str):
            return len(value) * 4
        if isinstance(value, dict):
            return sum(ContextRecoveryRegistry._weight(item) for item in value.values())
        if isinstance(value, list):
            return sum(ContextRecoveryRegistry._weight(item) for item in value)
        return 0

    def issue(self, data: dict) -> str | None:
        """Retain a bounded request snapshot without writing credentials or attachments to disk."""
        self._prune()
        weight = self._weight(data["workspace_context"]) + self._weight(
            data["attachments"].content,
        )
        weight += self._weight(data.get("prompt", "")) + sum(
            len(message.content) * 4 for message in data.get("messages", ())
        )
        if weight > self.max_bytes:
            return None
        while self._tickets and (
            len(self._tickets) >= self.max_tickets
            or sum(ticket.weight for ticket in self._tickets.values()) + weight
            > self.max_bytes
        ):
            del self._tickets[next(iter(self._tickets))]
        ticket_id = str(uuid4())
        self._tickets[ticket_id] = RecoveryTicket(
            ticket_id, monotonic() + self.ttl, data, weight,
        )
        return ticket_id

    def get(self, ticket_id: str) -> RecoveryTicket:
        """Require an unexpired receipt before accessing its private failed-request snapshot."""
        self._prune()
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise RecoveryError("This recovery request expired or is no longer available. Send a new request after reviewing the input.", status=410)
        if ticket.used:
            raise RecoveryError("This recovery request was already attempted. No additional call was started.")
        return ticket

    def invalidate_attachment(self, attachment_id: str) -> None:
        """Discard unused prepared payloads when their retained original is deleted."""
        self._tickets = {
            key: ticket for key, ticket in self._tickets.items()
            if attachment_id not in {
                source.get("attachment_id")
                for source in ticket.data.get("attachment_sources") or []
            }
        }

    def begin(
        self, conversation_id: str, *, ticket: RecoveryTicket | None = None,
        operation_id: str | None = None,
    ) -> ChatOperation:
        """Claim one conversation synchronously before any attachment or provider await."""
        if conversation_id in self._busy:
            raise RecoveryError("Another request is running for this conversation. Wait for it to finish before trying again.", code="conversation_request_in_progress")
        if ticket is not None:
            if ticket.used:
                raise RecoveryError("This recovery request was already attempted. No additional call was started.")
            ticket.used, ticket.operation_id = True, operation_id
        self._busy.add(conversation_id)
        return ChatOperation(self, conversation_id, ticket)
