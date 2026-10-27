"""Represent persisted conversations, transcript memory, and independent model selections."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from models.message import Message
from models.workspace import WorkspaceBinding


@dataclass
class Conversation:
    """Hold transcript authority and an independent revision for the next response's model."""

    id: str = field(default_factory=lambda: str(uuid4()))
    title: str = "New conversation"
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    messages: list[Message] = field(default_factory=list)
    memory_summary: str = ""
    summarized_message_count: int = 0
    version: int = 0
    profile_id: str | None = None
    profile_revision: int = 0
    profile_initialized: bool = field(default=True, repr=False, compare=False)
    workspace: WorkspaceBinding = field(default_factory=WorkspaceBinding)
    is_deleted: bool = field(default=False, repr=False, compare=False)

    def to_dict(self, include_messages: bool = True) -> dict:
        """Serialize conversation metadata and optionally include the message transcript."""
        data = {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "memory_summary": self.memory_summary,
            "summarized_message_count": self.summarized_message_count,
            "version": self.version,
            "workspace": self.workspace.model_dump(),
        }
        # Preserve absent legacy fields until the selection is initialized durably.
        if self.profile_initialized:
            data.update(
                profile_id=self.profile_id, profile_revision=self.profile_revision,
            )
        if include_messages:
            data["messages"] = [message.to_dict() for message in self.messages]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Conversation":
        """Restore persisted state, resetting inconsistent summary coverage."""
        conversation_id = data.get("id")
        title = data.get("title")
        if not isinstance(conversation_id, str) or not isinstance(title, str):
            raise ValueError("Invalid conversation metadata.")
        messages_data = data.get("messages", [])
        if not isinstance(messages_data, list):
            raise ValueError("Invalid conversation messages.")
        memory_summary = data.get("memory_summary", "")
        summarized_message_count = data.get("summarized_message_count", 0)
        if (
            not isinstance(memory_summary, str)
            or type(summarized_message_count) is not int
        ):
            raise ValueError("Invalid conversation memory.")
        version = data.get("version", 0)
        if type(version) is not int or version < 0:
            raise ValueError("Invalid conversation version.")
        profile_id = data.get("profile_id")
        profile_revision = data.get("profile_revision", 0)
        if (
            profile_id is not None
            and (not isinstance(profile_id, str) or not profile_id.strip())
        ):
            raise ValueError("Invalid conversation profile.")
        if type(profile_revision) is not int or profile_revision < 0:
            raise ValueError("Invalid conversation profile revision.")
        if "profile_revision" in data and "profile_id" not in data:
            raise ValueError("Conversation profile selection is incomplete.")
        messages = [Message.from_dict(message) for message in messages_data]
        if (
            summarized_message_count < 0
            or summarized_message_count > len(messages)
            or bool(memory_summary.strip()) != (summarized_message_count > 0)
        ):
            # Reset inconsistent memory so it cannot hide edited or deleted transcript content.
            logging.getLogger(__name__).warning(
                "Reset inconsistent conversation memory: %s", conversation_id
            )
            memory_summary = ""
            summarized_message_count = 0
        return cls(
            id=conversation_id,
            title=title,
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            messages=messages,
            memory_summary=memory_summary,
            summarized_message_count=summarized_message_count,
            version=version,
            profile_id=profile_id, profile_revision=profile_revision,
            profile_initialized="profile_id" in data,
            workspace=WorkspaceBinding.model_validate(data.get("workspace", {})),
        )
