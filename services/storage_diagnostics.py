"""Inspect Helios data without starting the app or mutating its stores."""

import argparse
import hashlib
import json
import sqlite3
import stat
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID

from config import PROJECT_ROOT
from models.conversation import Conversation
from services.attachment_store import AttachmentStore
from services.json_storage import SCHEMA_VERSION, JsonStorageError, decode_storage_json
from services.profile_service import ProfileService
from services.settings_service import SETTINGS_SCHEMA_VERSION, SettingsService
from services.work_run_store import WORK_RUN_SCHEMA_VERSION


PROBLEMS = {"invalid_data", "unsupported_version", "missing_reference"}
SQLITE_TABLES = {
    "data/work_runs.sqlite3": {"work_metadata", "work_runs", "model_steps", "tool_steps"},
    "data/attachments/manifest.sqlite3": {"uploads", "store_metadata"},
    "logs/usage_calls.sqlite3": {"usage_calls"},
    "logs/summary_usage.sqlite3": {"summary_usage"},
}
MANIFEST = "data/attachments/manifest.sqlite3"


def is_upload_id(value: object) -> bool:
    """Recognize a canonical upload UUID without accepting alternate filename spellings."""
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


class SQLiteSidecarsPresent(OSError):
    """Defer a database check while journal or WAL files may affect its contents."""


@dataclass(frozen=True)
class Finding:
    """Expose a stable classification and a safe, root-relative location."""

    path: str
    status: str
    code: str
    message: str


@dataclass(frozen=True)
class AttachmentFingerprint:
    """Record the size and digest of an active attachment without its original name."""

    size_bytes: int
    sha256: str


