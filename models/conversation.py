from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from models.message import Message


@dataclass
class Conversation:
    id: str = field(default_factory=lambda: str(uuid4()))
    title: str = "New conversation"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    messages: list[Message] = field(default_factory=list)
    memory_summary: str = ""
    summarized_message_count: int = 0

    def to_dict(self, include_messages: bool = True) -> dict:
        data = {"id": self.id, "title": self.title, "created_at": self.created_at.isoformat(), "updated_at": self.updated_at.isoformat(),
                "memory_summary": self.memory_summary, "summarized_message_count": self.summarized_message_count}
        if include_messages:
            data["messages"] = [message.to_dict() for message in self.messages]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Conversation":
        conversation_id = data.get("id")
        title = data.get("title")
        if not isinstance(conversation_id, str) or not isinstance(title, str):
            raise ValueError("Invalid conversation metadata.")
        messages_data = data.get("messages", [])
        if not isinstance(messages_data, list):
            raise ValueError("Invalid conversation messages.")
        memory_summary = data.get("memory_summary", "")
        summarized_message_count = data.get("summarized_message_count", 0)
        if not isinstance(memory_summary, str) or not isinstance(summarized_message_count, int):
            raise ValueError("Invalid conversation memory.")
        messages = [Message.from_dict(message) for message in messages_data]
        if summarized_message_count < 0 or summarized_message_count > len(messages):
            raise ValueError("Invalid summarized message count.")
        return cls(
            id=conversation_id,
            title=title,
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            messages=messages,
            memory_summary=memory_summary,
            summarized_message_count=summarized_message_count,
        )
