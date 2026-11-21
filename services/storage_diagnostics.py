"""Inspect Helios data without starting the app or mutating its stores."""

import argparse
import hashlib
import json
import sqlite3
import stat
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import UUID

from config import PROJECT_ROOT
from models.attachment import is_upload_id
from models.conversation import Conversation
from services.attachment_store import AttachmentStore
from services.json_storage import SCHEMA_VERSION, JsonStorageError, decode_storage_json
from services.local_work import check_local_cancelled, local_work_cancelled
from services.profile_service import ProfileService
from services.settings_service import SETTINGS_SCHEMA_VERSION, SettingsService
from services.storage_references import (
    ATTACHMENTS, CALL_USAGE, CONVERSATIONS, MANIFEST, REFERENCE_READERS, SUMMARY_USAGE, WORK_RUNS,
    Finding, StorageReferences, Transcript,
)
from services.work_run_store import WORK_RUN_SCHEMA_VERSION


PROBLEMS = {"invalid_data", "unsupported_version", "missing_reference"}
ATTACHMENT_RECOVERY_CODES = {"upload_copy_missing", "upload_size_mismatch", "upload_hash_mismatch"}
MAX_ATTENTION_FINDINGS = 50
SQLITE_TABLES = {
    WORK_RUNS: {"work_metadata", "work_runs", "model_steps", "tool_steps"},
    MANIFEST: {"uploads", "store_metadata"},
    CALL_USAGE: {"usage_calls"},
    SUMMARY_USAGE: {"summary_usage"},
}


class SQLiteSidecarsPresent(OSError):
    """Defer a database check while journal or WAL files may affect its contents."""


class SQLiteLiveWalMode(OSError):
    """Defer live WAL readers that could create SQLite coordination files."""


@dataclass(frozen=True)
class AttachmentFingerprint:
    """Record the size and digest of an active attachment without its original name."""

    size_bytes: int
    sha256: str


