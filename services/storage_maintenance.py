"""Coordinate storage ownership and one offline job across server restarts."""

import hashlib
import json
import logging
import os
import stat
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ValidationError

from services.cache_staging import cache_write_lock, staged_cache_write
from services.project_access import is_link, signature
from services.storage_diagnostics import ATTACHMENT_RECOVERY_CODES
from services.storage_recovery import (
    RecoveryError, RecoveryPreviewReport, recovery_copy_name, resolve_recovery_backup, resolve_recovery_output,
)

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class StorageInUse(RuntimeError):
    """Report a store already owned by a cooperating Helios process."""


class MaintenanceBusy(RuntimeError):
    """Report that a worker has already accepted an offline operation."""


RECOVERY_PREVIEW = "recovery-preview"
RECOVERY_APPLY = "recovery-apply"
CheckScope = Literal["basic", "deep"]
RecoveryOperation = Literal["recovery-preview", "recovery-apply"]
REPORT_CACHE_NAMES = {
    "basic": "last-basic-check.json", "deep": "last-deep-check.json",
    RECOVERY_PREVIEW: "last-recovery-preview.json", RECOVERY_APPLY: "last-recovery-copy.json",
}
logger = logging.getLogger(__name__)


def ownership_path(root: Path) -> Path:
    """Locate the shared, persistent ownership lock for one store."""
    return root / ".cache" / "storage-owner.lock"


@contextmanager
def own_storage(root: Path):
    """Hold exclusive ownership through startup, serving, or offline inspection."""
    path = ownership_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "r+b", buffering=0) as handle:
        try:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise StorageInUse("Another Helios process owns this storage directory.") from None
        yield


def write_job(path: Path, job: dict) -> None:
    """Publish job state atomically before either process acts on it."""
    if path.name in REPORT_CACHE_NAMES.values() and path.parent.parts[-2:] == (".cache", "storage-maintenance"):
        with staged_cache_write(path, encoding="utf-8") as target:
            json.dump(job, target, ensure_ascii=True, allow_nan=False)
        return
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as target:
            temporary = Path(target.name)
            json.dump(job, target, ensure_ascii=True, allow_nan=False)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_job(path: Path, *, strict_io: bool = False) -> dict | None:
    """Return an intact job envelope and treat missing or damaged state as absent."""
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
        identity = job["id"]
        if isinstance(identity, str) and str(UUID(identity)) == identity:
            return job
    except OSError:
        if strict_io:
            raise
    except (ValueError, KeyError, TypeError, RecursionError):
        pass
    return None


def result_path(root: Path, *, scope: CheckScope | RecoveryOperation = "deep") -> Path:
    """Locate the latest report of the selected maintenance operation."""
    return root / ".cache" / "storage-maintenance" / REPORT_CACHE_NAMES[scope]


def recovery_record_dir(root: Path) -> Path:
    """Locate durable records of published recovery copies."""
    return root / "logs" / "storage-recovery"


def _valid_report(report: object, scope: CheckScope) -> bool:
    """Check the saved report fields the Settings view needs to render."""
    if not isinstance(report, dict) or report.get("scope") != scope or type(report.get("complete")) is not bool:
        return False
    if type(report.get("has_attachment_recovery_candidates", False)) is not bool:
        return False
    summary = report.get("summary")
    findings = report.get("findings")
    omitted = report.get("omitted_findings")
    if (not isinstance(summary, dict) or not isinstance(findings, list)
            or type(omitted) is not int or omitted < 0):
        return False
    counts_valid = all(type(summary.get(key)) is int and summary[key] >= 0
                       for key in ("problems", "review", "deferred"))
    rows_valid = all(
        isinstance(item, dict) and all(isinstance(item.get(key), str)
                                       for key in ("path", "status", "code", "message"))
        for item in findings
    )
    return counts_valid and rows_valid


def _finished_outcome(job: dict | None) -> dict | None:
    """Return the public identity, state, and timestamp of a saved result."""
    if not job:
        return None
    try:
        finished = datetime.fromisoformat(job.get("finished_at"))
        if finished.tzinfo is None:
            return None
        timestamp = finished.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None
    return {"id": job["id"], "status": job.get("status"), "finished_at": timestamp}


