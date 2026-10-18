"""Retain immutable uploaded originals outside caches, with atomic publication and explicit deletion.

Only app-issued UUIDs address stored files; client filenames never determine disk paths.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

from services.attachment_errors import AttachmentError
from services.local_work import check_local_cancelled


class AttachmentStore:
    """Manage durable originals and process-local read leases without open database handles."""

    SCHEMA_VERSION = "0.1"

    def __init__(self, root: Path) -> None:
        """Defer filesystem work until uploads or existing records are accessed."""
        self.root = root
        self._lock = RLock()
        self._ready = False
        self._busy: dict[str, int] = {}

    def _initialize(self) -> None:
        """Validate the manifest before cleaning interrupted staging and orphaned blobs."""
        if self._ready:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = self.root / "manifest.sqlite3"
        if not manifest.exists() and any(self.root.glob("*.blob")):
            raise AttachmentError(
                "attachment_store_version", "The attachment manifest is missing. Existing originals were preserved; restore the storage backup.", "", 409,
            )
        with closing(sqlite3.connect(manifest)) as db:
            db.execute("BEGIN IMMEDIATE")
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            versions = (
                db.execute("SELECT schema_version FROM store_metadata").fetchall()
                if "store_metadata" in tables else []
            )
            if tables and (
                versions != [(self.SCHEMA_VERSION,)]
                or not {"uploads", "store_metadata"} <= tables
            ):
                raise AttachmentError(
                    "attachment_store_version", "Attachment storage requires a compatible application version.", "", 409,
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS uploads ("
                "id TEXT PRIMARY KEY, filename TEXT NOT NULL, size_bytes INTEGER NOT NULL, "
                "sha256 TEXT NOT NULL, created_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)"
            )
            if not tables:
                db.execute("CREATE TABLE store_metadata (schema_version TEXT NOT NULL)")
                db.execute("INSERT INTO store_metadata VALUES (?)", (self.SCHEMA_VERSION,))
            retained = {row[0] for row in db.execute("SELECT id FROM uploads WHERE deleted=0")}
            db.commit()
        for path in self.root.iterdir():
            if path.suffix not in {".part", ".blob"}:
                continue
            try:
                identity = str(UUID(path.stem))
            except ValueError:
                continue
            if path.suffix == ".part" or identity not in retained:
                path.unlink(missing_ok=True)
        self._ready = True

    @staticmethod
    def validate_filename(filename: str) -> str:
        """Accept a display basename without permitting paths or control characters."""
        if (
            not filename.strip() or len(filename) > 255
            or filename in {".", ".."}
            or any(char in filename for char in '/\\:')
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)
        ):
            raise AttachmentError(
                "attachment_name_invalid", "Supply a filename without directories or control characters.", "",
            )
        return filename

    def stage(self, filename: str, max_bytes: int) -> "StagedUpload":
        """Allocate an owned writer before asynchronous work can be cancelled."""
        return StagedUpload(self, self.validate_filename(filename), max_bytes)

    def get(self, attachment_id: str) -> dict:
        """Resolve a live original or require explicit reselection after deletion or loss."""
        identity = str(UUID(attachment_id))
        with self._lock:
            self._initialize()
            with closing(sqlite3.connect(self.root / "manifest.sqlite3")) as db:
                db.row_factory = sqlite3.Row
                row = db.execute("SELECT * FROM uploads WHERE id=?", (identity,)).fetchone()
            if row is None or row["deleted"] or not (self.root / f"{identity}.blob").is_file():
                raise AttachmentError(
                    "attachment_source_missing", "This uploaded original is unavailable. Select the file again.", identity, 410,
                )
            return self._metadata(dict(row))

    @staticmethod
    def _metadata(row: dict) -> dict:
        """Expose an upload's identity and integrity information without its disk location."""
        return {
            "attachment_id": row["id"], "filename": row["filename"],
            "size_bytes": row["size_bytes"], "sha256": row["sha256"],
            "created_at": row["created_at"],
            "available": True,
            "reference": {"source": "upload", "attachment_id": row["id"]},
        }

    def list(self) -> list[dict]:
        """List retained uploads independently of workspace and conversation membership."""
        with self._lock:
            if not self.root.exists():
                return []
            self._initialize()
            with closing(sqlite3.connect(self.root / "manifest.sqlite3")) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute("SELECT * FROM uploads WHERE deleted=0 ORDER BY created_at DESC").fetchall()
            return [
                {
                    **self._metadata(dict(row)),
                    "available": (self.root / f"{row['id']}.blob").is_file(),
                }
                for row in rows
            ]

    def validate_originals(self, attachment_ids: list[str]) -> None:
        """Verify retained originals before reusing an exact failed-request payload."""
        for identity in set(attachment_ids):
            check_local_cancelled()
            metadata = self.get(identity)
            path = self.root / f"{metadata['attachment_id']}.blob"
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as original:
                while chunk := original.read(65536):
                    check_local_cancelled()
                    size += len(chunk)
                    if size > metadata["size_bytes"]:
                        break
                    digest.update(chunk)
            if size != metadata["size_bytes"] or digest.hexdigest() != metadata["sha256"]:
                raise AttachmentError(
                    "attachment_source_damaged", "The uploaded original changed or is damaged. Select the file again.", identity, 410,
                )

    def claim(self, attachment_ids: list[str]) -> Callable[[], None]:
        """Prevent deletion until all owning requests finish worker and provider cleanup."""
        identities = {str(UUID(value)) for value in attachment_ids}
        with self._lock:
            for identity in identities:
                self._busy[identity] = self._busy.get(identity, 0) + 1
        released = False

        def release() -> None:
            """Release each original once, including after a cancelled request."""
            nonlocal released
            with self._lock:
                if released:
                    return
                released = True
                for identity in identities:
                    self._busy[identity] -= 1
                    if not self._busy[identity]:
                        del self._busy[identity]

        return release

    def delete(self, attachment_id: str) -> None:
        """Tombstone an idle original before removing bytes so stale references never revive."""
        identity = str(UUID(attachment_id))
        with self._lock:
            self._initialize()
            if self._busy.get(identity):
                raise AttachmentError(
                    "attachment_in_use", "This attachment is in use. Stop the request and wait for cleanup before deleting it.", identity, 409,
                )
            with closing(sqlite3.connect(self.root / "manifest.sqlite3")) as db:
                if not db.execute("SELECT 1 FROM uploads WHERE id=?", (identity,)).fetchone():
                    raise AttachmentError("attachment_source_missing", "Uploaded attachment not found.", identity, 410)
                db.execute("UPDATE uploads SET deleted=1 WHERE id=?", (identity,))
                db.commit()
            (self.root / f"{identity}.blob").unlink(missing_ok=True)