class StorageScan:
    """Collect content-free findings from a data directory that is at rest."""

    def __init__(self, root: Path, *, deep: bool = False) -> None:
        """Bind the scan to one storage root."""
        self.root = root
        self.deep = deep
        self.findings: list[Finding] = []

    def add(self, path: str, status: str, code: str, message: str) -> None:
        """Keep diagnostics separate from the data that produced them."""
        self.findings.append(Finding(path, status, code, message))

    def stamp(self, path: Path, *, directory: bool = False) -> tuple | None:
        """Identify an ordinary file or directory without opening an external target."""
        if not path.resolve().is_relative_to(self.root):
            raise OSError("Storage target is outside the selected directory.")
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(info.st_mode):
            raise OSError("Unexpected storage entry type.")
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    @contextmanager
    def stable(self, path: Path, *, directory: bool = False):
        """Treat a detected concurrent change as an unavailable check."""
        before = self.stamp(path, directory=directory)
        try:
            yield before
        finally:
            if self.stamp(path, directory=directory) != before:
                raise OSError("Storage changed during the check.")

    def unavailable(self, name: str) -> None:
        """Avoid interpreting read failures or concurrent writes as data damage."""
        self.add(name, "unavailable", "read_unavailable",
                 "The check could not read a stable local entry. Close Helios and try again.")

    def json_registry(self, name: str, loader, current_version: str):
        """Reuse the app's read-only registry validation and check its backup claim."""
        path = self.root / name
        try:
            with self.stable(path) as original:
                service = loader(path) if original is not None else None
        except JsonStorageError as error:
            if error.reason == "unsupported_version":
                self.add(name, "unsupported_version", "unsupported_schema",
                         "Use a Helios version that supports this schema.")
            elif error.reason == "invalid_document":
                self.add(name, "invalid_data", "invalid_registry",
                         "The file could not be validated. Preserve it and verify a backup before restoring.")
            else:
                self.unavailable(name)
            return None
        except (OSError, RuntimeError):
            self.unavailable(name)
            return None
        if service is None:
            self.add(name, "not_initialized", "registry_absent", "No saved file was found.")
            return None
        document = service._document
        if document.version == current_version:
            self.add(name, "ok", "registry_valid", "The saved registry passed app validation.")
        else:
            self.add(name, "compatible_legacy", "legacy_registry",
                     "The previous schema can be loaded by this version of Helios.")
        if document.migration is not None:
            self.backup(name, document.migration)
        return service

    def backup(self, name: str, migration: dict) -> None:
        """Check that the claimed migration source still has its recorded bytes."""
        path = (self.root / name).with_name(migration["backup_file"])
        location = path.relative_to(self.root).as_posix()
        try:
            with self.stable(path) as info:
                digest = None
                if info is not None:
                    with path.open("rb") as source:
                        digest = hashlib.file_digest(source, "sha256").hexdigest()
        except (OSError, RuntimeError):
            self.unavailable(location)
            return
        if digest is None:
            self.add(location, "missing_reference", "migration_backup_missing",
                     "The registry refers to a migration backup that is missing.")
        elif digest != migration["source_sha256"]:
            self.add(location, "invalid_data", "migration_backup_mismatch",
                     "The backup bytes differ from the recorded migration source.")

    def conversations(self, profiles: set[str] | None) -> set[str]:
        """Check transcript structure and collect references from readable conversations."""
        name = "conversation"
        path = self.root / name
        references = set()
        try:
            with self.stable(path, directory=True) as info:
                paths = sorted(entry for entry in path.iterdir() if entry.suffix == ".json") if info else []
        except (OSError, RuntimeError):
            self.unavailable(name)
            return references
        if not paths:
            self.add(name, "not_initialized", "conversations_absent", "No saved conversations were found.")
            return references
        readable = 0
        for path in paths:
            location = path.relative_to(self.root).as_posix()
            try:
                with self.stable(path):
                    data = decode_storage_json(path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict) or str(UUID(data["id"])) + ".json" != path.name:
                        raise ValueError("Invalid conversation identity.")
                    memory = data.get("memory_summary", "")
                    count = data.get("summarized_message_count", 0)
                    messages = data.get("messages", [])
                    invalid_memory = (
                        isinstance(memory, str) and type(count) is int and isinstance(messages, list)
                        and (not 0 <= count <= len(messages) or bool(memory.strip()) != (count > 0))
                    )
                    if invalid_memory:
                        data = {**data, "memory_summary": "", "summarized_message_count": 0}
                    conversation = Conversation.from_dict(data)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
                self.add(location, "invalid_data", "invalid_conversation", "The saved conversation is invalid.")
                continue
            except (OSError, RuntimeError):
                self.unavailable(location)
                continue
            readable += 1
            if invalid_memory:
                self.add(location, "invalid_data", "invalid_memory_coverage",
                         "Saved memory and its message coverage disagree. Preserve the transcript.")
            if (profiles is not None and conversation.profile_id is not None
                    and conversation.profile_id not in profiles):
                self.add(location, "review", "profile_reference_missing",
                         "The saved selection refers to a missing profile. Select a profile to continue.")
            references.update(source["attachment_id"] for message in conversation.messages
                              for source in message.attachment_sources if source["source"] == "upload")
        status = "ok" if readable == len(paths) else "info"
        self.add(name, status, "conversations_read", f"Read {readable} of {len(paths)} conversation files.")
        return references

    def has_sidecars(self, path: Path) -> bool:
        """Skip SQLite snapshots that could depend on journal or WAL state."""
        return any(self.stamp(path.with_name(path.name + suffix)) is not None
                   for suffix in ("-journal", "-wal", "-shm"))

    def database(self, name: str) -> tuple[str, dict[str, AttachmentFingerprint] | None]:
        """Run physical and schema checks without opening a writable SQLite connection."""
        path = self.root / name
        try:
            with self.stable(path) as info:
                if self.has_sidecars(path):
                    raise SQLiteSidecarsPresent
                try:
                    if info is None:
                        result = ("not_initialized", None)
                    else:
                        # Immutable mode avoids creating a WAL index or recovering a journal.
                        uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
                        with closing(sqlite3.connect(uri, uri=True)) as db:
                            db.execute("PRAGMA trusted_schema=OFF")
                            result = self.database_contents(name, db)
                finally:
                    if self.has_sidecars(path):
                        raise SQLiteSidecarsPresent
        except SQLiteSidecarsPresent:
            self.add(name, "unavailable", "sqlite_sidecars_present",
                     "Journal or WAL files are present. Close Helios or inspect a consistent offline copy.")
            return "unavailable", None
        except (OSError, RuntimeError):
            self.unavailable(name)
            return "unavailable", None
        except sqlite3.Error as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xff
            if code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_ERROR}:
                result = ("invalid_data", None)
            else:
                self.unavailable(name)
                return "unavailable", None
        status, uploads = result
        code, message = {
            "ok": ("sqlite_valid", "SQLite structure, indexes, and declared foreign keys passed validation."
                   if self.deep else "SQLite structure and declared foreign keys passed validation."),
            "not_initialized": ("database_absent", "No initialized database was found."),
            "compatible_legacy": ("legacy_database", "The database uses a supported previous schema."),
            "unsupported_version": ("unsupported_schema", "Use a Helios version that supports this database schema."),
            "invalid_data": ("invalid_database", "The database structure, schema, or declared foreign keys are invalid."),
        }[status]
        self.add(name, status, code, message)
        return status, uploads

    def database_contents(
        self, name: str, db: sqlite3.Connection,
    ) -> tuple[str, dict[str, AttachmentFingerprint] | None]:
        """Distinguish a compatible prior schema from missing structure and damage."""
        check = "PRAGMA integrity_check(1)" if self.deep else "PRAGMA quick_check(1)"
        if db.execute(check).fetchall() != [("ok",)]:
            return "invalid_data", None
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return "not_initialized", None
        version = current = None
        if name == MANIFEST and "store_metadata" in tables:
            versions = db.execute("SELECT schema_version FROM store_metadata LIMIT 2").fetchall()
            current, previous = AttachmentStore.SCHEMA_VERSION, {"0.1"}
        elif name == "data/work_runs.sqlite3" and "work_metadata" in tables:
            versions = db.execute("SELECT value FROM work_metadata WHERE key='schema_version' LIMIT 2").fetchall()
            current, previous = WORK_RUN_SCHEMA_VERSION, {"0.1", "0.2"}
        if current is not None:
            if len(versions) != 1 or not isinstance(versions[0][0], str):
                return "invalid_data", None
            version = versions[0][0]
            if version != current and version not in previous:
                return "unsupported_version", None
        if not SQLITE_TABLES[name] <= tables:
            return "invalid_data", None
        if name == "data/work_runs.sqlite3":
            if ((version in {"0.2", current} and "work_execution_segments" not in tables)
                    or (version == current and not {"work_segment_contexts", "work_resume_contexts"} <= tables)):
                return "invalid_data", None
            db.execute("SELECT id, conversation_id, status, data FROM work_runs LIMIT 0")
        elif name == "logs/usage_calls.sqlite3":
            db.execute("SELECT id, conversation_id, kind, status, started_at, source, data FROM usage_calls LIMIT 0")
        elif name == "logs/summary_usage.sqlite3":
            db.execute("SELECT id, data FROM summary_usage LIMIT 0")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            return "invalid_data", None
        uploads = None
        if name == MANIFEST:
            uploads = {}
            if version == current:
                if "attachment_drafts" not in tables:
                    return "invalid_data", None
                db.execute("SELECT unclaimed_until FROM uploads LIMIT 0")
            for identity, size, digest, deleted in db.execute("SELECT id, size_bytes, sha256, deleted FROM uploads"):
                if (not is_upload_id(identity) or type(size) is not int or size < 0
                        or type(deleted) is not int or deleted not in {0, 1}
                        or not isinstance(digest, str) or len(digest) != 64
                        or any(character not in "0123456789abcdef" for character in digest)):
                    return "invalid_data", None
                if not deleted:
                    uploads[identity] = AttachmentFingerprint(size, digest)
        return ("ok" if current == version else "compatible_legacy"), uploads

    def attachment_store(self, references: set[str]) -> None:
        """Discard attachment findings if the manifest changes during the copy checks."""
        path = self.root / MANIFEST
        start = len(self.findings)
        try:
            with self.stable(path):
                status, uploads = self.database(MANIFEST)
                self.attachments(references, status, uploads)
                if (status == "not_initialized" or uploads is not None) and self.has_sidecars(path):
                    raise SQLiteSidecarsPresent
        except (OSError, RuntimeError):
            del self.findings[start:]
            self.unavailable(MANIFEST)

    def attachments(
        self, references: set[str], status: str, uploads: dict[str, AttachmentFingerprint] | None,
    ) -> None:
        """Check known upload references, stored sizes, and optional content digests."""
        path = self.root / MANIFEST
        if status == "not_initialized":
            try:
                with self.stable(path.parent, directory=True) as info:
                    blobs = info is not None and any(entry.suffix == ".blob" for entry in path.parent.iterdir())
            except (OSError, RuntimeError):
                self.unavailable("data/attachments")
                return
            if blobs or references:
                self.add(MANIFEST, "missing_reference", "attachment_manifest_missing",
                         "Saved upload references or copies exist without an initialized manifest.")
        if uploads is None:
            return
        for identity in sorted(references - uploads.keys()):
            self.add(f"data/attachments/{identity}.blob", "missing_reference", "upload_reference_missing",
                     "A conversation refers to an upload without an active manifest entry.")
        verified = sum(self.attachment_copy(identity, fingerprint) for identity, fingerprint in sorted(uploads.items()))
        if self.deep:
            if uploads:
                self.add("data/attachments", "ok" if verified == len(uploads) else "info", "attachment_hashes_checked",
                         f"Verified SHA-256 hashes for {verified} of {len(uploads)} active attachment copies.")
            self.untracked_copies(references | uploads.keys())

    def attachment_copy(self, identity: str, fingerprint: AttachmentFingerprint) -> bool:
        """Compare a stable copy with the manifest and report only the type of failure."""
        location = f"data/attachments/{identity}.blob"
        path = self.root / location
        digest = None
        try:
            with self.stable(path) as info:
                if self.deep and info is not None and info[2] == fingerprint.size_bytes:
                    with path.open("rb") as source:
                        digest = hashlib.file_digest(source, "sha256").hexdigest()
        except (OSError, RuntimeError):
            self.unavailable(location)
            return False
        if info is None:
            self.add(location, "missing_reference", "upload_copy_missing",
                     "An active attachment entry refers to a missing copy.")
        elif info[2] != fingerprint.size_bytes:
            self.add(location, "invalid_data", "upload_size_mismatch",
                     "The attachment size differs from the manifest.")
        elif self.deep and digest != fingerprint.sha256:
            self.add(location, "invalid_data", "upload_hash_mismatch",
                     "The attachment bytes differ from the manifest's recorded hash.")
        else:
            return True
        return False

    def untracked_copies(self, known: set[str]) -> None:
        """Flag canonical copies without a live manifest row or an existing reference finding."""
        name = "data/attachments"
        directory = self.root / name
        try:
            with self.stable(directory, directory=True):
                paths = sorted(path for path in directory.iterdir()
                               if path.suffix == ".blob" and is_upload_id(path.stem) and path.stem not in known)
        except (OSError, RuntimeError):
            self.unavailable(name)
            return
        for path in paths:
            location = path.relative_to(self.root).as_posix()
            try:
                if self.stamp(path) is None:
                    raise OSError("Attachment copy disappeared during the check.")
            except (OSError, RuntimeError):
                self.unavailable(location)
                continue
            self.add(location, "review", "upload_copy_untracked",
                     "This copy has no active manifest entry. Preserve it until it can be reviewed.")


