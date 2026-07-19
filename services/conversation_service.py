"""Conversation management with local JSON persistence."""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from uuid import UUID

from models.conversation import Conversation
from models.message import Message


class ConversationManager:
    def __init__(self, storage_root: Path) -> None:
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
        for path in self._storage_root.glob("*.json"):
            try:
                conversation = Conversation.from_dict(json.loads(path.read_text(encoding="utf-8")))
                # Ensure the document id matches its safe storage path.
                if self._path_for(conversation.id) != path:
                    raise ValueError("Conversation file name does not match its id.")
                self._conversations[conversation.id] = conversation
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                logging.getLogger(__name__).exception("Could not load conversation file: %s", path.name)

    def _save(self, conversation: Conversation) -> None:
        target = self._path_for(conversation.id)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(conversation.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def create(self) -> Conversation:
        conversation = Conversation()
        self._conversations[conversation.id] = conversation
        self._save(conversation)
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)

    def get_or_create(self, conversation_id: str | None) -> Conversation:
        return self.get(conversation_id) if conversation_id and self.get(conversation_id) else self.create()

    def rename(self, conversation_id: str, title: str) -> Conversation | None:
        conversation = self.get(conversation_id)
        if not conversation:
            return None
        cleaned_title = title.strip()
        if not cleaned_title:
            raise ValueError("Conversation title cannot be empty.")
        conversation.title = cleaned_title[:100]
        conversation.updated_at = datetime.now(timezone.utc)
        self._save(conversation)
        return conversation

    def delete(self, conversation_id: str) -> bool:
        conversation = self._conversations.pop(conversation_id, None)
        if not conversation:
            return False
        try:
            self._path_for(conversation_id).unlink(missing_ok=True)
        except OSError:
            # Keep the conversation in memory if the disk deletion did not succeed.
            self._conversations[conversation_id] = conversation
            raise
        return True

    def edit_user_message(self, conversation_id: str, message_index: int, content: str) -> Conversation | None:
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
        message.content = cleaned_content
        conversation.messages = conversation.messages[:message_index + 1]
        # An edit invalidates all later replies and any memory that may include them.
        conversation.memory_summary = ""
        conversation.summarized_message_count = 0
        conversation.updated_at = datetime.now(timezone.utc)
        self._save(conversation)
        return conversation

    def update_memory(self, conversation: Conversation, summary: str, summarized_message_count: int) -> None:
        conversation.memory_summary = summary
        conversation.summarized_message_count = summarized_message_count
        conversation.updated_at = datetime.now(timezone.utc)
        self._save(conversation)

    def list(self) -> list[dict]:
        ordered = sorted(self._conversations.values(), key=lambda item: item.updated_at, reverse=True)
        return [item.to_dict(include_messages=False) for item in ordered]

    def add_message(self, conversation: Conversation, role: str, content: str) -> Message:
        message = Message(role=role, content=content)  # type: ignore[arg-type]
        conversation.messages.append(message)
        conversation.updated_at = datetime.now(timezone.utc)
        if role == "user" and conversation.title == "New conversation":
            conversation.title = content.strip().replace("\n", " ")[:60] or conversation.title
        self._save(conversation)
        return message
