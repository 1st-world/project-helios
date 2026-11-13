"""Supervise a loopback Helios server and run deep storage checks between worker processes."""

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
    MaintenanceSession, StorageInUse, own_storage, read_job, result_path, write_job,
)


def _offline_result(root: Path, channel: Path, job: dict, exit_code: int) -> dict:
    """Run the check only after the worker finished a clean shutdown."""
    if exit_code or read_job(channel / "drained.json") != job:
        return {**job, "status": "failed", "message": "Helios could not finish shutting down. The deep check was skipped."}
    try:
        with own_storage(root):
            findings = inspect_storage(root, deep=True)
    except StorageInUse:
        return {**job, "status": "failed", "message": "Another Helios process owns this store. The deep check was skipped."}
    except Exception as error:
        print(f"Deep storage check failed ({type(error).__name__}).", file=sys.stderr, flush=True)
        return {**job, "status": "failed", "message": "The deep check could not finish. Try again after Helios restarts."}
    return {**job, "status": "completed", "report": storage_report(findings, scope="deep"),
            "findings": [asdict(item) for item in findings]}


def _save_result(root: Path, result: dict) -> None:
    """Keep a full offline report available even if the next worker cannot start."""
    path = result_path(root)
    try:
        write_job(path, {**result, "finished_at": datetime.now(timezone.utc).isoformat()})
    except OSError:
        print("Could not save the deep storage report. Helios will still restart.", file=sys.stderr, flush=True)
        print(result.get("message") or result["report"]["summary"], file=sys.stderr, flush=True)
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
                _save_result(root, {**job, "status": "failed", "message": "The launcher stopped before running the deep check."})
                return exit_code
            print("Helios has stopped. Starting storage maintenance.", flush=True)
            try:
                _save_result(root, _offline_result(root, channel, job, exit_code))
            except KeyboardInterrupt:
                _save_result(root, {**job, "status": "failed", "message": "The launcher stopped during the deep check."})
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