def inspect_storage(root: Path, *, deep: bool = False) -> list[Finding]:
    """Inspect independent stores even when one registry prevents app startup."""
    scan = StorageScan(root, deep=deep)
    try:
        scan.root = root.resolve()
        if scan.stamp(scan.root, directory=True) is None:
            raise OSError("Selected data directory is absent.")
    except (OSError, RuntimeError):
        scan.add(".", "unavailable", "storage_root_unavailable",
                 "Check the selected data directory and its read permissions.")
        return scan.findings
    settings = scan.json_registry("settings.json", SettingsService, SETTINGS_SCHEMA_VERSION)
    app_settings = (settings.migration_origin or settings.current) if settings is not None else None
    profiles = scan.json_registry(
        "profiles.json", lambda path: ProfileService(path, app_settings=app_settings), SCHEMA_VERSION,
    )
    profile_ids = None
    if profiles is not None:
        profile_ids = {item["id"] for item in profiles.list_profiles()}
    elif scan.findings[-1].code == "registry_absent":
        profile_ids = set()
    references = scan.conversations(profile_ids)
    for name in SQLITE_TABLES:
        if name == MANIFEST:
            scan.attachment_store(references)
        else:
            scan.database(name)
    return scan.findings


def main(argv: list[str] | None = None) -> int:
    """Print a human-readable or JSON report with a diagnostic exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="Helios directory or offline data copy")
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    parser.add_argument("--deep", action="store_true", help="verify attachment hashes and run full SQLite integrity checks")
    args = parser.parse_args(argv)
    findings = inspect_storage(args.root, deep=args.deep)
    complete = not any(item.status == "unavailable" for item in findings)
    problems = any(item.status in PROBLEMS for item in findings)
    exit_code = 1 if problems else 0 if complete else 2
    if args.json:
        print(json.dumps({"complete": complete, "exit_code": exit_code, "findings": [asdict(item) for item in findings]}))
    else:
        for item in findings:
            print(f"[{item.status}] {json.dumps(item.path)} - {item.message}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
