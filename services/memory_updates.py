"""Publish short-lived background memory notices to viewers of the owning conversation."""

import asyncio
from collections.abc import AsyncIterator
from typing import Literal


MemoryUpdatePhase = Literal["idle", "updating", "updated", "not_updated"]


class BackgroundMemoryUpdates:
    """Keep live status and briefly retain outcomes for conversation switches."""

    def __init__(self) -> None:
        """Initialize process-local state shared by memory policy snapshots."""
        self._phases: dict[str, MemoryUpdatePhase] = {}
        self._expiry: dict[str, asyncio.TimerHandle] = {}
        self._watchers: dict[str, set[asyncio.Event]] = {}

    def publish(self, conversation_id: str, phase: MemoryUpdatePhase) -> None:
        """Wake that conversation's observers and expire its terminal notice."""
        timer = self._expiry.pop(conversation_id, None)
        if timer is not None:
            timer.cancel()
        if phase == "idle":
            self._phases.pop(conversation_id, None)
        else:
            self._phases[conversation_id] = phase
        if phase in {"updated", "not_updated"}:
            self._expiry[conversation_id] = asyncio.get_running_loop().call_later(
                4.5, self.publish, conversation_id, "idle",
            )
        for event in self._watchers.get(conversation_id, ()):
            event.set()

    async def watch(self, conversation_id: str) -> AsyncIterator[MemoryUpdatePhase]:
        """Yield the current phase, then wait for changes until the viewer leaves."""
        event = asyncio.Event()
        watchers = self._watchers.setdefault(conversation_id, set())
        watchers.add(event)
        try:
            while True:
                event.clear()
                yield self._phases.get(conversation_id, "idle")
                await event.wait()
        finally:
            watchers.discard(event)
            if not watchers:
                del self._watchers[conversation_id]