def check_result(job: dict | None, *, scope: CheckScope) -> dict | None:
    """Return a valid saved check outcome for Settings."""
    result = _finished_outcome(job)
    if job is None or result is None:
        return None
    status = result["status"]
    if status == "completed" and _valid_report(job.get("report"), scope):
        return {**result, "report": job["report"]}
    if scope == "deep" and status == "failed" and isinstance(job.get("message"), str) and job["message"].strip():
        return {**result, "message": job["message"]}
    return None


def load_check_result(path: Path, *, scope: CheckScope, strict_io: bool = False) -> dict | None:
    """Read the saved result and treat a missing or malformed file as no history."""
    return check_result(read_job(path, strict_io=strict_io), scope=scope)


def _check_result_signature(path: Path) -> tuple | None:
    """Identify a regular, unlinked result file in its expected location."""
    try:
        if path.resolve() != path:
            raise OSError("Saved check result is redirected.")
        info = path.lstat()
    except FileNotFoundError:
        return None
    if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("Saved check result is not an ordinary file.")
    return signature(info)


def remove_check_result(root: Path, scope: CheckScope, result_id: UUID) -> bool:
    """Remove a reviewed check result, returning false if a different result occupies its slot."""
    path = result_path(root.absolute(), scope=scope)
    if _check_result_signature(path) is None:
        return True
    with cache_write_lock(path.parent):
        before = _check_result_signature(path)
        if before is None:
            return True
        result = load_check_result(path, scope=scope)
        after = _check_result_signature(path)
        if after is None:
            return True
        if before != after or result is None or result["id"] != str(result_id):
            return False
        path.unlink(missing_ok=True)
    return True


def load_recovery_result(root: Path, operation: RecoveryOperation) -> dict | None:
    """Load the latest saved recovery result for one operation."""
    return _recovery_result(read_job(result_path(root, scope=operation)), operation)


def _recovery_result(job: dict | None, operation: RecoveryOperation) -> dict | None:
    """Return a validated public outcome without exposing private job parameters."""
    result = _finished_outcome(job)
    if job is None or result is None or job.get("operation") != operation:
        return None
    if result["status"] == "completed":
        if operation == RECOVERY_PREVIEW:
            try:
                report = RecoveryPreviewReport.model_validate(job.get("report")).model_dump()
            except ValidationError:
                return None
            return {**result, "report": report, "can_apply": bool(report["verified"] and _recovery_confirmation(job))}
        report = _recovery_copy_report(job.get("report"))
        return {**result, "report": report} if report is not None else None
    if result["status"] == "failed":
        code, message = job.get("code"), job.get("message")
        if isinstance(code, str) and code and isinstance(message, str) and message.strip():
            return {**result, "code": code, "message": message}
    return None


def _recovery_copy_report(report: object) -> dict | None:
    """Expose only the copy location, attachment totals, and inspection summary."""
    if not isinstance(report, dict):
        return None
    preview_id, output = report.get("preview_id"), report.get("output")
    restored, unresolved = report.get("restored"), report.get("unresolved")
    diagnostics = report.get("diagnostics")
    if (not isinstance(preview_id, str) or not isinstance(output, str) or "\x00" in output
            or type(restored) is not int or restored < 1
            or type(unresolved) is not int or unresolved < 0 or not _valid_report(diagnostics, "deep")):
        return None
    try:
        if not Path(output).is_absolute() or Path(output).name != recovery_copy_name(UUID(preview_id)):
            return None
    except ValueError:
        return None
    return {"preview_id": preview_id, "output": output, "restored": restored,
            "unresolved": unresolved, "diagnostics": diagnostics}


def archive_recovery_copy(root: Path, job: dict | None) -> Path | None:
    """Save a successful copy's compact record without private recovery inputs."""
    result = _recovery_result(job, RECOVERY_APPLY)
    if not result or result["status"] != "completed":
        return None
    identity = str(UUID(result["id"]))
    directory = recovery_record_dir(root.resolve())
    if directory.resolve() != directory:
        raise OSError("Recovery records directory is redirected.")
    report = result["report"]
    diagnostics = report["diagnostics"]
    record = {"id": identity, "finished_at": result["finished_at"], "output": report["output"],
              "restored": report["restored"], "unresolved": report["unresolved"],
              "verification": {"complete": diagnostics["complete"],
                               **{key: diagnostics["summary"][key] for key in ("problems", "review", "deferred")}}}
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{identity}.json"
    if path.resolve() != path:
        raise OSError("Recovery record is redirected.")
    if read_job(path) != record:
        write_job(path, record)
    return path


