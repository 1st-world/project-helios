"""Serve only Helios's pinned project-reading tools over local MCP stdio."""

import asyncio
import json
import os
import sys
from pathlib import Path

# Isolated Python startup excludes the script directory from module search paths.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from models.workspace import WorkspaceReadOptions
from services.local_work import run_local
from services.project_access import ProjectReadError
from services.project_reader import ProjectReader
from services.project_tools import SERVER_VERSION, tool_definitions, validate_arguments


async def serve() -> None:
    """Load host-owned paths and let the official SDK dispatch the MCP protocol."""
    raw = os.environ.pop("HELIOS_PROJECT_CONTEXT", "")
    if len(raw.encode("utf-8")) > 16384:
        raise RuntimeError("Invalid project server bootstrap.")
    config = json.loads(raw)
    reader = ProjectReader(
        Path(config["root"]), [Path(item) for item in config["protected"]],
        options=WorkspaceReadOptions.model_validate(config.get("read_options", {})),
    )
    tools = tool_definitions()
    lock = anyio.Lock()

    async def list_tools(context, params):
        """Advertise only the three built-in tools and their fixed schemas."""
        return ListToolsResult(tools=[Tool(**item) for item in tools])

    async def call_tool(context, params):
        """Validate and run one bounded reader operation without exposing exception data."""
        async with lock:
            try:
                validate_arguments(params.name, params.arguments or {})
                result = await run_local(reader.call, params.name, params.arguments or {})
                failed = False
            except ProjectReadError as exc:
                result = {"contract_version": "0.1", "error": {"code": exc.code}}
                failed = True
            except (OSError, UnicodeError):
                result = {"contract_version": "0.1", "error": {"code": "file_access_error"}}
                failed = True
            except Exception:
                result = {"contract_version": "0.1", "error": {"code": "local_tool_failed"}}
                failed = True
            text = json.dumps(result, ensure_ascii=False, allow_nan=False)
            return CallToolResult(
                content=[TextContent(type="text", text=text)],
                structuredContent=result, isError=failed,
            )

    server = Server(
        "helios-project", version=SERVER_VERSION,
        on_list_tools=list_tools, on_call_tool=call_tool,
        get_tool_input_schema=lambda name: next((
            item["inputSchema"] for item in tools if item["name"] == name
        ), None),
    )
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        reader.close()


if __name__ == "__main__":
    asyncio.run(serve())
