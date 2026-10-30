"""Persist a conversation's folder identity and optional automatic-read exclusions."""

from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorkspaceReadOptions(BaseModel):
    """Select optional automatic exclusions independently from custom ignore rules."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ignore_hidden: bool = Field(default=False, strict=True)
    use_gitignore: bool = Field(default=False, strict=True)


class WorkspaceBinding(BaseModel):
    """Keep workspace references stable across reloads without accessing the filesystem."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "0.1"
    root: str | None = Field(default=None, min_length=1, max_length=4096)
    revision: int = Field(default=0, ge=0, strict=True)
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    read_options: WorkspaceReadOptions = Field(default_factory=WorkspaceReadOptions)

    @field_validator("schema_version")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        """Reject future bindings rather than silently replacing their authority."""
        if value != "0.1":
            raise ValueError("Unsupported workspace binding version.")
        return value

    @field_validator("root")
    @classmethod
    def validate_root(cls, value: str | None) -> str | None:
        """Accept only explicit absolute paths without resolving or creating folders."""
        if value is not None and ("\x00" in value or not Path(value).is_absolute()):
            raise ValueError("Workspace path must be absolute.")
        return value

    @field_validator("session_id")
    @classmethod
    def validate_session(cls, value: str) -> str:
        """Require the canonical UUID used by stored attachment references."""
        if str(UUID(value)) != value:
            raise ValueError("Invalid workspace identity.")
        return value
