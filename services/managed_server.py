"""Supervise a loopback Helios server and run storage maintenance between workers."""

import argparse
import asyncio
import os
import subprocess
import sys
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from config import PROJECT_ROOT, Settings
from services.storage_diagnostics import inspect_storage, storage_report
from services.storage_maintenance import (
    RECOVERY_PREVIEW, MaintenanceSession, StorageInUse, own_storage, read_job, result_path, write_job,
)
from services.storage_recovery import (
    RecoveryError, plan_attachment_recovery, recovery_preview_report, resolve_recovery_backup,
)


def _failed_result(job: dict, code: str, message: str) -> dict:
    """Record a failed operation without retaining request parameters in its result."""
    result = {"id": job["id"], "status": "failed", "message": message}
    if "operation" in job:
        result.update(operation=job["operation"], code=code)
    return result


def _preview_recovery(root: Path, job: dict) -> dict:
    """Verify attachment backups under the storage lock using the selected directory."""
    path = job.get("backup_path")
    if not isinstance(path, str):
        raise RecoveryError("invalid_backup_dir", "Choose an existing backup folder and try again.")
    backup = resolve_recovery_backup(root, path)
    if backup != Path(path):
        raise RecoveryError("backup_changed", "The backup folder changed. Select it again.")
    plan = plan_attachment_recovery(root, backup)
    if plan.backup != backup:
        raise RecoveryError("backup_changed", "The backup folder changed. Select it again.")
    return {"id": job["id"], "status": "completed", "operation": RECOVERY_PREVIEW,
            "report": recovery_preview_report(plan)}


def _offline_result(root: Path, channel: Path, job: dict, exit_code: int) -> dict:
    """Run one supported operation after the worker finished a clean shutdown."""
    operation = job.get("operation")
    if operation not in (None, RECOVERY_PREVIEW):
        return _failed_result(job, "unsupported_operation", "The launcher received an unsupported storage operation.")
    label = "recovery preview" if operation == RECOVERY_PREVIEW else "deep check"
    if exit_code or read_job(channel / "drained.json") != job:
        return _failed_result(job, "shutdown_failed", f"Helios could not finish shutting down. The {label} was skipped.")
    try:
        with own_storage(root):
            if operation == RECOVERY_PREVIEW:
                return _preview_recovery(root, job)
            findings = inspect_storage(root, deep=True)
    except StorageInUse:
        return _failed_result(job, "storage_in_use", f"Another Helios process owns this store. The {label} was skipped.")
    except RecoveryError as error:
        return _failed_result(job, error.code, str(error))
    except Exception as error:
        print(f"Storage maintenance failed ({type(error).__name__}).", file=sys.stderr, flush=True)
        return _failed_result(job, "operation_failed", f"The {label} could not finish. Try again after Helios restarts.")
    return {"id": job["id"], "status": "completed", "report": storage_report(findings, scope="deep"),
            "findings": [asdict(item) for item in findings]}


def _save_result(root: Path, result: dict) -> None:
    """Keep each type of offline outcome available even if the next worker cannot start."""
    operation = result.get("operation")
    if operation not in (None, RECOVERY_PREVIEW):
        print(result["message"], file=sys.stderr, flush=True)
        return
    path = result_path(root, scope=RECOVERY_PREVIEW if operation == RECOVERY_PREVIEW else "deep")
    label = "recovery preview" if operation == RECOVERY_PREVIEW else "deep storage"
    try:
        write_job(path, {**result, "finished_at": datetime.now(timezone.utc).isoformat()})
    except OSError:
        print(f"Could not save the {label} report.", file=sys.stderr, flush=True)
        if result.get("message"):
            print(result["message"], file=sys.stderr, flush=True)
        elif operation == RECOVERY_PREVIEW:
            print(f"Verified backups: {result['report']['verified']}. Unresolved: {result['report']['unresolved']}.",
                  file=sys.stderr, flush=True)
        else:
            print(result["report"]["summary"], file=sys.stderr, flush=True)
    else:
        print(f"Storage maintenance {result['status']}. Report: {path}", flush=True)


def _wait_for_worker(command: list[str]) -> tuple[int, bool]:
    """Wait for normal exit and request graceful stop when the launcher is interrupted."""
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
               else {"start_new_session": True})
    with subprocess.Popen(command, cwd=PROJECT_ROOT, stdin=subprocess.PIPE, **options) as child:
        try:
            return child.wait(), False
        except KeyboardInterrupt:
            child.stdin.close()
            child.wait()
            return 130, True


def run_managed(root: Path, port: int) -> int:
    """Restart only for accepted maintenance and retain the outcome between workers."""
    root = root.resolve()
    result_path(root).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="helios-maintenance-", ignore_cleanup_errors=True) as temporary:
        channel = Path(temporary)
        while True:
            command = [sys.executable, "-m", "services.managed_server", "--port", str(port),
                       "--worker", str(root), str(channel)]
            exit_code, interrupted = _wait_for_worker(command)
            job = read_job(channel / "request.json")
            if not job:
                return exit_code
            if interrupted:
                _save_result(root, _failed_result(job, "launcher_stopped", "The launcher stopped before running storage maintenance."))
                return exit_code
            print("Helios has stopped. Starting storage maintenance.", flush=True)
            try:
                _save_result(root, _offline_result(root, channel, job, exit_code))
            except KeyboardInterrupt:
                _save_result(root, _failed_result(job, "launcher_stopped", "The launcher stopped during storage maintenance."))
                return 130
            if exit_code or read_job(channel / "drained.json") != job:
                (channel / "drafts.json").unlink(missing_ok=True)
            (channel / "request.json").unlink()
            (channel / "drained.json").unlink(missing_ok=True)
            print("Starting Helios again.", flush=True)


def _serve_worker(root: Path, channel: Path, port: int) -> int:
    """Run one isolated server child and gracefully stop if its launcher exits."""
    import uvicorn
    from app import create_app

    settings = replace(Settings(), profiles_path=root / "profiles.json",
                       conversations_root=root / "conversation", logs_root=root / "logs")

    def request_exit() -> None:
        """Let Uvicorn drain existing responses and background work."""
        server.should_exit = True

    maintenance = MaintenanceSession(channel, root, request_exit)
    app = create_app(settings, maintenance=maintenance)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, lifespan="on", timeout_graceful_shutdown=None,
    ))

    def watch_launcher(loop: asyncio.AbstractEventLoop) -> None:
        """Use pipe closure as a portable parent-death signal without polling."""
        with suppress(OSError):
            os.read(sys.stdin.fileno(), 1)
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(request_exit)

    async def serve() -> None:
        """Observe the supervisor while serving one worker generation."""
        Thread(target=watch_launcher, args=(asyncio.get_running_loop(),), daemon=True).start()
        await server.serve()

    asyncio.run(serve())
    return 0 if server.started else 1


def main(argv: list[str] | None = None) -> int:
    """Start the managed loopback app or its private worker entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000, help="loopback HTTP port (default: 8000)")
    parser.add_argument("--worker", type=Path, nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        return _serve_worker(*args.worker, args.port) if args.worker else run_managed(PROJECT_ROOT, args.port)
    except (OSError, StorageInUse) as error:
        print(f"Could not start Helios: {error}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
