"""Plan attachment recovery against an offline manifest and publish a verified storage copy.

The source store and the backup are read-only inputs to both operations.
"""

import argparse
import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, BinaryIO, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from config import PROJECT_ROOT
from services.project_access import (
    RECOVERY_COPY_PREFIX, RECOVERY_STAGE_PREFIX, ProjectReadError, final_handle_path, is_link, signature,
)
from services.storage_diagnostics import (
    PROBLEMS, SQLITE_TABLES, AttachmentFingerprint, StorageScan, inspect_storage, is_upload_id,
)
from services.storage_references import ATTACHMENTS, CONVERSATIONS, MANIFEST, Finding


Stamp = tuple[int, int, int, int, int]
READ_ERRORS = (OSError, RuntimeError, ProjectReadError)
ATTACHMENT_PROBLEMS = {
    "upload_copy_missing": "The stored copy is missing.",
    "upload_size_mismatch": "The stored copy has the wrong size.",
    "upload_hash_mismatch": "The stored copy has the wrong SHA-256 hash.",
}
MAX_PREVIEW_ATTACHMENTS = 50
PreviewCount = Annotated[int, Field(ge=0)]


class RecoveryError(RuntimeError):
    """Expose a safe recovery failure without including paths or private values."""

    def __init__(self, code: str, message: str) -> None:
        """Pair a stable failure code with guidance for the operator."""
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class AttachmentRecovery:
    """Retain the identity and validated inputs of one proposed blob repair."""

    identity: str
    problem: str
    backup_status: str
    fingerprint: AttachmentFingerprint
    original_stamp: Stamp | None
    backup_stamp: Stamp | None = None

    def __post_init__(self) -> None:
        """Keep staging destinations bound to app-issued attachment identities."""
        if not is_upload_id(self.identity):
            raise ValueError("Invalid attachment identity.")

    @property
    def path(self) -> str:
        """Resolve the managed blob location from its identity."""
        return f"{ATTACHMENTS}/{self.identity}.blob"

    def report(self) -> dict:
        """Describe the affected entry without disclosing names, bytes, or hashes."""
        return {"path": self.path, "problem": self.problem, "backup": self.backup_status,
                "size_bytes": self.fingerprint.size_bytes}


@dataclass(frozen=True)
class RecoveryPlan:
    """Bind restore candidates to the manifest and the offline directories examined."""

    root: Path
    backup: Path
    manifest_stamp: Stamp
    attachments: tuple[AttachmentRecovery, ...]
    confirmation: str = ""

    @property
    def ready(self) -> tuple[AttachmentRecovery, ...]:
        """Select repairs whose backups matched the manifest."""
        return tuple(item for item in self.attachments if item.backup_status == "verified")


class RecoveryPreviewAttachment(BaseModel):
    """Expose only bounded, root-relative attachment metadata in saved previews."""

    model_config = ConfigDict(strict=True)
    path: str = Field(pattern=rf"^{ATTACHMENTS}/[0-9a-f-]{{36}}\.blob$")
    problem: Literal["upload_copy_missing", "upload_size_mismatch", "upload_hash_mismatch"]
    backup: Literal["verified", "missing", "mismatch", "unavailable"]
    size_bytes: PreviewCount


class RecoveryPreviewReport(BaseModel):
    """Keep the overall counts and the first attachment results for Settings."""

    model_config = ConfigDict(strict=True)
    verified: PreviewCount
    unresolved: PreviewCount
    attachments: list[RecoveryPreviewAttachment] = Field(max_length=MAX_PREVIEW_ATTACHMENTS)
    omitted_attachments: PreviewCount


def recovery_preview_report(plan: RecoveryPlan) -> dict:
    """Project a recovery plan without storing source or backup paths or hashes."""
    total = len(plan.attachments)
    verified = len(plan.ready)
    return {
        "verified": verified,
        "unresolved": total - verified,
        "attachments": [item.report() for item in plan.attachments[:MAX_PREVIEW_ATTACHMENTS]],
        "omitted_attachments": max(0, total - MAX_PREVIEW_ATTACHMENTS),
    }