class StorageScan:
    """Collect content-free findings from a data directory that is at rest."""

    def __init__(self, root: Path, *, deep: bool = False, live: bool = False) -> None:
        """Bind the scan to one storage root."""
        self.root = root
        self.deep = deep
        self.live = live
        self.findings: list[Finding] = []
        self.references = StorageReferences() if deep else None
        self.reference_stamps: dict[Path, tuple | None] = {}

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
        message = "The check could not read a stable local entry."
        message += " Try again when Helios is idle." if self.live else " Close Helios and try again."
        self.add(name, "unavailable", "read_unavailable", message)

    def json_registry(self, name: str, loader, current_version: str, *, path: Path | None = None):
        """Reuse the app's read-only registry validation and check its backup claim."""
        path = path if path is not None else self.root / name
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
            self.backup(name, document.migration, path=path)
        return service

    def backup(self, name: str, migration: dict, *, path: Path) -> None:
        """Check that the claimed migration source still has its recorded bytes."""
        path = path.with_name(migration["backup_file"])
        location = Path(name).with_name(path.name).as_posix()
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

    def conversations(self, profiles: set[str] | None, *, path: Path | None = None) -> set[str]:
        """Check transcript structure and collect references from readable conversations."""
        name = CONVERSATIONS
        path = path if path is not None else self.root / name
        references = set()
        try:
            with self.stable(path, directory=True) as info:
                paths = sorted(entry for entry in path.iterdir() if entry.suffix == ".json") if info else []
        except (OSError, RuntimeError):
            self.unavailable(name)
            return references
        if self.references is not None:
            self.references.conversation_files = {path.name for path in paths}
            self.reference_stamps[path] = info
        if not paths:
            self.add(name, "not_initialized", "conversations_absent", "No saved conversations were found.")
            return references
        readable = 0
        first_finding = len(self.findings)
        for path in paths:
            check_local_cancelled()
            location = f"{name}/{path.name}"
            try:
                with self.stable(path) as info:
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
                    if self.references is not None:
                        transcript = Transcript.from_conversation(location, conversation)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
                self.add(location, "invalid_data", "invalid_conversation", "The saved conversation is invalid.")
                continue
            except (OSError, RuntimeError):
                self.unavailable(location)
                continue
            if self.references is not None:
                self.reference_stamps[path] = info
                self.references.conversations[conversation.id] = transcript
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
        status = "ok" if len(self.findings) == first_finding else "info"
        self.add(name, status, "conversations_read", f"Read {readable} of {len(paths)} conversation files.")
        return references

    def has_sidecars(self, path: Path) -> bool:
        """Skip SQLite snapshots that could depend on journal or WAL state."""
        return any(self.stamp(path.with_name(path.name + suffix)) is not None
                   for suffix in ("-journal", "-wal", "-shm"))

    @contextmanager
    def database_reader(self, path: Path):
        """Use SQLite read locks for live reads and immutable mode for offline snapshots."""
        uri = path.resolve().as_uri() + "?mode=ro"
        if self.live:
            with path.open("rb") as source:
                header = source.read(20)
            # WAL readers can create sidecars even when the database is opened read-only.
            if header.startswith(b"SQLite format 3\0") and 2 in header[18:20]:
                raise SQLiteLiveWalMode
        else:
            uri += "&immutable=1"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.05 if self.live else 5.0)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            if self.live:
                db.execute("BEGIN")
                db.set_progress_handler(local_work_cancelled, 1024)
            yield db

    def database(self, name: str, *, path: Path | None = None) -> tuple[str, dict[str, AttachmentFingerprint] | None]:
        """Run physical and schema checks without opening a writable SQLite connection."""
        path = path if path is not None else self.root / name
        reader = REFERENCE_READERS.get(name) if self.references is not None else None
        projected = None
        try:
            with self.stable(path) as info:
                if self.has_sidecars(path):
                    raise SQLiteSidecarsPresent
                try:
                    if info is None:
                        result = ("not_initialized", None)
                    else:
                        with self.database_reader(path) as db:
                            result = self.database_contents(name, db)
                            if reader is not None and result[0] in {"ok", "compatible_legacy"}:
                                projected = reader(db)
                finally:
                    if self.has_sidecars(path):
                        raise SQLiteSidecarsPresent
        except SQLiteSidecarsPresent:
            message = "Journal or WAL files are present. Close Helios or inspect a consistent offline copy."
            if self.live:
                message = "Journal or WAL files are present. Try again when Helios is idle."
            self.add(name, "unavailable", "sqlite_sidecars_present",
                     message)
            return "unavailable", None
        except SQLiteLiveWalMode:
            self.add(name, "unavailable", "sqlite_wal_requires_offline_check",
                     "This database uses WAL mode and needs an offline check.")
            return "unavailable", None
        except (OSError, RuntimeError):
            self.unavailable(name)
            return "unavailable", None
        except sqlite3.Error as error:
            check_local_cancelled()
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
        if reader is not None and status in {"ok", "compatible_legacy", "not_initialized"}:
            self.references.add_database(projected if projected is not None else reader(None))
            self.reference_stamps[path] = info
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
        elif name == WORK_RUNS and "work_metadata" in tables:
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
        if name == WORK_RUNS:
            if ((version in {"0.2", current} and "work_execution_segments" not in tables)
                    or (version == current and not {"work_segment_contexts", "work_resume_contexts"} <= tables)):
                return "invalid_data", None
            db.execute("SELECT id, conversation_id, status, data FROM work_runs LIMIT 0")
        elif name == CALL_USAGE:
            db.execute("SELECT id, conversation_id, kind, status, started_at, source, data FROM usage_calls LIMIT 0")
        elif name == SUMMARY_USAGE:
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
                check_local_cancelled()
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
            with self.stable(path) as info:
                status, uploads = self.database(MANIFEST)
                self.attachments(references, status, uploads)
                if (status == "not_initialized" or uploads is not None) and self.has_sidecars(path):
                    raise SQLiteSidecarsPresent
        except (OSError, RuntimeError):
            del self.findings[start:]
            self.unavailable(MANIFEST)
            return
        if self.references is not None and (status == "not_initialized" or uploads is not None):
            self.references.uploads = set(uploads or ())
            self.references.manifest_initialized = uploads is not None
            self.reference_stamps[path] = info

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
                self.unavailable(ATTACHMENTS)
                return
            if blobs or (self.references is None and references):
                self.add(MANIFEST, "missing_reference", "attachment_manifest_missing",
                         "Saved upload references or copies exist without an initialized manifest.")
        if uploads is None:
            return
        if self.references is None:
            for identity in sorted(references - uploads.keys()):
                self.add(f"{ATTACHMENTS}/{identity}.blob", "missing_reference", "upload_reference_missing",
                         "A conversation refers to an upload without an active manifest entry.")
        verified = sum(self.attachment_copy(identity, fingerprint) for identity, fingerprint in sorted(uploads.items()))
        if self.references is not None:
            if uploads:
                self.add(ATTACHMENTS, "ok" if verified == len(uploads) else "info", "attachment_hashes_checked",
                         f"Verified SHA-256 hashes for {verified} of {len(uploads)} active attachment copies.")
            work_uploads = self.references.work.uploads if self.references.work is not None else set()
            self.untracked_copies(references | uploads.keys() | work_uploads)

    def attachment_copy(self, identity: str, fingerprint: AttachmentFingerprint) -> bool:
        """Compare a stable copy with the manifest and report only the type of failure."""
        location = f"{ATTACHMENTS}/{identity}.blob"
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
        name = ATTACHMENTS
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

    def compare_references(self, transcript_uploads: set[str]) -> None:
        """Publish cross-store findings only while participating snapshots remain stable."""
        if self.references is None:
            return
        findings = self.references.findings(transcript_uploads)
        try:
            for path, before in self.reference_stamps.items():
                if (self.stamp(path, directory=path == self.root / CONVERSATIONS) != before
                        or path.suffix == ".sqlite3" and self.has_sidecars(path)):
                    raise OSError("A reference source changed during the check.")
        except (OSError, RuntimeError):
            self.add(".", "unavailable", "reference_snapshot_changed",
                     "A source changed during the reference check. Close Helios and try again.")
            return
        if any(item.path.startswith(f"{CONVERSATIONS}/") for item in findings):
            for index, item in enumerate(self.findings):
                if item.code == "conversations_read" and item.status == "ok":
                    self.findings[index] = replace(item, status="info")
                    break
        existing = set(self.findings)
        self.findings.extend(item for item in findings if item not in existing)


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
    scan.compare_references(references)
    return scan.findings


def storage_report(findings: list[Finding], *, scope: str) -> dict:
    """Summarize basic or deep findings for Settings while bounding attention details."""
    problems = [item for item in findings if item.status in PROBLEMS]
    review = [item for item in findings if item.status == "review"]
    deferred = [item for item in findings if item.status == "unavailable"]
    attention = problems + deferred + review
    routine = [item for item in findings if item.status not in PROBLEMS | {"review", "unavailable"}]
    return {
        "scope": scope,
        "complete": not deferred,
        "has_attachment_recovery_candidates": any(item.code in ATTACHMENT_RECOVERY_CODES for item in problems),
        "summary": {"problems": len(problems), "review": len(review), "deferred": len(deferred)},
        "findings": [asdict(item) for item in attention[:MAX_ATTENTION_FINDINGS] + routine],
        "omitted_findings": max(0, len(attention) - MAX_ATTENTION_FINDINGS),
    }


def main(argv: list[str] | None = None) -> int:
    """Print a human-readable or JSON report with a diagnostic exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="Helios directory or offline data copy")
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    parser.add_argument("--deep", action="store_true", help="check attachment hashes, SQLite indexes, and saved references")
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
