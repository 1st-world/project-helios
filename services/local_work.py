"""Run cancellable local work without releasing request ownership before its worker exits."""

from collections.abc import Callable
from contextvars import ContextVar
from threading import Event
from typing import Any, TypeVar

import anyio


T = TypeVar("T")
_stop: ContextVar[Event | None] = ContextVar("local_work_stop", default=None)


class _LocalWorkCancelled(BaseException):
    """Unwind local parser contexts without becoming a file-validation error."""


def check_local_cancelled() -> None:
    """Stop at a safe boundary when the owning asynchronous request was cancelled."""
    stop = _stop.get()
    if stop is not None and stop.is_set():
        raise _LocalWorkCancelled()


async def run_local(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Offload work, request cooperative cancellation, and always wait for worker cleanup."""
    stop = Event()
    finished = anyio.Event()
    result: Any = None
    error: BaseException | None = None

    def invoke() -> T:
        """Install a request-specific stop signal only inside this worker call."""
        token = _stop.set(stop)
        try:
            check_local_cancelled()
            return function(*args, **kwargs)
        finally:
            _stop.reset(token)

    async def wait_for_worker() -> None:
        """Keep the thread joined even when its parent task receives cancellation."""
        nonlocal result, error
        with anyio.CancelScope(shield=True):
            try:
                result = await anyio.to_thread.run_sync(invoke)
            except BaseException as exc:
                error = exc
            finally:
                finished.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(wait_for_worker)
        try:
            await finished.wait()
        finally:
            stop.set()
    if error is not None:
        raise error
    return result
