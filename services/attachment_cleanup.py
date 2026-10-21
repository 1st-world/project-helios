"""Schedule attachment cleanup on ownership changes, actual expiries, and failed-file retries."""

import asyncio
import logging
import sqlite3
from threading import Lock
from time import monotonic

from services.attachment_errors import AttachmentReferencesUnavailableError
from services.attachment_store import AttachmentStore
from services.context_recovery import ContextRecoveryRegistry
from services.local_work import run_local

logger = logging.getLogger(__name__)


class AttachmentCleanup:
    """Sleep without a timer when no expiry or retry remains and wake safely from workers."""

    def __init__(self, store: AttachmentStore, recovery: ContextRecoveryRegistry) -> None:
        """Keep pending identities until startup without requiring a running event loop."""
        self.store, self.recovery = store, recovery
        self._lock = Lock()
        self._pending: set[str] = set()
        self._loop = None
        self._wake = None

    def notify(self, identities: set[str] | None = None) -> None:
        """Queue affected copies or reschedule a changed deadline from either thread."""
        with self._lock:
            self._pending.update(identities or ())
            loop, wake = self._loop, self._wake
        if loop is not None:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                # Startup reconciliation recovers changes made after shutdown begins.
                pass

    def _take_pending(self) -> set[str]:
        """Drain one batch without dropping notifications arriving during local work."""
        with self._lock:
            identities, self._pending = self._pending, set()
            return identities

    async def run(self) -> None:
        """Reconcile once, then wait for a change or the earliest concrete deadline."""
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._wake = asyncio.Event()
        first, failures, retry_at = True, 0, None
        suspended = False
        try:
            while True:
                self._wake.clear()
                identities = self.recovery.prune() | self._take_pending()
                delay = None
                try:
                    if suspended or (retry_at is not None and retry_at > monotonic()):
                        with self._lock:
                            self._pending.update(identities)
                    else:
                        delay = await run_local(self.store.next_cleanup_delay)
                        if first or identities or retry_at is not None or delay == 0:
                            await run_local(self.store.collect, identities)
                            delay = await run_local(self.store.next_cleanup_delay)
                            failures, retry_at = 0, None
                except AttachmentReferencesUnavailableError:
                    with self._lock:
                        self._pending.update(identities)
                    suspended, retry_at, delay = True, None, None
                    logger.warning("Automatic attachment cleanup is suspended. Repair the conversation files reported at startup and restart Helios to verify their references.")
                except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
                    with self._lock:
                        self._pending.update(identities)
                    failures += 1
                    retry_at = monotonic() + min(2 ** min(failures - 1, 9), 300)
                    logger.warning("Attachment cleanup was deferred because stored references could not be verified.", exc_info=True)
                first = False
                if retry_at is not None:
                    delay = max(0, retry_at - monotonic())
                delays = [delay] if delay is not None else []
                recovery_due = self.recovery.next_attachment_expiry()
                if recovery_due is not None:
                    delays.append(max(0, recovery_due - monotonic()))
                if not delays:
                    await self._wake.wait()
                else:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=min(delays))
                    except asyncio.TimeoutError:
                        pass
        finally:
            with self._lock:
                self._loop = self._wake = None
