"""Review and remove selected migration backups from the local app store."""

import hashlib
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.json_storage import JsonDocumentStore, JsonStorageError, migration_backup_pattern
from services.local_work import check_local_cancelled
from services.project_access import directory_identity, is_link, signature


Registry = Literal["settings", "profiles"]
BackupOutcome = Literal["removed", "missing", "changed", "protected", "unavailable", "failed"]


class BackupSelection(BaseModel):
    """Identify the revision of a managed backup reviewed by the user."""

    model_config = ConfigDict(extra="forbid")
    registry: Registry
    name: str = Field(min_length=1, max_length=255)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class BackupRemovalRequest(BaseModel):
    """Describe the explicitly selected files in one cleanup request."""

    model_config = ConfigDict(extra="forbid")
    items: list[BackupSelection] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_items(self) -> "BackupRemovalRequest":
        """Reject two revisions or repeated selections for the same backup name."""
        if len({(item.registry, item.name) for item in self.items}) != len(self.items):
            raise ValueError("Select each backup only once.")
        return self


@dataclass(frozen=True)
class BackupEntry:
    """Describe a reviewed app-owned backup without exposing its contents."""

    registry: Registry
    name: str
    revision: str
    size_bytes: int
    modified_at: str
    protected: bool


def _entry(
    registry: Registry, path: Path, root_identity: tuple[tuple[int, int], ...], protected: set[str],
) -> BackupEntry:
    """Capture the revision and metadata of a regular, single-link backup."""
    info = path.lstat()
    if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("The backup is not an ordinary file.")
    identity = repr((path.name, root_identity, signature(info))).encode("utf-8")
    return BackupEntry(
        registry, path.name, hashlib.sha256(identity).hexdigest(), info.st_size,
        datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(), path.name in protected,
    )


def inspect_migration_backups(documents: Mapping[Registry, JsonDocumentStore]) -> dict:
    """List reviewable backups and report registries whose scans could not be completed."""
    items: list[BackupEntry] = []
    incomplete: list[Registry] = []
    for registry, document in documents.items():
        root = document.path.parent.absolute()
        pattern = migration_backup_pattern(document.path.name)
        found = []
        partial = False
        try:
            identity = directory_identity(root)
            protected = document.retained_backup_names()
            for path in root.iterdir():
                check_local_cancelled()
                if pattern.fullmatch(path.name):
                    try:
                        found.append(_entry(registry, path, identity, protected))
                    except (OSError, ValueError, OverflowError):
                        partial = True
            if directory_identity(root) != identity or document.retained_backup_names() != protected:
                raise OSError("The registry or its directory changed during review.")
        except (OSError, JsonStorageError):
            incomplete.append(registry)
            continue
        if partial:
            incomplete.append(registry)
        items.extend(found)
    items.sort(key=lambda entry: (entry.modified_at, entry.name), reverse=True)
    return {"items": [asdict(item) for item in items], "incomplete": incomplete}


def _remove_one(
    selection: BackupSelection, document: JsonDocumentStore, root: Path,
    root_identity: tuple[tuple[int, int], ...], protected: set[str],
) -> BackupOutcome:
    """Compare the reviewed identity and unlink just one eligible backup."""
    if not migration_backup_pattern(document.path.name).fullmatch(selection.name):
        return "unavailable"
    if selection.name in protected:
        return "protected"
    path = root / selection.name
    try:
        entry = _entry(selection.registry, path, root_identity, protected)
        if directory_identity(root) != root_identity:
            return "unavailable"
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError, OverflowError):
        return "unavailable"
    if entry.revision != selection.revision:
        return "changed"
    check_local_cancelled()
    try:
        path.unlink()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "failed"
    return "removed"


def remove_migration_backups(
    documents: Mapping[Registry, JsonDocumentStore], items: list[BackupSelection],
) -> dict:
    """Remove selected backups under each registry's save lock and report individual outcomes."""
    outcomes: dict[tuple[Registry, str], BackupOutcome] = {}
    for registry, document in documents.items():
        selected = [item for item in items if item.registry == registry]
        if not selected:
            continue
        root = document.path.parent.absolute()
        try:
            identity = directory_identity(root)
            with document.guard_backup_removal() as protected:
                if directory_identity(root) != identity:
                    raise OSError("The backup directory changed before removal.")
                for item in selected:
                    outcomes[(registry, item.name)] = _remove_one(item, document, root, identity, protected)
        except (OSError, JsonStorageError):
            continue
    return {"items": [
        {**item.model_dump(), "outcome": outcomes.get((item.registry, item.name), "unavailable")}
        for item in items
    ]}
