"""Own native folder picker processes across browser disconnects, reconnects, and cancellation."""

import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


class PickerReconnectingError(RuntimeError):
    """The previous page connection or process cleanup is still finishing."""


@dataclass
class PickerSession:
    """Track one picker process, its tab connection, result, and cleanup tasks."""

    process: subprocess.Popen
    result: dict = field(default_factory=lambda: {"status": "pending"})
    connected: bool = True
    cancelling: bool = False
    retiring: bool = False
    reader: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class FolderPickerService:
    """Manage tab-owned picker sessions with a grace period for browser reconnection."""

    RECONNECT_GRACE = 5

    def __init__(self):
        """Initialize the session registry and shutdown state."""
        self.sessions: dict[str, PickerSession] = {}
        self.closing = False

    def attach(
        self, token: str, initial_dir: str, *, start: bool
    ) -> PickerSession | None:
        """Resume a disconnected session or start a worker when explicitly requested."""
        if self.closing:
            raise RuntimeError("Folder picker service is stopping.")
        session = self.sessions.get(token)
        if session is not None:
            if session.retiring:
                raise PickerReconnectingError(
                    "Folder picker cleanup is still in progress."
                )
            if session.connected:
                raise PickerReconnectingError(
                    "The previous picker connection is still open."
                )
            session.connected = True
            if session.cleanup:
                session.cleanup.cancel()
                session.cleanup = None
            return session
        if not start:
            return None
        worker = Path(__file__).with_name("folder_picker_worker.py")
        process = subprocess.Popen(
            [sys.executable, str(worker), initial_dir],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
        session = PickerSession(process)
        self.sessions[token] = session
        session.reader = asyncio.create_task(self._read_result(session))
        return session

    async def _read_result(self, session: PickerSession) -> None:
        """Validate worker output without overwriting a cancellation in progress."""
        try:
            output, _ = await asyncio.to_thread(session.process.communicate)
            result = json.loads(output)
            if result.get("status") not in {"selected", "cancelled", "error"}:
                raise ValueError("Invalid picker result")
            if result["status"] == "selected" and not isinstance(
                result.get("path"), str
            ):
                raise ValueError("Invalid picker path")
        except Exception:
            result = {
                "status": "error",
                "detail": "Folder picker exited unexpectedly. Try Browse again or enter the path manually.",
            }
        if not session.cancelling:
            session.result = result

    async def cancel(self, session: PickerSession) -> None:
        """Stop the owned process and acknowledge cancellation after its reader finishes."""
        async with session.lock:
            session.cancelling = True
            process = session.process
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.to_thread(process.wait, timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=2)
            if session.reader:
                await session.reader
            session.result = {"status": "cancelled"}

    def detach(self, token: str, session: PickerSession) -> None:
        """Mark the tab disconnected and schedule cleanup after the reconnect grace period."""
        session.connected = False
        if not self.closing:
            session.cleanup = asyncio.create_task(
                self._cleanup(token, session)
            )

    async def _cleanup(self, token: str, session: PickerSession) -> None:
        """Retire and stop a session that remains disconnected after the grace period."""
        await asyncio.sleep(self.RECONNECT_GRACE)
        # Mark retirement before awaiting termination so late reconnects cannot revive the process.
        if session.connected or self.sessions.get(token) is not session:
            return
        session.retiring = True
        try:
            await self.cancel(session)
            self.sessions.pop(token, None)
        except Exception:
            logger.exception("Could not stop disconnected folder picker")

    async def close(self) -> None:
        """Stop all registered picker processes and clear the session registry."""
        self.closing = True
        sessions = list(self.sessions.values())
        for session in sessions:
            if session.cleanup and not session.retiring:
                session.cleanup.cancel()
        results = await asyncio.gather(
            *(self.cancel(session) for session in sessions),
            return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                logger.error(
                    "Could not stop folder picker during shutdown: %s", result
                )
        self.sessions.clear()