def resolve_recovery_backup(root: Path, path: str) -> Path:
    """Resolve a user-selected backup folder distinct from the active store."""
    try:
        backup = Path(path)
        if "\x00" in path or not backup.is_absolute():
            raise ValueError
        backup = backup.resolve(strict=True)
        if not backup.is_dir() or backup == root.resolve():
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise RecoveryError("invalid_backup_dir", "Choose an existing backup folder separate from the active storage folder.") from None
    return backup


def resolve_recovery_output(root: Path, backup: Path, path: Path) -> tuple[Path, tuple[int, int]]:
    """Resolve an existing output directory outside both input trees and pin its identity."""
    try:
        parent = path.resolve(strict=True)
        stamp = _stamp(parent, directory=True)
        if (stamp is None or parent.is_relative_to(root) or parent.is_relative_to(backup)):
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise RecoveryError("invalid_output_dir", "Choose an existing output directory outside the source and backup trees.") from None
    return parent, stamp[:2]


def recovery_copy_name(identity: UUID) -> str:
    """Use a stable folder name for one approved preview and output directory."""
    return f"{RECOVERY_COPY_PREFIX}{identity.hex}"


def _stamp(path: Path, *, directory: bool = False) -> Stamp | None:
    """Identify an ordinary entry on its declared path without following storage links."""
    if path.resolve() != path:
        raise OSError("Storage path redirected.")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if is_link(info) or not expected(info.st_mode):
        raise OSError("Unexpected storage entry type.")
    return signature(info)


def _database_stamp(path: Path) -> Stamp | None:
    """Require a database file whose on-disk bytes include all committed state."""
    for suffix in ("-journal", "-wal", "-shm"):
        if _stamp(path.with_name(path.name + suffix)) is not None:
            raise RecoveryError("sqlite_sidecars_present", "SQLite sidecars are present. Close Helios and use a consistent offline copy.")
    return _stamp(path)


def _transfer(
    path: Path, before: Stamp, *, fingerprint: AttachmentFingerprint | None = None, output: BinaryIO | None = None,
) -> bool:
    """Stream stable bytes with optional checksum verification and copying in the same pass."""
    if _stamp(path) != before:
        raise OSError("Storage input changed.")
    size = before[2]
    if fingerprint is not None and size != fingerprint.size_bytes:
        return False
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        if (final_handle_path(source.fileno(), path) != path
                or signature(os.fstat(source.fileno())) != before):
            raise OSError("Opened storage input changed.")
        remaining = size
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError("Storage input shrank.")
            remaining -= len(chunk)
            if fingerprint is not None:
                digest.update(chunk)
            if output is not None:
                output.write(chunk)
        if source.read(1) or signature(os.fstat(source.fileno())) != before:
            raise OSError("Storage input changed during the read.")
    if _stamp(path) != before:
        raise OSError("Storage input changed during the read.")
    return fingerprint is None or digest.hexdigest() == fingerprint.sha256


def _attachment_candidate(
    root: Path, backup: Path, identity: str, fingerprint: AttachmentFingerprint,
) -> AttachmentRecovery | None:
    """Look for a replacement only when the active copy is missing or damaged."""
    location = f"{ATTACHMENTS}/{identity}.blob"
    original = root / location
    original_stamp = _stamp(original)
    if original_stamp is None:
        problem = "upload_copy_missing"
    elif original_stamp[2] != fingerprint.size_bytes:
        problem = "upload_size_mismatch"
    elif _transfer(original, original_stamp, fingerprint=fingerprint):
        return None
    else:
        problem = "upload_hash_mismatch"
    candidate = backup / location
    backup_stamp = None
    try:
        backup_stamp = _stamp(candidate)
        if backup_stamp is None:
            status = "missing"
        else:
            status = "verified" if _transfer(candidate, backup_stamp, fingerprint=fingerprint) else "mismatch"
    except READ_ERRORS:
        status = "unavailable"
    return AttachmentRecovery(identity, problem, status, fingerprint, original_stamp, backup_stamp)