def retire_recovery_preview(root: Path, preview_id: str) -> None:
    """Remove only the specified temporary preview while holding storage ownership."""
    path = result_path(root.resolve(), scope=RECOVERY_PREVIEW)
    try:
        if path.resolve() != path:
            return
        current = read_job(path)
        if current and current["id"] == preview_id:
            path.unlink(missing_ok=True)
    except (OSError, RuntimeError):
        logger.warning("Could not remove the obsolete recovery preview.", exc_info=True)


def retire_clean_recovery_preview(root: Path) -> None:
    """Remove a preview only when a newer deep check rules out attachment damage."""
    preview = load_recovery_result(root, RECOVERY_PREVIEW)
    if preview is None:
        return
    deep = load_check_result(result_path(root), scope="deep")
    if deep is None or deep["status"] != "completed":
        return
    report = deep["report"]
    # A damaged manifest can block attachment checks without producing a deferred finding.
    if (not report["complete"] or report.get("has_attachment_recovery_candidates") is not False
            or report["summary"]["problems"] or report["summary"]["deferred"]):
        return
    checked = datetime.fromisoformat(deep["finished_at"])
    if checked <= datetime.fromisoformat(preview["finished_at"]):
        return
    basic = load_check_result(result_path(root, scope="basic"), scope="basic")
    if basic is not None and datetime.fromisoformat(basic["finished_at"]) > checked:
        latest = basic["report"]
        has_candidates = latest.get("has_attachment_recovery_candidates")
        if has_candidates is None:
            has_candidates = latest["omitted_findings"] > 0 or any(
                finding["code"] in ATTACHMENT_RECOVERY_CODES for finding in latest["findings"]
            )
        if has_candidates:
            return
    retire_recovery_preview(root, preview["id"])


def reconcile_recovery_records(root: Path) -> None:
    """Archive a legacy successful result and reconcile temporary state at startup."""
    job = read_job(result_path(root, scope=RECOVERY_APPLY))
    try:
        if archive_recovery_copy(root, job) is not None:
            retire_recovery_preview(root, job["report"]["preview_id"])
    except (OSError, RuntimeError):
        logger.warning("Could not archive the saved recovery copy record.", exc_info=True)
    retire_clean_recovery_preview(root)


def recovery_backup_selection(backup: Path) -> str:
    """Identify the selected folder without storing its path in the preview report."""
    return hashlib.sha256(os.fsencode(os.path.normcase(backup))).hexdigest()


def _recovery_confirmation(job: dict) -> str | None:
    """Reject previews saved before apply support or with invalid confirmation data."""
    confirmation, selection = job.get("confirmation"), job.get("backup_selection")
    if all(isinstance(digest, str) and len(digest) == 64
           and all(char in "0123456789abcdef" for char in digest) for digest in (confirmation, selection)):
        return confirmation
    return None


def require_recovery_confirmation(root: Path, preview_id: UUID, backup: Path) -> str:
    """Require the latest successful, non-empty preview and return its private confirmation."""
    job = read_job(result_path(root, scope=RECOVERY_PREVIEW))
    result = _recovery_result(job, RECOVERY_PREVIEW)
    confirmation = _recovery_confirmation(job) if job is not None else None
    if (job is None or result is None or result["id"] != str(preview_id)
            or result["status"] != "completed" or confirmation is None):
        raise RecoveryError("recovery_preview_required", "Run a new recovery preview and review its results before creating a copy.")
    if not result["report"]["verified"]:
        raise RecoveryError("no_verified_backups", "This preview has no verified attachment backups to restore.")
    if job.get("backup_selection") != recovery_backup_selection(backup):
        raise RecoveryError("backup_changed", "Select the backup folder used for this preview or run a new preview.")
    return confirmation


