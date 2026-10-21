"""Retain immutable attachment copies while messages, drafts, or requests reference them.

Only app-issued UUIDs address stored files; client filenames never determine disk paths.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from time import time
from uuid import UUID, uuid4

from services.attachment_errors import AttachmentError
from services.local_work import check_local_cancelled

logger = logging.getLogger(__name__)


class AttachmentStore:
    """Manage durable originals and process-local read leases without open database handles."""

    SCHEMA_VERSION = "0.2"
    DRAFT_TTL = 24 * 60 * 60

    def __init__(
        self, root: Path, *, references: Callable[[], set[str]] | None = None,
        referenced: Callable[[str], bool] | None = None,
    ) -> None:
        """Defer filesystem work until uploads or existing records are accessed."""
        self.root = root
        self.references = references or set
        self.referenced = referenced or (lambda identity: identity in self.references())
        self.on_change: Callable[[set[str] | None], None] | None = None
        self._lock = RLock()
        self._ready = False
        self._busy: dict[str, int] = {}
        self._released: set[str] = set()
        self._reconciled = False
        self._retry_files: dict[str, tuple[float, int]] = {}

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
                versions not in ([("0.1",)], [(self.SCHEMA_VERSION,)])
                or not {"uploads", "store_metadata"} <= tables
            ):
                raise AttachmentError(
                    "attachment_store_version", "Attachment storage requires a compatible application version.", "", 409,
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS uploads ("
                "id TEXT PRIMARY KEY, filename TEXT NOT NULL, size_bytes INTEGER NOT NULL, "
                "sha256 TEXT NOT NULL, created_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, "
                "unclaimed_until REAL NOT NULL)"
            )
            if versions == [("0.1",)]:
                # Give existing unowned copies a full grace period during the first upgrade.
                db.execute("ALTER TABLE uploads ADD COLUMN unclaimed_until REAL NOT NULL DEFAULT 0")
                db.execute("UPDATE uploads SET unclaimed_until=?", (time() + self.DRAFT_TTL,))
                db.execute("UPDATE store_metadata SET schema_version=?", (self.SCHEMA_VERSION,))
            db.execute(
                "CREATE TABLE IF NOT EXISTS attachment_drafts ("
                "draft_id TEXT NOT NULL, attachment_id TEXT NOT NULL, expires_at REAL NOT NULL, "
                "PRIMARY KEY (draft_id, attachment_id))"
            )
            if not tables:
                db.execute("CREATE TABLE store_metadata (schema_version TEXT NOT NULL)")
                db.execute("INSERT INTO store_metadata VALUES (?)", (self.SCHEMA_VERSION,))
            db.execute(
                "CREATE INDEX IF NOT EXISTS uploads_expiry ON uploads(unclaimed_until) "
                "WHERE deleted=0 AND unclaimed_until>0"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS drafts_expiry ON attachment_drafts(expires_at)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS drafts_attachment "
                "ON attachment_drafts(attachment_id, expires_at)"
            )
            retained = {row[0] for row in db.execute("SELECT id FROM uploads WHERE deleted=0")}
            db.commit()
        paths = list(self.root.iterdir())
        self._ready = True
        for path in paths:
            if path.suffix not in {".part", ".blob"}:
                continue
            try:
                identity = str(UUID(path.stem))
            except ValueError:
                continue
            if path.suffix == ".part" or identity not in retained:
                self._remove_file(path.name)
        if self._retry_files:
            self._changed()

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
        """List copies still retained by ownership or the temporary selection grace period."""
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
                        self._released.add(identity)
                if identities:
                    self._changed(identities)

        return release

    def delete(self, attachment_id: str) -> None:
        """Tombstone an idle original before removing bytes so stale references never revive."""
        identity = str(UUID(attachment_id))
        with self._lock:
            self._initialize()
            with closing(sqlite3.connect(self.root / "manifest.sqlite3")) as db:
                if not db.execute("SELECT 1 FROM uploads WHERE id=?", (identity,)).fetchone():
                    raise AttachmentError("attachment_source_missing", "Uploaded attachment not found.", identity, 410)
                if self._is_protected(identity, db):
                    raise AttachmentError(
                        "attachment_in_use", "This attachment is used by a conversation, draft, or request. Remove those references before deleting it.", identity, 409,
                    )
                db.execute("UPDATE uploads SET deleted=1 WHERE id=?", (identity,))
                db.commit()
            self._remove_file(f"{identity}.blob")
            self._changed()

    def _is_protected(self, identity: str, db: sqlite3.Connection) -> bool:
        """Check one copy's committed, active-reader, and live-draft owners."""
        return bool(
            self._busy.get(identity) or self.referenced(identity)
            or db.execute(
                "SELECT 1 FROM attachment_drafts WHERE attachment_id=? AND expires_at>? LIMIT 1",
                (identity, time()),
            ).fetchone()
        )

    def replace_draft(
        self, draft_id: str, attachment_ids: list[str],
        released_attachment_ids: list[str] | None = None,
    ) -> None:
        """Renew one tab's selection and make released copies eligible for cleanup."""
        draft_id = str(UUID(draft_id))
        identities = {str(UUID(value)) for value in attachment_ids}
        released = {str(UUID(value)) for value in released_attachment_ids or []} - identities
        with self._lock:
            self._initialize()
            with closing(sqlite3.connect(self.root / "manifest.sqlite3")) as db:
                previous = {row[0] for row in db.execute(
                    "SELECT attachment_id FROM attachment_drafts WHERE draft_id=?", (draft_id,),
                )}
                for identity in identities - previous:
                    self.get(identity)
                if identities == previous:
                    db.execute(
                        "UPDATE attachment_drafts SET expires_at=? WHERE draft_id=?",
                        (time() + self.DRAFT_TTL, draft_id),
                    )
                else:
                    db.execute("DELETE FROM attachment_drafts WHERE draft_id=?", (draft_id,))
                    db.executemany(
                        "INSERT INTO attachment_drafts VALUES (?,?,?)",
                        [(draft_id, identity, time() + self.DRAFT_TTL) for identity in identities],
                    )
                    db.executemany(
                        "UPDATE uploads SET unclaimed_until=0 WHERE id=?",
                        [(identity,) for identity in identities - previous],
                    )
                db.commit()
            removed = (previous - identities) | released
            try:
                if removed:
                    self.collect(removed)
            finally:
                self._changed()

    def collect(self, candidates: set[str] | None = None) -> list[str]:
        """Collect affected or newly expired copies; reconcile all eligible records only once."""
        with self._lock:
            if not self.root.exists():
                self._released.clear()
                self._reconciled = True
                return []
            self._initialize()
            now = time()
            pending = {str(UUID(identity)) for identity in (candidates or set()) | self._released}
            with closing(sqlite3.connect(self.root / "manifest.sqlite3")) as db:
                pending.update(row[0] for row in db.execute(
                    "SELECT attachment_id FROM attachment_drafts WHERE expires_at<=?", (now,),
                ))
                pending.update(row[0] for row in db.execute(
                    "SELECT id FROM uploads WHERE deleted=0 "
                    "AND unclaimed_until>0 AND unclaimed_until<=?", (now,),
                ))
                if not self._reconciled:
                    pending.update(row[0] for row in db.execute(
                        "SELECT id FROM uploads WHERE deleted=0 AND unclaimed_until=0",
                    ))
                db.execute("DELETE FROM attachment_drafts WHERE expires_at<=?", (now,))
                db.executemany(
                    "UPDATE uploads SET unclaimed_until=0 WHERE id=?",
                    [(identity,) for identity in pending],
                )
                db.commit()
                # Persist release intent before ownership verification can defer deletion.
                self._released.update(pending)
                identities = {
                    identity for identity in pending
                    if db.execute("SELECT 1 FROM uploads WHERE id=? AND deleted=0", (identity,)).fetchone()
                    and not self._is_protected(identity, db)
                }
                db.executemany(
                    "UPDATE uploads SET deleted=1 WHERE id=?",
                    [(identity,) for identity in identities],
                )
                db.commit()
            self._released.clear()
            self._reconciled = True
            retry = {name for name, (due, _) in self._retry_files.items() if due <= now}
            for name in retry | {f"{identity}.blob" for identity in identities}:
                self._remove_file(name)
            return list(identities)

    def next_cleanup_at(self) -> float | None:
        """Return the earliest actual expiry or failed-file retry without a polling interval."""
        with self._lock:
            if not self.root.exists():
                return None
            self._initialize()
            if self._released:
                return time()
            with closing(sqlite3.connect(self.root / "manifest.sqlite3")) as db:
                deadlines = [
                    db.execute(
                        "SELECT MIN(unclaimed_until) FROM uploads "
                        "WHERE deleted=0 AND unclaimed_until>0"
                    ).fetchone()[0],
                    db.execute("SELECT MIN(expires_at) FROM attachment_drafts").fetchone()[0],
                    *(due for due, _ in self._retry_files.values()),
                ]
            return min((due for due in deadlines if due is not None), default=None)

    def _remove_file(self, name: str) -> None:
        """Retry only failed owned paths with exponential delay capped at five minutes."""
        try:
            (self.root / name).unlink(missing_ok=True)
        except OSError:
            attempts = self._retry_files.get(name, (0, 0))[1] + 1
            self._retry_files[name] = (time() + min(2 ** min(attempts - 1, 9), 300), attempts)
            logger.warning("Could not remove an unused attachment copy; a targeted retry was scheduled.", exc_info=True)
        else:
            self._retry_files.pop(name, None)

    def _changed(self, identities: set[str] | None = None) -> None:
        """Wake the cleanup scheduler after a deadline or owning request changes."""
        if self.on_change is not None:
            self.on_change(identities)


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
                    "INSERT INTO uploads (id,filename,size_bytes,sha256,created_at,unclaimed_until) VALUES (?,?,?,?,?,?)",
                    (self.id, self.filename, self.size, self.digest.hexdigest(), created_at, time() + self.store.DRAFT_TTL),
                )
                db.commit()
            self.published = True
            self.store._changed()
            return self.store.get(self.id)

    def abort(self, *, discard: bool = False) -> None:
        """Close partial writes and remove originals that were never published."""
        if self._file is not None:
            self._file.close()
            self._file = None
        with self.store._lock:
            self.store._remove_file(self.path.name)
            if self.published and discard:
                self.store.delete(self.id)
            if not self.published:
                self.store._remove_file(f"{self.id}.blob")
            if self.store._retry_files:
                self.store._changed()
