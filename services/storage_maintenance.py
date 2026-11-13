"""Coordinate storage ownership and a single deep-check job across server restarts."""

import json
import os
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from uuid import UUID, uuid4

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class StorageInUse(RuntimeError):
    """Report a store already owned by a cooperating Helios process."""


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


def result_path(root: Path, *, scope: Literal["basic", "deep"] = "deep") -> Path:
    """Locate the latest report of the selected check type."""
    filename = "last-basic-check.json" if scope == "basic" else "last-deep-check.json"
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


def check_result(job: dict | None, *, scope: Literal["basic", "deep"]) -> dict | None:
    """Return a valid saved outcome for Settings."""
    if not job:
        return None
    try:
        finished = datetime.fromisoformat(job.get("finished_at"))
        if finished.tzinfo is None:
            return None
        timestamp = finished.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None
    status = job.get("status")
    result = {"id": job["id"], "status": status, "finished_at": timestamp}
    if status == "completed" and _valid_report(job.get("report"), scope):
        return {**result, "report": job["report"]}
    if scope == "deep" and status == "failed" and isinstance(job.get("message"), str) and job["message"].strip():
        return {**result, "message": job["message"]}
    return None


def load_check_result(path: Path, *, scope: Literal["basic", "deep"]) -> dict | None:
    """Read the saved result and treat a missing or malformed file as no history."""
    return check_result(read_job(path), scope=scope)


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
    """Publish one fixed deep-check request and expose its result to Settings."""

    def __init__(self, channel: Path, root: Path, request_exit: Callable[[], None]) -> None:
        """Connect a server process to its owning launcher."""
        self.channel = channel
        self.result_file = result_path(root)
        self.request_exit = request_exit
        self.pending: dict | None = None

    def queue_check(self) -> dict:
        """Record the request before beginning graceful shutdown."""
        job = {"id": str(uuid4()), "status": "pending"}
        write_job(self.channel / "request.json", job)
        self.pending = job
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
        """Return this process's queued job or the most recent saved result."""
        if self.pending is not None:
            return self.pending
        result = load_check_result(self.result_file, scope="deep")
        return self.pending if self.pending is not None else result
