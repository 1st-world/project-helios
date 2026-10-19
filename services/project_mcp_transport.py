"""Bound local MCP stdio frames and own the server process tree through cleanup."""

import asyncio
import os
import signal
import subprocess
from contextlib import asynccontextmanager, suppress

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import jsonrpc_message_adapter

from services.project_access import ProjectReadError


class ProjectStdio:
    """Bridge bounded newline frames to SDK streams without implementing MCP sessions."""

    def __init__(self, command: str, args: list[str], env: dict, cwd, maximum: int):
        """Capture only host-owned launch parameters and the IPC byte boundary."""
        self.command = command
        self.args = args
        self.env = env
        self.cwd = cwd
        self.maximum = maximum
        self.process = None

    @asynccontextmanager
    async def connect(self):
        """Start one process tree and always close its streams and reap its owner."""
        with open(os.devnull, "w", encoding="utf-8") as errors:
            if os.name == "nt":
                from mcp.os.win32 import utilities

            async def start():
                """Finish acquiring the process handle before allowing owner cancellation."""
                if os.name == "nt":
                    return await utilities.create_windows_process(
                        self.command, self.args, self.env, errors, self.cwd,
                    )
                return await anyio.open_process(
                    [self.command, *self.args], env=self.env, cwd=self.cwd,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                )

            startup = asyncio.create_task(start())
            try:
                self.process = await asyncio.shield(startup)
            except BaseException:
                with anyio.CancelScope(shield=True):
                    self.process = await startup
                    await self._kill_tree()
                    await self._close_process()
                raise
            incoming, receive = anyio.create_memory_object_stream(0)
            send, outgoing = anyio.create_memory_object_stream(0)

            async def read() -> None:
                """Reject oversized or malformed lines before SDK parsing can retain them."""
                buffer = bytearray()
                try:
                    async with incoming:
                        while True:
                            try:
                                chunk = await self.process.stdout.receive(65536)
                            except anyio.EndOfStream:
                                if buffer:
                                    raise ProjectReadError("mcp_truncated_message")
                                break
                            buffer.extend(chunk)
                            while (boundary := buffer.find(b"\n")) >= 0:
                                if boundary > self.maximum:
                                    raise ProjectReadError("mcp_message_limit")
                                line = bytes(buffer[:boundary])
                                del buffer[:boundary + 1]
                                try:
                                    message = jsonrpc_message_adapter.validate_json(line, by_name=False)
                                except ValueError:
                                    raise ProjectReadError("mcp_invalid_message") from None
                                await incoming.send(SessionMessage(message))
                            if len(buffer) > self.maximum:
                                raise ProjectReadError("mcp_message_limit")
                except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
                    pass

            async def write() -> None:
                """Serialize only bounded SDK messages to the child process input."""
                try:
                    async with outgoing:
                        async for message in outgoing:
                            data = message.message.model_dump_json(
                                by_alias=True, exclude_unset=True,
                            ).encode("utf-8")
                            if len(data) > self.maximum:
                                raise ProjectReadError("mcp_message_limit")
                            await self.process.stdin.send(data + b"\n")
                except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
                    incoming.close()

            try:
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(read)
                    tasks.start_soon(write)
                    try:
                        if os.name == "nt" and self.process not in utilities._process_jobs:
                            # SDK 2.3.0 exposes no public job-assignment status; fail closed.
                            raise ProjectReadError("mcp_process_boundary_unavailable")
                        yield receive, send
                    finally:
                        with anyio.CancelScope(shield=True):
                            send.close()
                            with anyio.move_on_after(0.5):
                                await self.process.stdin.aclose()
                                await self.process.wait()
                            await self._kill_tree()
                            receive.close()
                            await self._close_process()
                        tasks.cancel_scope.cancel()
            finally:
                with anyio.CancelScope(shield=True):
                    await self._kill_tree()
                    await self._close_process()

    async def _close_process(self) -> None:
        """Close OS pipe transports before waiting for a potentially flooded process."""
        with anyio.fail_after(2):
            if hasattr(self.process, "aclose"):
                await self.process.aclose()
            else:
                await self.process.stdin.aclose()
                await self.process.stdout.aclose()
                await self.process.wait()

    async def _kill_tree(self) -> None:
        """Reap descendants even when their parent already exited normally."""
        if self.process is None:
            return
        if os.name == "nt":
            from mcp.os.win32.utilities import terminate_windows_process_tree

            await terminate_windows_process_tree(self.process)
        else:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)

    async def __aenter__(self):
        """Enter the transport in the same owner task that will later close it."""
        self._context = self.connect()
        return await self._context.__aenter__()

    async def __aexit__(self, *details):
        """Unwind protocol streams and process ownership together."""
        return await self._context.__aexit__(*details)
