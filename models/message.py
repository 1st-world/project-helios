from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


@dataclass
class Message:
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content, "created_at": self.created_at.isoformat()}

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        role = data.get("role")
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Invalid message role.")
        content = data.get("content")
        if not isinstance(content, str):
            raise ValueError("Invalid message content.")
        created_at = datetime.fromisoformat(data["created_at"])
        return cls(role=role, content=content, created_at=created_at)
