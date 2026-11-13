"""Coordinate storage ownership and one offline job across server restarts."""

import json
import os
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ValidationError

from services.storage_recovery import RecoveryPreviewReport

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class StorageInUse(RuntimeError):
    """Report a store already owned by a cooperating Helios process."""


class MaintenanceBusy(RuntimeError):
    """Report that a worker has already accepted an offline operation."""


RECOVERY_PREVIEW = "recovery-preview"


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


def read_job(path: Path) -> dict | None:
    """Return an intact job envelope and treat missing or damaged state as absent."""
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
        identity = job["id"]
        if isinstance(identity, str) and str(UUID(identity)) == identity:
            return job
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        pass
    return None


def result_path(root: Path, *, scope: Literal["basic", "deep", "recovery-preview"] = "deep") -> Path:
    """Locate the latest report of the selected maintenance operation."""
    filename = {"basic": "last-basic-check.json", "deep": "last-deep-check.json",
                RECOVERY_PREVIEW: "last-recovery-preview.json"}[scope]
    return root / ".cache" / "storage-maintenance" / filename


def _valid_report(report: object, scope: Literal["basic", "deep"]) -> bool:
    """Check the saved report fields the Settings view needs to render."""
    if not isinstance(report, dict) or report.get("scope") != scope or type(report.get("complete")) is not bool:
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


def check_result(job: dict | None, *, scope: Literal["basic", "deep"]) -> dict | None:
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


def load_check_result(path: Path, *, scope: Literal["basic", "deep"]) -> dict | None:
    """Read the saved result and treat a missing or malformed file as no history."""
    return check_result(read_job(path), scope=scope)


def load_recovery_preview(root: Path) -> dict | None:
    """Return the latest validated preview without exposing private job parameters."""
    job = read_job(result_path(root, scope=RECOVERY_PREVIEW))
    result = _finished_outcome(job)
    if job is None or result is None or job.get("operation") != RECOVERY_PREVIEW:
        return None
    if result["status"] == "completed":
        try:
            report = RecoveryPreviewReport.model_validate(job.get("report"))
        except ValidationError:
            return None
        return {**result, "report": report.model_dump()}
    if result["status"] == "failed":
        code, message = job.get("code"), job.get("message")
        if isinstance(code, str) and code and isinstance(message, str) and message.strip():
            return {**result, "code": code, "message": message}
    return None


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

    def _queue(self, **parameters: str) -> dict:
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

    def recovery_preview_status(self) -> dict | None:
        """Return the queued recovery preview or its latest saved result."""
        pending = self._pending_ticket(RECOVERY_PREVIEW)
        if pending is not None:
            return pending
        saved = load_recovery_preview(self.root)
        return self._pending_ticket(RECOVERY_PREVIEW) or saved

    def _pending_ticket(self, operation: str | None) -> dict | None:
        """Project a pending job without returning its private parameters."""
        pending = self.pending
        if pending is not None and pending.get("operation") == operation:
            return {"id": pending["id"], "status": pending["status"]}
        return None
