"""Manage live conversations and persist transcript and memory changes as local JSON."""

import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import UUID

from models.conversation import Conversation
from models.message import Message
from models.workspace import WorkspaceBinding
from services.attachment_errors import AttachmentReferencesUnavailableError
from services.work_run_store import WorkRunStore

logger = logging.getLogger(__name__)


class ConversationUnavailableError(ValueError):
    """A pending operation holds a deleted or unregistered conversation."""


class ConversationManager:
    """Mutations run synchronously on one event loop in a single server process."""

    def __init__(self, storage_root: Path) -> None:
        """Create the storage directory and load existing conversations into memory."""
        self._storage_root = storage_root
        self._storage_root.mkdir(parents=True, exist_ok=True)
        self._conversations: dict[str, Conversation] = {}
        self._reference_lock = RLock()
        self._attachment_references: dict[str, set[str]] = {}
        self._attachment_counts: Counter[str] = Counter()
        self._references_available = True
        self.on_attachments_released: Callable[[set[str]], None] | None = None
        self._load()

    def _path_for(self, conversation_id: str) -> Path:
        """Map only UUID conversation identifiers to a file inside storage_root."""
        try:
            safe_id = str(UUID(conversation_id))
        except ValueError as exc:
            raise ValueError("Invalid conversation identifier.") from exc
        return self._storage_root / f"{safe_id}.json"

    def _load(self) -> None:
        """Load valid conversation files while logging and skipping invalid records."""
        try:
            paths = list(self._storage_root.iterdir())
        except OSError:
            self._references_available = False
            logger.exception("Could not read the conversation directory.")
            return
        for path in paths:
            if path.suffix != ".json":
                continue
            try:
                conversation = Conversation.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
                if self._path_for(conversation.id) != path:
                    raise ValueError(
                        "Conversation file name does not match its id."
                    )
                self._conversations[conversation.id] = conversation
                self._index_attachments(conversation.id, self._message_attachment_ids(conversation))
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                json.JSONDecodeError,
            ):
                self._references_available = False
                logger.exception(
                    "Could not load conversation file: %s", path.name
                )

    def is_active(self, conversation: Conversation) -> bool:
        """Return whether this exact conversation object remains registered and live."""
        return (
            not conversation.is_deleted
            and self._conversations.get(conversation.id) is conversation
        )

    def _require_active(self, conversation: Conversation) -> None:
        """Reject operations on deleted or superseded conversation objects."""
        if not self.is_active(conversation):
            raise ConversationUnavailableError(
                "Conversation is no longer available."
            )

    def _save(self, conversation: Conversation) -> None:
        """Persist a live conversation using a temporary file and atomic replacement."""
        self._require_active(conversation)
        target = self._path_for(conversation.id)
        temporary = target.with_suffix(".tmp")
        identities = self._message_attachment_ids(conversation)
        try:
            temporary.write_text(
                json.dumps(
                    conversation.to_dict(), ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
            # Request leases protect new copies until this committed index is published.
            with self._reference_lock:
                temporary.replace(target)
                released = self._index_attachments(conversation.id, identities)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "Could not remove temporary conversation file: %s",
                    temporary.name,
                    exc_info=True,
                )
            raise
        self._notify_attachments(released)

    def _commit_changes(
        self, conversation: Conversation, **changes: object
    ) -> None:
        """Publish one version, restoring the live object if atomic file replacement fails.

        Callers supply new lists/messages rather than mutating shared values. There
        must be no await between validation, mutation, saving, and rollback.
        """
        self._require_active(conversation)
        changes.update(
            version=conversation.version + 1,
            updated_at=datetime.now(timezone.utc),
        )
        previous = {name: getattr(conversation, name) for name in changes}
        for name, value in changes.items():
            setattr(conversation, name, value)
        try:
            self._save(conversation)
        except Exception:
            for name, value in previous.items():
                setattr(conversation, name, value)
            raise

    def create(self, workspace: WorkspaceBinding | None = None) -> Conversation:
        """Register and persist an empty conversation, rolling back on save failure."""
        conversation = Conversation(workspace=workspace or WorkspaceBinding())
        self._conversations[conversation.id] = conversation
        try:
            self._save(conversation)
        except Exception:
            self._conversations.pop(conversation.id)
            conversation.is_deleted = True
            raise
        return conversation

    def set_workspace(
        self, conversation: Conversation, workspace: WorkspaceBinding,
    ) -> None:
        """Persist a changed folder atomically with the conversation's next version."""
        if conversation.workspace != workspace:
            self._commit_changes(conversation, workspace=workspace)

    def get(self, conversation_id: str) -> Conversation | None:
        """Return a registered conversation by identifier, or None if absent."""
        return self._conversations.get(conversation_id)

    def get_or_create(self, conversation_id: str | None) -> Conversation:
        """Return the requested conversation when found, otherwise create one."""
        return (
            self.get(conversation_id)
            if conversation_id and self.get(conversation_id)
            else self.create()
        )

    def rename(self, conversation_id: str, title: str) -> Conversation | None:
        """Save a nonempty title capped at 100 characters, or return None if missing."""
        conversation = self.get(conversation_id)
        if not conversation:
            return None
        cleaned_title = title.strip()
        if not cleaned_title:
            raise ValueError("Conversation title cannot be empty.")
        self._commit_changes(conversation, title=cleaned_title[:100])
        return conversation

    def delete(self, conversation_id: str) -> bool:
        """Delete a conversation and its file, restoring registration if deletion fails."""
        conversation = self._conversations.pop(conversation_id, None)
        if not conversation:
            return False
        previous_version = conversation.version
        conversation.is_deleted = True
        conversation.version += 1
        try:
            with self._reference_lock:
                self._path_for(conversation_id).unlink(missing_ok=True)
                released = self._index_attachments(conversation_id, set())
                self._attachment_references.pop(conversation_id, None)
        except OSError:
            # Keep the conversation in memory if the disk deletion did not succeed.
            conversation.is_deleted = False
            conversation.version = previous_version
            self._conversations[conversation_id] = conversation
            raise
        self._notify_attachments(released)
        return True

    def edit_user_message(
        self, conversation_id: str, message_index: int, content: str
    ) -> Conversation | None:
        """Replace user content, discard later messages, and invalidate overlapping memory."""
        conversation = self.get(conversation_id)
        if not conversation:
            return None
        if message_index < 0 or message_index >= len(conversation.messages):
            raise ValueError("Message not found.")
        message = conversation.messages[message_index]
        if message.role != "user":
            raise ValueError("Only user messages can be edited.")
        cleaned_content = content.strip()
        if not cleaned_content:
            raise ValueError("Message cannot be empty.")
        # An edit invalidates all later replies and any memory that may include them.
        messages = conversation.messages[:message_index] + [
            replace(message, content=cleaned_content)
        ]
        changes = {"messages": messages}
        if conversation.summarized_message_count > message_index:
            changes.update(memory_summary="", summarized_message_count=0)
        self._commit_changes(conversation, **changes)
        return conversation

    def update_memory_if_version_matches(
        self,
        conversation: Conversation,
        summary: str,
        summarized_message_count: int,
        *,
        expected_version: int,
    ) -> bool:
        """Save advancing summary coverage only while the conversation version still matches."""
        if (
            not self.is_active(conversation)
            or conversation.version != expected_version
        ):
            return False
        if type(
            summarized_message_count
        ) is not int or not conversation.summarized_message_count < summarized_message_count <= len(
            conversation.messages
        ):
            raise ValueError("Invalid summarized message count.")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("Conversation memory cannot be empty.")
        self._commit_changes(
            conversation,
            memory_summary=summary.strip(),
            summarized_message_count=summarized_message_count,
        )
        return True

    def list(self) -> list[dict]:
        """Return metadata-only conversation summaries in most-recent-update order."""
        ordered = sorted(
            self._conversations.values(),
            key=lambda item: item.updated_at,
            reverse=True,
        )
        return [item.to_dict(include_messages=False) for item in ordered]

    def attachment_ids(self) -> set[str]:
        """Return committed reference identities without rereading transcript files."""
        with self._reference_lock:
            self._require_attachment_index()
            return set(self._attachment_counts)

    def attachment_is_referenced(self, identity: str) -> bool:
        """Check one copy against the committed index and fail closed after an invalid startup."""
        with self._reference_lock:
            self._require_attachment_index()
            return identity in self._attachment_counts

    def _require_attachment_index(self) -> None:
        """Preserve copies if startup skipped a transcript or its directory is unavailable."""
        if not self._references_available:
            raise AttachmentReferencesUnavailableError(
                "Saved conversation references could not be verified at startup."
            )
        self._storage_root.stat()

    @staticmethod
    def _message_attachment_ids(conversation: Conversation) -> set[str]:
        """Extract unique upload references from one committed transcript."""
        return {
            source["attachment_id"] for message in conversation.messages
            for source in message.attachment_sources if source["source"] == "upload"
        }

    def _index_attachments(self, conversation_id: str, identities: set[str]) -> set[str]:
        """Publish reference changes only after the matching disk mutation succeeds."""
        previous = self._attachment_references.get(conversation_id, set())
        for identity in previous - identities:
            self._attachment_counts[identity] -= 1
            if not self._attachment_counts[identity]:
                del self._attachment_counts[identity]
        self._attachment_counts.update(identities - previous)
        self._attachment_references[conversation_id] = identities
        return previous - identities

    def _notify_attachments(self, identities: set[str]) -> None:
        """Notify cleanup after commitment without rolling back a successfully saved transcript."""
        if identities and self.on_attachments_released is not None:
            try:
                self.on_attachments_released(identities)
            except Exception:
                logger.exception("Could not schedule released attachment copies for cleanup.")

    def replace_reply(
        self,
        conversation: Conversation,
        user_index: int,
        content: str,
        *,
        expected_messages: "list[Message]",
        user_attachment_sources: "list[dict] | None" = None,
        **metadata: object,
    ) -> Message:
        """Replace a branch only after a successful response against the same transcript."""
        self._require_active(conversation)
        if conversation.messages is not expected_messages:
            raise ValueError(
                "Conversation changed during regeneration. Please retry."
            )
        if (
            not 0 <= user_index < len(expected_messages)
            or expected_messages[user_index].role != "user"
        ):
            raise ValueError("Regeneration requires a user message.")
        message = Message(role="assistant", content=content, **metadata)
        changes = {"messages": [*expected_messages[: user_index + 1], message]}
        if user_attachment_sources is not None:
            changes["messages"][user_index] = replace(
                expected_messages[user_index], attachment_sources=user_attachment_sources,
            )
        # Summaries may have advanced in the background while the reply streamed.
        if conversation.summarized_message_count > user_index:
            changes.update(memory_summary="", summarized_message_count=0)
        self._commit_changes(conversation, **changes)
        return message

    def add_message(
        self,
        conversation: Conversation,
        role: str,
        content: str,
        profile_id: str | None = None,
        profile_name: str | None = None,
        deployment: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        response_time_ms: int | None = None,
        estimated_cost: float | None = None,
        is_long_context: bool = False,
        uncached_input_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        usage_status: str | None = None,
        cost_status: str | None = None,
        provider_usage: dict | None = None,
        usage_call_id: str | None = None,
        run_id: str | None = None,
        attachment_sources: "list[dict] | None" = None,
        reasoning_summary: "list[dict] | None" = None,
    ) -> Message:
        """Persist a new message and derive the initial title from the first user content."""
        message = Message(
            role=role,  # type: ignore[arg-type]
            content=content,
            profile_id=profile_id,
            profile_name=profile_name,
            deployment=deployment,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            response_time_ms=response_time_ms,
            estimated_cost=estimated_cost,
            is_long_context=is_long_context,
            uncached_input_tokens=uncached_input_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
            usage_status=usage_status,
            cost_status=cost_status,
            provider_usage=provider_usage,
            usage_call_id=usage_call_id,
            run_id=run_id,
            attachment_sources=attachment_sources or [],
            reasoning_summary=reasoning_summary or [],
        )
        title = conversation.title
        if role == "user" and conversation.title == "New conversation":
            title = content.strip().replace("\n", " ")[:60] or title
        self._commit_changes(
            conversation,
            messages=[*conversation.messages, message],
            title=title,
        )
        return message

    def adopt_run_reply(
        self, conversation: Conversation, run_id: str, run_store: WorkRunStore, *,
        expected_messages: "list[Message]",
        user_attachment_sources: "list[dict] | None" = None,
    ) -> Message:
        """Adopt a final run answer only against its original conversation version."""
        self._require_active(conversation)
        run = run_store.get(run_id, conversation.id)
        if (conversation.version != run["base_version"]
                or conversation.messages is not expected_messages
                or run["status"] != "ready"):
            raise ValueError("Conversation or work run changed before adoption.")
        answer = run_store.answer(run_id)
        summary = run_store.reasoning_summary(run_id)
        # A completed orphan is retained if the later JSON write fails or is interrupted.
        run_store.finish(run_id, "completed")
        if run["kind"] == "regeneration":
            return self.replace_reply(
                conversation, run["user_index"], answer,
                expected_messages=expected_messages, run_id=run_id,
                user_attachment_sources=user_attachment_sources,
                reasoning_summary=summary,
            )
        return self.add_message(conversation, "assistant", answer, run_id=run_id,
                                reasoning_summary=summary)