def resolve_preview_backup(root: Path, preview_id: UUID, backup_path: str) -> tuple[Path, str]:
    """Resolve the chosen backup folder and confirm it belongs to the saved preview."""
    backup = resolve_recovery_backup(root, backup_path)
    return backup, require_recovery_confirmation(root, preview_id, backup)


def prepare_recovery_copy(root: Path, preview_id: UUID, backup_path: str, output_dir: str) -> dict:
    """Validate a copy request without writing to a selected directory."""
    backup, confirmation = resolve_preview_backup(root, preview_id, backup_path)
    if "\x00" in output_dir or not Path(output_dir).is_absolute():
        raise RecoveryError("invalid_output_dir", "Choose an absolute path to an existing output directory.")
    parent, identity = resolve_recovery_output(root.resolve(), backup, Path(output_dir))
    destination = parent / recovery_copy_name(preview_id)
    if destination.exists() or destination.is_symlink():
        raise RecoveryError("recovery_copy_exists", "A recovery folder for this preview already exists in the selected directory.")
    return {"operation": RECOVERY_APPLY, "preview_id": str(preview_id), "confirmation": confirmation,
            "backup_path": str(backup), "output_dir": str(parent), "output_identity": list(identity)}


def save_basic_check(root: Path, report: dict) -> dict:
    """Keep the latest finished basic report and return it even if saving fails."""
    job = {"id": str(uuid4()), "status": "completed", "report": report,
           "finished_at": datetime.now(timezone.utc).isoformat()}
    path = result_path(root, scope="basic")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_job(path, job)
    except OSError:
        return {**job, "saved": False}
    return {**job, "saved": True}


class MaintenanceSession:
    """Publish one offline job and expose its result to Settings."""

    def __init__(self, channel: Path, root: Path, request_exit: Callable[[], None]) -> None:
        """Connect a server process to its owning launcher."""
        self.channel = channel
        self.root = root
        self.request_exit = request_exit
        self.pending: dict | None = None

    def queue_check(self) -> dict:
        """Queue a deep check and return its public ticket."""
        return self._queue()

    def queue_recovery_preview(self, backup: Path) -> dict:
        """Queue an attachment preview for one resolved backup folder."""
        return self._queue(operation=RECOVERY_PREVIEW, backup_path=str(backup))

    def queue_recovery_copy(self, parameters: dict) -> dict:
        """Queue a copy using validated inputs and one reviewed preview."""
        return self._queue(**parameters)

    def _queue(self, **parameters: object) -> dict:
        """Publish one job before requesting shutdown and return only its ticket."""
        if self.pending is not None:
            raise MaintenanceBusy("Storage maintenance is already scheduled.")
        job = {"id": str(uuid4()), "status": "pending"}
        request = {**job, **parameters}
        write_job(self.channel / "request.json", request)
        self.pending = request
        self.request_exit()
        return job

    def take_drafts(self) -> list[dict]:
        """Load the previous worker's draft bindings once."""
        path = self.channel / "drafts.json"
        handoff = read_job(path)
        path.unlink(missing_ok=True)
        return handoff.get("drafts", []) if handoff is not None else []

    def mark_drained(self, drafts: list[dict]) -> None:
        """Hand off unsaved Workspace bindings and acknowledge completed shutdown."""
        write_job(self.channel / "drafts.json", {"id": self.pending["id"], "drafts": drafts})
        write_job(self.channel / "drained.json", self.pending)

    def status(self) -> dict | None:
        """Return the queued deep check or its latest saved result."""
        pending = self._pending_ticket(None)
        if pending is not None:
            return pending
        saved = load_check_result(result_path(self.root), scope="deep")
        return self._pending_ticket(None) or saved

    def recovery_status(self, operation: RecoveryOperation) -> dict | None:
        """Return the pending or latest saved result for one recovery operation."""
        pending = self._pending_ticket(operation)
        if pending is not None:
            return pending
        saved = load_recovery_result(self.root, operation)
        return self._pending_ticket(operation) or saved

    def _pending_ticket(self, operation: str | None) -> dict | None:
        """Project a pending job without returning its private parameters."""
        pending = self.pending
        if pending is not None and pending.get("operation") == operation:
            return {"id": pending["id"], "status": pending["status"]}
        return None
