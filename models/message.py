"""Represent chat messages and serialize their timestamps, provider metadata, and usage."""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from models.attachment import ATTACHMENT_REFERENCE_VERSION, ATTACHMENT_SOURCES

RUN_REFERENCE_SCHEMA_VERSION = "0.1"


@dataclass
class Message:
    """Hold message content, creation time, and optional provider and usage metadata."""

    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    profile_id: str | None = None
    profile_name: str | None = None
    deployment: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    response_time_ms: int | None = None
    estimated_cost: float | None = None
    is_long_context: bool = False
    uncached_input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    usage_status: str | None = None
    cost_status: str | None = None
    provider_usage: dict | None = None
    usage_call_id: str | None = None
    run_id: str | None = None
    attachment_sources: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize a message, omitting metadata that was not recorded."""
        data: dict[str, object] = {
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
        }
        if self.attachment_sources:
            data["attachment_schema_version"] = ATTACHMENT_REFERENCE_VERSION
            data["attachment_sources"] = deepcopy(self.attachment_sources)
        if self.run_id is not None:
            data["run_schema_version"] = RUN_REFERENCE_SCHEMA_VERSION
            data["run_id"] = self.run_id
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
        for name in (
            "uncached_input_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "usage_status",
            "cost_status",
            "provider_usage",
            "usage_call_id",
        ):
            value = getattr(self, name)
            if value is not None:
                data[name] = deepcopy(value)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """Validate stored role and content, then restore timestamps and usage metadata."""
        role = data.get("role")
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Invalid message role.")
        content = data.get("content")
        if not isinstance(content, str):
            raise ValueError("Invalid message content.")
        created_at = datetime.fromisoformat(data["created_at"])
        cost = data.get("estimated_cost")
        run_id = data.get("run_id")
        if run_id is not None or "run_schema_version" in data:
            if (data.get("run_schema_version") != RUN_REFERENCE_SCHEMA_VERSION
                    or role != "assistant" or not isinstance(run_id, str)):
                raise ValueError("Unsupported work run reference.")
            if str(UUID(run_id)) != run_id:
                raise ValueError("Invalid work run reference.")
        sources = data.get("attachment_sources", [])
        if (
            (sources or "attachment_schema_version" in data)
            and data.get("attachment_schema_version") != ATTACHMENT_REFERENCE_VERSION
        ):
            raise ValueError("Unsupported attachment reference schema.")
        sources = [
            source.model_dump(mode="json", exclude_none=True)
            for source in ATTACHMENT_SOURCES.validate_python(sources)
        ]
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
            uncached_input_tokens=data.get("uncached_input_tokens"),
            cache_read_tokens=data.get("cache_read_tokens"),
            cache_write_tokens=data.get("cache_write_tokens"),
            reasoning_tokens=data.get("reasoning_tokens"),
            usage_status=data.get("usage_status"),
            cost_status=data.get("cost_status"),
            provider_usage=deepcopy(data.get("provider_usage")),
            usage_call_id=data.get("usage_call_id"),
            run_id=run_id,
            attachment_sources=sources,
        )