def plan_attachment_recovery(root: Path, backup: Path) -> RecoveryPlan:
    """Preview the damaged active copies and verify candidate bytes without writing files."""
    try:
        root, backup = root.resolve(strict=True), backup.resolve(strict=True)
        backup_stamp = _stamp(backup, directory=True)
        if _stamp(root, directory=True) is None or backup_stamp is None:
            raise OSError("Recovery input directory is missing.")
        path = root / MANIFEST
        before = _database_stamp(path)
        status, uploads = StorageScan(root, deep=True).database(MANIFEST)
        if before is None or uploads is None:
            raise RecoveryError("manifest_unavailable", f"The source attachment manifest is {status.replace('_', ' ')}. Use a valid offline manifest.")
        ordered_uploads = sorted(uploads.items())
        attachments = tuple(item for identity, fingerprint in ordered_uploads
                            if (item := _attachment_candidate(root, backup, identity, fingerprint)) is not None)
        if _database_stamp(path) != before:
            raise RecoveryError("source_changed", "The manifest changed during planning. Close Helios and preview recovery again.")
        latest_backup = _stamp(backup, directory=True)
        if latest_backup is None or latest_backup[:2] != backup_stamp[:2]:
            raise RecoveryError("backup_changed", "The backup folder changed. Preview recovery again.")
    except RecoveryError:
        raise
    except READ_ERRORS:
        raise RecoveryError("read_unavailable", "Recovery could not read stable source files. Check the offline paths and try again.") from None
    # Pin active attachment data rather than unrelated draft lease changes in SQLite.
    digest = hashlib.sha256(json.dumps([os.path.normcase(root), os.path.normcase(backup),
                                       before[:2], backup_stamp[:2]]).encode())
    for identity, fingerprint in ordered_uploads:
        digest.update(json.dumps([identity, fingerprint.size_bytes, fingerprint.sha256]).encode())
    for item in attachments:
        digest.update(json.dumps(asdict(item), sort_keys=True).encode())
    return RecoveryPlan(root, backup, before, attachments, digest.hexdigest())


def _storage_snapshot(root: Path) -> dict[str, Stamp]:
    """List the durable stores to copy and defer any SQLite snapshot with sidecars."""
    files = {}
    for name in SQLITE_TABLES:
        if (stamp := _database_stamp(root / name)) is not None:
            files[name] = stamp
    paths = [root / "profiles.json", root / "settings.json"]
    paths.extend(root.glob("profiles.json.v*.bak"))
    paths.extend(root.glob("settings.json.v*.bak"))
    conversations = root / CONVERSATIONS
    if _stamp(conversations, directory=True) is not None:
        paths.extend(path for path in conversations.iterdir() if path.suffix == ".json")
    originals = root / ATTACHMENTS
    if _stamp(originals, directory=True) is not None:
        paths.extend(path for path in originals.iterdir() if path.suffix == ".blob" and is_upload_id(path.stem))
    for path in paths:
        if (stamp := _stamp(path)) is not None:
            files[path.relative_to(root).as_posix()] = stamp
    return files


