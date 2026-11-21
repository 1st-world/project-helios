"""Describe reviewed attachment copies and bind removal to a managed file revision."""

import hashlib
import stat
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.attachment import is_upload_id
from services.project_access import is_link, signature


class AttachmentCleanupSelection(BaseModel):
    """Identify one manifest-owned copy and the file revision reviewed by the user."""

    model_config = ConfigDict(extra="forbid")
    attachment_id: UUID
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class AttachmentCleanupRequest(BaseModel):
    """Carry a bounded set of explicitly selected attachment copies."""

    model_config = ConfigDict(extra="forbid")
    items: list[AttachmentCleanupSelection] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_items(self) -> "AttachmentCleanupRequest":
        """Allow only one removal attempt and outcome for each copy."""
        if len({item.attachment_id for item in self.items}) != len(self.items):
            raise ValueError("Select each attachment copy only once.")
        return self


def attachment_blob_id(name: str) -> str | None:
    """Recognize only the exact filenames issued for published copies."""
    if not name.endswith(".blob"):
        return None
    identity = name[:-5]
    return identity if is_upload_id(identity) else None


def reviewed_copy_file(path: Path, owner: tuple) -> dict:
    """Describe a regular single-link copy without reading its contents."""
    info = path.lstat()
    if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("The attachment copy is not an ordinary file.")
    identity = repr((path.name, signature(info), owner)).encode("utf-8")
    return {"size_bytes": info.st_size, "revision": hashlib.sha256(identity).hexdigest()}
