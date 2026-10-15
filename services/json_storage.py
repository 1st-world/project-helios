"""Read versioned JSON and publish guarded, atomic saves after preserving legacy originals."""

import hashlib
import json
import math
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

if os.name == "nt":
    import msvcrt
else:
    import fcntl


SCHEMA_VERSION = 1


class JsonStorageError(RuntimeError):
    """Report a safe persistence failure without exposing serialized contents."""

    def __init__(self, message: str, *, reason: str = "write_failed") -> None:
        """Keep a machine-readable reason alongside the safe user-facing message."""
        super().__init__(message)
        self.reason = reason


def _unique_members(pairs: list[tuple]) -> dict:
    """Reject duplicate JSON members instead of silently discarding earlier values."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member.")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    """Reject non-finite JSON numbers, including overflowing exponent notation."""
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON number.")
    return parsed


def _reject_constant(value: str) -> None:
    """Reject JavaScript numeric constants outside the JSON specification."""
    raise ValueError("Unsupported JSON number.")


class JsonDocumentStore:
    """Own a disk snapshot, preserve its first legacy version, and detect stale writers."""

    def __init__(
        self, path: Path, payload_key: str, *, max_bytes: int | None = None,
    ) -> None:
        """Read a document without creating, migrating, or replacing any file."""
        self.path = path
        self.payload_key = payload_key
        self.max_bytes = max_bytes
        self.original: bytes | None = None
        self.version: int | None = None
        self.payload: dict | None = None
        self.migration: dict | None = None
        try:
            original = self._read_current()
        except OSError:
            raise JsonStorageError("Could not read the saved file.", reason="read_failed") from None
        if original is None:
            return
        try:
            document = json.loads(
                original, object_pairs_hook=_unique_members,
                parse_float=_finite_float, parse_constant=_reject_constant,
            )
            if not isinstance(document, dict):
                raise ValueError("Document must be an object.")
            if "schema_version" not in document:
                version, payload, migration = 0, document, None
            else:
                version = document["schema_version"]
                if type(version) is not int or version != SCHEMA_VERSION:
                    raise JsonStorageError(
                        "The saved file uses an unsupported schema version.",
                        reason="unsupported_version",
                    )
                if set(document) - {"schema_version", payload_key, "migration"}:
                    raise ValueError("Unknown document field.")
                payload = document[payload_key]
                if not isinstance(payload, dict):
                    raise ValueError("Payload must be an object.")
                migration = document.get("migration")
                if migration is not None:
                    self._validate_migration(migration)
        except (UnicodeError, ValueError, TypeError, KeyError, RecursionError):
            raise JsonStorageError("The saved file is invalid.", reason="invalid_document") from None
        self.original = original
        self.version, self.payload, self.migration = version, payload, migration

    def _read_current(self) -> bytes | None:
        """Read complete bytes or absence, retaining the existing settings size guard."""
        try:
            with self.path.open("rb") as source:
                data = (
                    source.read() if self.max_bytes is None
                    else source.read(self.max_bytes + 1)
                )
        except FileNotFoundError:
            return None
        if self.max_bytes is not None and len(data) > self.max_bytes:
            raise JsonStorageError("The saved file exceeds its size limit.", reason="invalid_document")
        return data

    def _validate_migration(self, migration: dict) -> None:
        """Validate provenance without following a stored path or exposing private values."""
        if not isinstance(migration, dict) or set(migration) != {
            "from_schema_version", "source_sha256", "backup_file", "policy",
        }:
            raise ValueError("Invalid migration record.")
        digest = migration["source_sha256"]
        if (
            type(migration["from_schema_version"]) is not int
            or migration["from_schema_version"] != 0
            or not isinstance(digest, str) or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or migration["backup_file"] != self._backup_name(digest)
            or not isinstance(migration["policy"], dict)
        ):
            raise ValueError("Invalid migration provenance.")

    def _backup_name(self, digest: str) -> str:
        """Name an immutable original by its source format and exact byte digest."""
        return f"{self.path.name}.v0-{digest}.bak"

    @contextmanager
    def _exclusive(self):
        """Coordinate cooperating writers without blocking the local API event loop."""
        handle = None
        locked = False
        try:
            descriptor = os.open(
                self.path.with_name(self.path.name + ".lock"),
                os.O_RDWR | os.O_CREAT, 0o600,
            )
            handle = os.fdopen(descriptor, "r+b")
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                raise JsonStorageError("Another process is saving this file. Try again.", reason="locked") from None
            yield
        finally:
            if handle is not None:
                try:
                    if locked:
                        handle.seek(0)
                        if os.name == "nt":
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    # Closing the handle releases the lock without masking a committed save.
                    pass
                try:
                    handle.close()
                except OSError:
                    pass

    def _preserve_original(self, backup: Path) -> None:
        """Create a private original once, or verify an earlier migration attempt's copy."""
        created = False
        try:
            try:
                descriptor = os.open(
                    backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                )
                created = True
            except FileExistsError:
                with backup.open("rb") as source:
                    existing = source.read(len(self.original) + 1)
                if existing != self.original:
                    raise JsonStorageError("The original backup could not be verified. Keep the saved file and repair the backup before retrying.", reason="backup_failed")
                return
            with os.fdopen(descriptor, "wb") as output:
                output.write(self.original)
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            if created:
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    pass
            raise JsonStorageError("Could not preserve the original backup. The saved file is unchanged.", reason="backup_failed") from None

    def save(self, payload: dict, *, migration_policy: dict | None = None) -> None:
        """Persist a candidate before publishing the new snapshot to its owning service."""
        temporary = None
        migration = self.migration
        if self.version == 0:
            digest = hashlib.sha256(self.original).hexdigest()
            migration = {
                "from_schema_version": 0, "source_sha256": digest,
                "backup_file": self._backup_name(digest),
                "policy": migration_policy or {},
            }
        envelope = {"schema_version": SCHEMA_VERSION, self.payload_key: payload}
        if migration is not None:
            envelope["migration"] = migration
        try:
            serialized = (json.dumps(
                envelope, ensure_ascii=False, allow_nan=False, indent=2,
            ) + "\n").encode("utf-8")
            if self.max_bytes is not None and len(serialized) > self.max_bytes:
                raise JsonStorageError("The saved file would exceed its size limit.")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._exclusive():
                if self._read_current() != self.original:
                    raise JsonStorageError("The saved file changed outside this app. Restart Helios to reload it before saving again.", reason="conflict")
                if self.version == 0:
                    self._preserve_original(
                        self.path.with_name(migration["backup_file"]),
                    )
                with NamedTemporaryFile(
                    mode="wb", dir=self.path.parent,
                    prefix=f".helios-{self.payload_key}-", suffix=".tmp", delete=False,
                ) as output:
                    temporary = Path(output.name)
                    output.write(serialized)
                    output.flush()
                    os.fsync(output.fileno())
                if self._read_current() != self.original:
                    raise JsonStorageError("The saved file changed during this update. Restart Helios to reload it before saving again.", reason="conflict")
                temporary.replace(self.path)
                self.original, self.version = serialized, SCHEMA_VERSION
                self.payload, self.migration = payload, migration
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
            raise JsonStorageError("Could not save the file. Existing preferences are unchanged.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