def _write_copy(
    source: Path, stamp: Stamp, target: Path, *, fingerprint: AttachmentFingerprint | None = None,
) -> None:
    """Write a private staging file and flush its verified bytes before publication."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(target, flags, 0o600), "wb") as output:
        if not _transfer(source, stamp, fingerprint=fingerprint, output=output):
            raise RecoveryError("backup_mismatch", "A backup no longer matches the recorded attachment. Preview recovery again.")
        output.flush()
        os.fsync(output.fileno())


def _verification(stage: Path, repaired: set[str]) -> list[Finding]:
    """Require a complete deep scan and confirm every proposed repair before publishing."""
    findings = inspect_storage(stage, deep=True)
    if any(item.status == "unavailable" for item in findings):
        raise RecoveryError("verification_incomplete", "The restored copy could not be fully inspected. Check the source and try again.")
    if (not any(item.path == MANIFEST and item.status in {"ok", "compatible_legacy"} for item in findings)
            or any(item.path in repaired and item.status in PROBLEMS | {"review"} for item in findings)):
        raise RecoveryError("verification_failed", "Deep diagnostics could not confirm every restored attachment.")
    return findings


def restore_attachment_copy(
    plan: RecoveryPlan, output_dir: Path, *,
    preview_id: UUID | None = None, output_identity: tuple[int, int] | None = None,
) -> tuple[Path, list[Finding]]:
    """Copy the offline store, apply verified repairs, and publish a new directory."""
    ready = plan.ready
    if not ready:
        raise RecoveryError("no_verified_backups", "The plan has no verified attachment backups to restore.")
    try:
        parent, parent_identity = resolve_recovery_output(plan.root, plan.backup, output_dir)
        if output_identity is not None and (parent != output_dir or parent_identity != output_identity):
            raise RecoveryError("output_changed", "The output location changed during recovery. Choose another directory.")
        destination = parent / recovery_copy_name(preview_id or uuid4())
        if destination.resolve() != destination or destination.exists():
            raise RecoveryError("output_changed", "The recovery folder already exists. Choose another output directory.")
        files = _storage_snapshot(plan.root)
        if (files.get(MANIFEST) != plan.manifest_stamp
                or any(files.get(item.path) != item.original_stamp for item in ready)):
            raise RecoveryError("source_changed", "The manifest or an affected attachment changed. Preview recovery again.")
        repaired = {item.path for item in ready}
        with TemporaryDirectory(prefix=RECOVERY_STAGE_PREFIX, dir=parent, ignore_cleanup_errors=True) as temporary:
            stage = Path(temporary) / "storage"
            current_parent = _stamp(parent, directory=True)
            if current_parent is None or current_parent[:2] != parent_identity or stage.resolve() != stage:
                raise RecoveryError("output_changed", "The output location changed during recovery. Choose another directory.")
            stage.mkdir(mode=0o700)
            for name, stamp in sorted(files.items()):
                if name not in repaired:
                    _write_copy(plan.root / name, stamp, stage / name)
            for item in ready:
                if item.backup_stamp is None or _stamp(plan.backup / item.path) != item.backup_stamp:
                    raise RecoveryError("backup_changed", "A verified backup changed. Preview recovery again.")
                _write_copy(plan.backup / item.path, item.backup_stamp, stage / item.path, fingerprint=item.fingerprint)
            findings = _verification(stage, repaired)
            if _storage_snapshot(plan.root) != files:
                raise RecoveryError("source_changed", "The source changed during recovery. Close Helios and try again.")
            current_parent = _stamp(parent, directory=True)
            if (current_parent is None or current_parent[:2] != parent_identity
                    or stage.resolve() != stage or destination.resolve() != destination or destination.exists()):
                raise RecoveryError("output_changed", "The output location changed during recovery. Choose another directory.")
            stage.rename(destination)
    except RecoveryError:
        raise
    except READ_ERRORS:
        raise RecoveryError("copy_unavailable", "Recovery could not read stable inputs or publish the new copy. Check the offline paths and available space.") from None
    return destination, findings


def main(argv: list[str] | None = None) -> int:
    """Preview recovery by default or explicitly publish a copy under the selected directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="offline Helios data directory")
    parser.add_argument("--backup", type=Path, required=True, help="backup directory with the same attachment layout")
    parser.add_argument("--output-dir", type=Path, help="apply into a new folder under this existing directory")
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    args = parser.parse_args(argv)
    report = {"status": "preview", "attachments": []}
    try:
        plan = plan_attachment_recovery(args.root, args.backup)
        report["attachments"] = [item.report() for item in plan.attachments]
        exit_code = int(len(plan.ready) != len(plan.attachments))
        if args.output_dir is not None and plan.ready:
            destination, findings = restore_attachment_copy(plan, args.output_dir)
            report.update(status="restored", output=str(destination), findings=[asdict(item) for item in findings])
            exit_code = int(any(item.status in PROBLEMS for item in findings))
        elif args.output_dir is not None:
            report["status"] = "nothing_to_restore"
    except RecoveryError as error:
        report.update(status="blocked", error={"code": error.code, "message": str(error)})
        exit_code = 2
    if args.json:
        print(json.dumps({**report, "exit_code": exit_code}))
    else:
        print(f"Attachment recovery: {report['status']}")
        for item in report["attachments"]:
            print(f"[backup {item['backup']}] {json.dumps(item['path'])} - {ATTACHMENT_PROBLEMS[item['problem']]} "
                  f"Expected size: {item['size_bytes']} bytes.")
        if "output" in report:
            print(f"Verified copy: {json.dumps(report['output'])}")
            for item in report["findings"]:
                print(f"[{item['status']}] {json.dumps(item['path'])} - {item['message']}")
        if "error" in report:
            print(report["error"]["message"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
