"""Keep short-lived recovery receipts and coordinate chat request ownership and cleanup."""

from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4

import anyio


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
    finished: anyio.Event = field(default_factory=anyio.Event, repr=False)

    def close(self) -> None:
        """Release ownership once and discard private inputs after a recovery attempt."""
        if self.closed:
            return
        self.closed = True
        if self.release_workspace is not None:
            self.release_workspace()
            self.release_workspace = None
        if self.ticket is not None:
            self.registry.release_ticket(self.ticket)
        if self.release_attachments is not None:
            self.release_attachments()
            self.release_attachments = None
        self.registry._busy.discard(self.conversation_id)
        self.registry._operations.pop(self.conversation_id, None)
        self.finished.set()


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
        self._operations: dict[str, ChatOperation] = {}
        self.on_attachments_changed: Callable[[set[str]], None] | None = None

    def _prune(self, *, notify: bool = True) -> set[str]:
        """Drop expired snapshots while active operations retain their own references."""
        now = monotonic()
        return self._discard(
            {key for key, ticket in self._tickets.items() if ticket.expires <= now}, notify=notify,
        )

    @staticmethod
    def _ticket_attachment_ids(ticket: RecoveryTicket) -> set[str]:
        """Extract upload identities without retaining or copying prepared contents."""
        return {
            source["attachment_id"] for source in ticket.data.get("attachment_sources") or []
            if source.get("source") == "upload"
        }

    def _discard(self, ticket_ids: set[str], *, notify: bool = True) -> set[str]:
        """Release copies when receipts expire, are evicted, or lose their conversation."""
        identities = set()
        for key in ticket_ids:
            identities.update(self._ticket_attachment_ids(self._tickets.pop(key)))
        if notify:
            self._notify_attachments(identities)
        return identities

    def _notify_attachments(self, identities: set[str]) -> None:
        """Wake targeted cleanup when receipt ownership changes."""
        if identities and self.on_attachments_changed is not None:
            self.on_attachments_changed(identities)

    def release_ticket(self, ticket: RecoveryTicket) -> None:
        """Discard used payloads and notify cleanup after their operation ends."""
        identities = self._ticket_attachment_ids(ticket)
        ticket.data = {}
        ticket.weight = 0
        self._notify_attachments(identities)

    def next_attachment_expiry(self) -> float | None:
        """Return the earliest receipt deadline that still protects attachment copies."""
        return min(
            (ticket.expires for ticket in self._tickets.values() if self._ticket_attachment_ids(ticket)),
            default=None,
        )

    def prune(self) -> set[str]:
        """Return scheduled expirations directly without notifying the consuming scheduler."""
        return self._prune(notify=False)

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
            self._discard({next(iter(self._tickets))})
        ticket_id = str(uuid4())
        self._tickets[ticket_id] = RecoveryTicket(
            ticket_id, monotonic() + self.ttl, data, weight,
        )
        self._notify_attachments(self._ticket_attachment_ids(self._tickets[ticket_id]))
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
        self._discard({
            key for key, ticket in self._tickets.items()
            if attachment_id in {
                source.get("attachment_id")
                for source in ticket.data.get("attachment_sources") or []
            }
        })

    def attachment_ids(self) -> set[str]:
        """Keep originals needed by unexpired recovery receipts until their payload is released."""
        return {
            source["attachment_id"] for ticket in self._tickets.copy().values()
            if ticket.expires > monotonic()
            for source in ticket.data.get("attachment_sources") or []
            if source.get("source") == "upload"
        }

    def attachment_is_referenced(self, identity: str) -> bool:
        """Check one copy against the bounded set of unexpired recovery receipts."""
        return any(
            ticket.expires > monotonic() and identity in self._ticket_attachment_ids(ticket)
            for ticket in self._tickets.copy().values()
        )

    def forget_conversation(self, conversation_id: str) -> None:
        """Drop retry receipts after their conversation has been deleted."""
        self._discard({
            key for key, ticket in self._tickets.items()
            if getattr(ticket.data.get("conversation"), "id", None) == conversation_id
        })

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
        operation = ChatOperation(self, conversation_id, ticket)
        self._operations[conversation_id] = operation
        return operation

    def is_busy(self, conversation_id: str) -> bool:
        """Report whether a conversation still owns a preparation or streaming request."""
        return conversation_id in self._busy

    async def wait_for_idle(self, conversation_id: str, *, timeout: float = 10) -> None:
        """Wait for the current owner to release resources without starting any paid work."""
        operation = self._operations.get(conversation_id)
        if operation is not None:
            with anyio.move_on_after(timeout):
                await operation.finished.wait()
