"""Own a fixed local project MCP session without starting it for ordinary chat."""

import asyncio
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

from mcp import Client
from mcp.types import CallToolResult, Tool

from config import settings
from services.project_access import ProjectAccess, ProjectReadError
from services.project_mcp_transport import ProjectStdio
from services.project_reader import ProjectReadLimits
from services.project_tools import (
    PROTOCOL_VERSION, tool_definitions, validate_arguments, validate_result,
)
from services.workspace_service import WorkspaceService


def protected_project_paths() -> list[Path]:
    """Protect host-selected credentials, backups, and runtime storage locations."""
    return [
        settings.profiles_path, settings.app_settings_path,
        settings.conversations_root, settings.logs_root,
        settings.attachments_root, settings.work_runs_path,
    ]


class ProjectMCPClient:
    """Serialize requests in one owner task and close the server on stop or failure."""

    def __init__(
        self, workspace: WorkspaceService, *,
        protected: list[Path] | None = None, timeout: float = 8,
    ) -> None:
        """Capture a selected workspace and trusted host paths without starting a process."""
        if workspace.root is None:
            raise ProjectReadError("workspace_required")
        self.workspace = workspace
        self.root = workspace.root
        self.revision = workspace.revision
        self.session_id = workspace.session_id
        self.protected = protected_project_paths() + (protected or [])
        self._access = ProjectAccess(self.root, self.protected)
        self.limits = ProjectReadLimits()
        self.timeout = timeout
        self._owner = None
        self._ready = None
        self._queue = asyncio.Queue(maxsize=1)
        self._lock = asyncio.Lock()
        self._closed = False
        self._stop_sent = False
        self._transport = None
        self.tools = []

    @property
    def pid(self) -> int | None:
        """Expose the owned process identifier for lifecycle diagnostics."""
        process = self._transport.process if self._transport else None
        return process.pid if process else None

    def _check_workspace(self) -> None:
        """Reject a changed workspace snapshot before any new local request."""
        if (self.workspace.root != self.root or self.workspace.revision != self.revision
                or self.workspace.session_id != self.session_id):
            raise ProjectReadError("workspace_changed")
        self._access.check_root()

    def _make_transport(self) -> ProjectStdio:
        """Launch only the bundled server with isolated Python and a fixed application cwd."""
        script = Path(__file__).with_name("project_mcp_server.py").resolve()
        config = {"root": str(self.root), "protected": [str(path) for path in self.protected]}
        env = {key: os.environ[key] for key in (
            "SystemRoot", "WINDIR", "TEMP", "TMP", "COMSPEC",
        ) if key in os.environ}
        env["HELIOS_PROJECT_CONTEXT"] = json.dumps(config, ensure_ascii=False)
        return ProjectStdio(
            sys.executable, ["-I", "-X", "utf8", "-u", str(script)],
            env, script.parent.parent, self.limits.ipc_bytes,
        )

    async def _serve(self) -> None:
        """Keep SDK context entry and exit in one task, including cancelled callers."""
        waiting = None
        try:
            self._transport = self._make_transport()
            async with Client(
                self._transport, mode=PROTOCOL_VERSION, cache=None,
                read_timeout_seconds=self.timeout,
            ) as client:
                listing = await client.list_tools()
                actual = [item.model_dump(by_alias=True, exclude_none=True) for item in listing.tools]
                definitions = tool_definitions()
                expected = [
                    Tool(**item).model_dump(by_alias=True, exclude_none=True)
                    for item in definitions
                ]
                if (client.protocol_version != PROTOCOL_VERSION
                        or listing.next_cursor is not None or actual != expected):
                    raise ProjectReadError("mcp_tool_contract_changed")
                self.tools = deepcopy(definitions)
                self._ready.set_result(None)
                while True:
                    name, arguments, waiting = await self._queue.get()
                    result = await client.session.call_tool(
                        name, arguments, read_timeout_seconds=self.timeout,
                        allow_input_required=True, allow_claimed=True,
                    )
                    if not isinstance(result, CallToolResult) or result.result_type != "complete":
                        raise ProjectReadError("mcp_unsupported_result")
                    payload = result.structured_content
                    validate_result(name, arguments, payload, self._access.normalize)
                    if (len(result.content) != 1 or result.content[0].type != "text"
                            or json.loads(result.content[0].text) != payload
                            or bool(payload.get("error")) != result.is_error
                            or len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > self.limits.result_bytes):
                        raise ProjectReadError("mcp_invalid_tool_result")
                    if not waiting.done():
                        waiting.set_result({
                            "payload": payload, "text": result.content[0].text,
                        })
                    waiting = None
        except BaseException as exc:
            self.failure_type = type(exc).__name__
            error = exc if isinstance(exc, ProjectReadError) else ProjectReadError("mcp_session_stopped")
            if not self._ready.done():
                self._ready.set_exception(error)
            if waiting is not None and not waiting.done():
                waiting.set_exception(error)
        finally:
            self._closed = True
            while not self._queue.empty():
                _, _, pending = self._queue.get_nowait()
                if not pending.done():
                    pending.set_exception(ProjectReadError("mcp_session_stopped"))

    async def __aenter__(self):
        """Start a session only for an explicitly selected project operation."""
        if self._owner is not None or self._closed:
            raise ProjectReadError("mcp_session_stopped")
        self._check_workspace()
        self._ready = asyncio.get_running_loop().create_future()
        self._owner = asyncio.create_task(self._serve())
        try:
            async with asyncio.timeout(self.timeout + 1):
                await asyncio.shield(self._ready)
            return self
        except BaseException:
            await self.close()
            if self._ready.done():
                self._ready.exception()
            raise

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Return the validated structured payload for one project request."""
        return (await self.call_tool_result(name, arguments))["payload"]

    async def call_tool_result(self, name: str, arguments: dict) -> dict:
        """Submit one bounded local request and stop the session if its caller leaves."""
        validate_arguments(name, arguments)
        if len(json.dumps(arguments, ensure_ascii=False).encode("utf-8")) > 16384:
            raise ProjectReadError("mcp_argument_limit")
        async with self._lock:
            if self._closed or self._owner is None:
                raise ProjectReadError("mcp_session_stopped")
            try:
                self._check_workspace()
                result = asyncio.get_running_loop().create_future()
                self._queue.put_nowait((name, deepcopy(arguments), result))
                async with asyncio.timeout(self.timeout):
                    return await result
            except BaseException:
                await self.close()
                raise

    async def close(self) -> None:
        """Cancel the owner and wait for process cleanup even under repeated cancellation."""
        self._closed = True
        owner = self._owner
        if owner is None:
            return
        if not owner.done() and not self._stop_sent:
            self._stop_sent = True
            owner.cancel()
        cancelled = False
        while not owner.done():
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError:
                if not owner.done():
                    cancelled = True
        if cancelled:
            raise asyncio.CancelledError()

    async def __aexit__(self, *details):
        """Close an explicit project session without affecting ordinary chat clients."""
        await self.close()