class StagedUpload:
    """Own an unpublished upload and remove partial bytes on every unsuccessful exit."""

    def __init__(self, store: AttachmentStore, filename: str, max_bytes: int) -> None:
        """Allocate an opaque identity without creating files on the event loop."""
        self.store, self.filename, self.max_bytes = store, filename, max_bytes
        self.id = str(uuid4())
        self.path = store.root / f"{self.id}.part"
        self.size = 0
        self.digest = hashlib.sha256()
        self._file = None
        self.published = False

    def open(self) -> None:
        """Initialize storage and exclusively create this upload's staging file."""
        with self.store._lock:
            self.store._initialize()
            self._file = self.path.open("xb")

    def write(self, data: bytes) -> None:
        """Enforce actual byte limits independently of the supplied Content-Length."""
        check_local_cancelled()
        if self.size + len(data) > self.max_bytes:
            raise AttachmentError(
                "attachment_too_large", f"Upload exceeds the local read limit of {self.max_bytes} bytes.", self.filename, 413,
            )
        self._file.write(data)
        self.digest.update(data)
        self.size += len(data)

    def seal(self) -> None:
        """Flush the complete original before validation and atomic publication."""
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self._file = None

    def publish(self) -> dict:
        """Publish the validated original only after the manifest transaction succeeds."""
        check_local_cancelled()
        with self.store._lock:
            created_at = datetime.now(timezone.utc).isoformat()
            self.path.replace(self.store.root / f"{self.id}.blob")
            with closing(sqlite3.connect(self.store.root / "manifest.sqlite3")) as db:
                db.execute(
                    "INSERT INTO uploads (id,filename,size_bytes,sha256,created_at) VALUES (?,?,?,?,?)",
                    (self.id, self.filename, self.size, self.digest.hexdigest(), created_at),
                )
                db.commit()
            self.published = True
            return self.store.get(self.id)

    def abort(self, *, discard: bool = False) -> None:
        """Close partial writes and remove originals that were never published."""
        if self._file is not None:
            self._file.close()
            self._file = None
        self.path.unlink(missing_ok=True)
        if self.published and discard:
            self.store.delete(self.id)
        if not self.published:
            (self.store.root / f"{self.id}.blob").unlink(missing_ok=True)
