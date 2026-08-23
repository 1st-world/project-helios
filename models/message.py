from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


@dataclass
class Message:
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    profile_id: str | None = None
    profile_name: str | None = None
    deployment: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    response_time_ms: int | None = None
    estimated_cost: float | None = None
    is_long_context: bool = False

    def to_dict(self) -> dict:
        data: dict[str, object] = {
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
        }
        if self.profile_id is not None:
            data["profile_id"] = self.profile_id
        if self.profile_name is not None:
            data["profile_name"] = self.profile_name
        if self.deployment is not None:
            data["deployment"] = self.deployment
        if self.input_tokens is not None:
            data["input_tokens"] = self.input_tokens
        if self.output_tokens is not None:
            data["output_tokens"] = self.output_tokens
        if self.total_tokens is not None:
            data["total_tokens"] = self.total_tokens
        if self.response_time_ms is not None:
            data["response_time_ms"] = self.response_time_ms
        if self.estimated_cost is not None:
            data["estimated_cost"] = self.estimated_cost
        if self.is_long_context:
            data["is_long_context"] = self.is_long_context
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        role = data.get("role")
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Invalid message role.")
        content = data.get("content")
        if not isinstance(content, str):
            raise ValueError("Invalid message content.")
        created_at = datetime.fromisoformat(data["created_at"])
        cost = data.get("estimated_cost")
        return cls(
            role=role,
            content=content,
            created_at=created_at,
            profile_id=data.get("profile_id"),
            profile_name=data.get("profile_name"),
            deployment=data.get("deployment"),
            input_tokens=data.get("input_tokens"),
            output_tokens=data.get("output_tokens"),
            total_tokens=data.get("total_tokens"),
            response_time_ms=data.get("response_time_ms"),
            estimated_cost=float(cost) if cost is not None else None,
            is_long_context=bool(data.get("is_long_context", False)),
        )
