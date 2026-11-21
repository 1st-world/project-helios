"""Review transcript files for explicit, age-filtered storage cleanup."""

import hashlib
import stat
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from services.local_work import check_local_cancelled
from services.project_access import directory_identity, is_link, signature


class ConversationFileChanged(OSError):
    """Report a transcript that no longer matches the registered disk revision."""


class ConversationFileUnavailable(OSError):
    """Report a transcript whose identity cannot be verified safely."""


class ConversationCleanupSelection(BaseModel):
    """Identify the saved transcript revision reviewed for removal."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        """Check the UUID while retaining the spelling used by a legacy transcript."""
        UUID(value)
        return value


class ConversationCleanupRequest(BaseModel):
    """Carry the same age threshold and exactly the items selected for removal."""

    model_config = ConfigDict(extra="forbid")
    updated_before: AwareDatetime
    items: list[ConversationCleanupSelection] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_items(self) -> "ConversationCleanupRequest":
        """Give each conversation one outcome and at most one removal attempt."""
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("Select each conversation only once.")
        return self


def cleanup_timestamp(value: datetime) -> datetime:
    """Normalize saved timestamps, treating older offset-free records as UTC."""
    if value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def inspect_transcript(path: Path, expected: tuple | None) -> dict:
    """Report an ordinary transcript's size and a revision bound to its registered identity."""
    if expected is None:
        raise OSError("No registered transcript file revision is available.")
    path = path.absolute()
    directory = directory_identity(path.parent)
    info = path.lstat()
    if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("The transcript is not an ordinary file.")
    if directory_identity(path.parent) != directory or signature(info) != expected:
        raise ConversationFileChanged("The saved transcript changed.")
    identity = repr((path.name, directory, expected)).encode("ascii")
    return {"size_bytes": info.st_size, "revision": hashlib.sha256(identity).hexdigest()}


def review_transcripts(entries: list[tuple[Path, tuple | None]]) -> list[dict | None]:
    """Read disk metadata for one review page off the application event loop."""
    results = []
    for path, expected in entries:
        check_local_cancelled()
        try:
            results.append(inspect_transcript(path, expected))
        except OSError:
            results.append(None)
    return results


def require_reviewed_transcript(path: Path, expected: tuple | None, revision: str) -> None:
    """Validate the exact reviewed transcript immediately before unlinking it."""
    try:
        current = inspect_transcript(path, expected)
    except (ConversationFileChanged, FileNotFoundError) as error:
        raise ConversationFileChanged("The saved transcript changed.") from error
    except OSError as error:
        raise ConversationFileUnavailable("The saved transcript cannot be verified.") from error
    if current["revision"] != revision:
        raise ConversationFileChanged("The saved transcript changed.")
