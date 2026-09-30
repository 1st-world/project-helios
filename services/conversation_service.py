"""Manage live conversations and persist transcript and memory changes as local JSON."""

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from models.conversation import Conversation
from models.message import Message

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
        for path in self._storage_root.glob("*.json"):
            try:
                conversation = Conversation.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
                if self._path_for(conversation.id) != path:
                    raise ValueError(
                        "Conversation file name does not match its id."
                    )
                self._conversations[conversation.id] = conversation
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                json.JSONDecodeError,
            ):
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
        try:
            temporary.write_text(
                json.dumps(
                    conversation.to_dict(), ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
            temporary.replace(target)
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

    def create(self) -> Conversation:
        """Register and persist an empty conversation, rolling back on save failure."""
        conversation = Conversation()
        self._conversations[conversation.id] = conversation
        try:
            self._save(conversation)
        except Exception:
            self._conversations.pop(conversation.id)
            conversation.is_deleted = True
            raise
        return conversation

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
            self._path_for(conversation_id).unlink(missing_ok=True)
        except OSError:
            # Keep the conversation in memory if the disk deletion did not succeed.
            conversation.is_deleted = False
            conversation.version = previous_version
            self._conversations[conversation_id] = conversation
            raise
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

    def replace_reply(
        self,
        conversation: Conversation,
        user_index: int,
        content: str,
        *,
        expected_messages: "list[Message]",
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
