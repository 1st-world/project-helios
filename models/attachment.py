"""Define versioned attachment references independently of provider payloads and storage paths."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


AttachmentPath = Annotated[str, Field(min_length=1, max_length=4096)]


class WorkspaceAttachment(BaseModel):
    """Identify an explicitly selected file inside one workspace session."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source: Literal["workspace"]
    path: AttachmentPath
    workspace_revision: int | None = Field(default=None, ge=0, strict=True)
    workspace_session_id: str | None = Field(default=None, min_length=1, max_length=64)


class UploadedAttachment(BaseModel):
    """Identify an immutable original using an app-issued identifier."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source: Literal["upload"]
    attachment_id: UUID


AttachmentSource = Annotated[
    WorkspaceAttachment | UploadedAttachment, Field(discriminator="source")
]
ATTACHMENT_SOURCES = TypeAdapter(list[AttachmentSource])
ATTACHMENT_REFERENCE_VERSION = "0.1"
