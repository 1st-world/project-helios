"""Serve the Helios web client and API, connecting application services and managing their lifecycle."""

import asyncio
import json
import logging
from contextlib import aclosing, asynccontextmanager
from datetime import date
from logging.handlers import RotatingFileHandler
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import anyio
from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from config import settings
from services.ai_service import AIService
from services.context_budget import (
    ContextBudget,
    ContextBudgetExceeded,
    ContextChangedError,
    ContextWindowExceeded,
    SummaryUnavailableError,
)
from services.conversation_service import (
    ConversationManager,
    ConversationUnavailableError,
)
from services.call_usage_store import CallUsageStore
from services.folder_picker_service import (
    FolderPickerService,
    PickerReconnectingError,
)
from services.memory_service import ConversationMemoryService
from services.profile_service import ProfileService
from services.summary_usage_store import SummaryUsageStore
from services.usage_service import UsageService
from services.workspace_service import WorkspaceAccessError, WorkspaceService

settings.logs_root.mkdir(parents=True, exist_ok=True)
file_handler = RotatingFileHandler(
    settings.logs_root / "helios.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=10,
    encoding="utf-8",
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[file_handler, logging.StreamHandler()],
)
logging.getLogger("watchfiles").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

profile_service = ProfileService(settings.profiles_path)
conversation_manager = ConversationManager(settings.conversations_root)
workspace_service = WorkspaceService(settings.workspace_root)
folder_picker_service = FolderPickerService()
usage_service = UsageService()
summary_usage_store = SummaryUsageStore(
    settings.logs_root / "summary_usage.sqlite3"
)
call_usage_store = CallUsageStore(settings.logs_root / "usage_calls.sqlite3")
try:
    call_usage_store.import_summaries(summary_usage_store)
except Exception:
    call_usage_store.import_errors += 1
    logger.exception("Could not import historical summary usage")
for stored_conversation in conversation_manager.list():
    try:
        call_usage_store.import_conversation(
            conversation_manager.get(stored_conversation["id"])
        )
    except Exception:
        call_usage_store.import_errors += 1
        logger.exception("Could not import historical conversation usage")
ai_service = AIService(
    usage_service, profile_service, summary_usage_store=summary_usage_store,
    call_usage_store=call_usage_store,
)
context_budget = ContextBudget(
    settings.context_token_budget,
    settings.context_output_reserve,
    settings.max_summary_calls,
)
memory_service = ConversationMemoryService(
    ai_service,
    conversation_manager,
    settings.max_context_messages,
    settings.keep_recent_messages,
    context_budget,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Log application startup and close picker and AI resources on shutdown."""
    logger.info("Helios started")
    yield
    await folder_picker_service.close()
    await ai_service.close()
    logger.info("Helios stopped")


app = FastAPI(title="Helios BYOK AI Assistant", lifespan=lifespan)
app.mount(
    "/static", StaticFiles(directory=settings.static_root), name="static"
)
templates = Jinja2Templates(directory=settings.templates_root)


@app.get("/api/usage/summaries")
async def summary_usage(conversation_id: str | None = None):
    """Return recorded summary usage for one conversation or all conversations."""
    return summary_usage_store.totals(conversation_id)


@app.get("/api/usage")
async def call_usage(
    conversation_id: str | None = None,
    kind: Literal[
        "chat", "regeneration", "summary", "legacy_reply"
    ] | None = None,
    timezone: str = "UTC", start_date: date | None = None,
    end_date: date | None = None,
):
    """Return cumulative call usage and daily buckets in the requested IANA timezone."""
    try:
        return call_usage_store.totals(
            conversation_id=conversation_id, kind=kind,
            timezone_name=timezone, start_date=start_date, end_date=end_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/usage/calls")
async def call_usage_records(
    conversation_id: str | None = None,
    kind: Literal[
        "chat", "regeneration", "summary", "legacy_reply"
    ] | None = None,
    timezone: str = "UTC", start_date: date | None = None,
    end_date: date | None = None, limit: int = 100, offset: int = 0,
):
    """Return a bounded page of immutable call results and saved pricing metadata."""
    try:
        return {"calls": call_usage_store.records(
            conversation_id=conversation_id, kind=kind,
            timezone_name=timezone, start_date=start_date, end_date=end_date,
            limit=limit, offset=offset,
        )}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ReadFileRequest(BaseModel):
    """Validate the relative workspace path requested for reading."""

    path: str = Field(min_length=1, max_length=4096)


class ChatRequest(BaseModel):
    """Validate a new chat turn or regeneration request and its context selection."""

    prompt: str = Field(min_length=1, max_length=100_000)
    conversation_id: str | None = None
    workspace_file: str | None = None
    profile_id: str | None = None
    regenerate_message_index: int | None = Field(default=None, ge=0)
    expected_conversation_version: int | None = Field(default=None, ge=0)


class RenameConversationRequest(BaseModel):
    """Validate the requested conversation title."""

    title: str = Field(min_length=1, max_length=100)


class EditMessageRequest(BaseModel):
    """Validate replacement content for a user message."""

    content: str = Field(min_length=1, max_length=100_000)


class WorkspaceRootRequest(BaseModel):
    """Validate the submitted workspace folder path."""

    path: str = Field(min_length=1, max_length=4096)


class CreateProfileRequest(BaseModel):
    """Validate connection credentials, deployment, and optional pricing for a profile."""

    name: str = Field(min_length=1, max_length=100)
    endpoint: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(min_length=1, max_length=1024)
    deployment: str = Field(min_length=1, max_length=256)
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)
    long_context_threshold: int | None = Field(default=128_000, ge=1)
    long_input_price_per_million: float | None = Field(default=None, ge=0)
    long_output_price_per_million: float | None = Field(default=None, ge=0)
    cache_read_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cache_write_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    long_cache_read_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    long_cache_write_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class UpdateProfileRequest(BaseModel):
    """Validate partial profile changes and explicit requests to clear pricing."""

    name: str | None = Field(default=None, max_length=100)
    endpoint: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=1024)
    deployment: str | None = Field(default=None, max_length=256)
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)
    clear_input_price: bool = False
    clear_output_price: bool = False
    long_context_threshold: int | None = Field(default=None, ge=1)
    long_input_price_per_million: float | None = Field(default=None, ge=0)
    long_output_price_per_million: float | None = Field(default=None, ge=0)
    clear_long_input_price: bool = False
    clear_long_output_price: bool = False
    cache_read_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cache_write_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    long_cache_read_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    long_cache_write_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    clear_cache_read_price: bool = False
    clear_cache_write_price: bool = False
    clear_long_cache_read_price: bool = False
    clear_long_cache_write_price: bool = False


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Render the main assistant page with the active deployment name."""
    return templates.TemplateResponse(
        request, "index.html", {"model_name": ai_service.active_deployment}
    )


@app.get("/api/health")
async def health():
    """Report whether the active AI connection is configured and name its deployment."""
    return {
        "configured": ai_service.configured,
        "model": ai_service.active_deployment,
    }


@app.get("/api/profiles")
async def list_profiles():
    """Return connection profiles and the active selection without API keys."""
    return profile_service.get_summary()


@app.post("/api/profiles")
async def create_profile(payload: CreateProfileRequest):
    """Create a connection profile and return the refreshed profile summary."""
    try:
        profile = profile_service.create_profile(
            payload.name,
            payload.endpoint,
            payload.api_key,
            payload.deployment,
            payload.input_price_per_million,
            payload.output_price_per_million,
            payload.long_context_threshold,
            payload.long_input_price_per_million,
            payload.long_output_price_per_million,
            cache_read_price_per_million=payload.cache_read_price_per_million,
            cache_write_price_per_million=payload.cache_write_price_per_million,
            long_cache_read_price_per_million=payload.long_cache_read_price_per_million,
            long_cache_write_price_per_million=payload.long_cache_write_price_per_million,
        )
    except Exception:
        logger.exception("Could not create profile")
        raise HTTPException(
            status_code=400, detail="Could not create connection profile."
        )
    return profile_service.get_summary()


@app.put("/api/profiles/{profile_id}")
async def update_profile(profile_id: str, payload: UpdateProfileRequest):
    """Apply profile changes and return the refreshed profile summary."""
    try:
        profile_service.update_profile(
            profile_id,
            name=payload.name,
            endpoint=payload.endpoint,
            api_key=payload.api_key,
            deployment=payload.deployment,
            input_price_per_million=payload.input_price_per_million,
            output_price_per_million=payload.output_price_per_million,
            clear_input_price=payload.clear_input_price,
            clear_output_price=payload.clear_output_price,
            long_context_threshold=payload.long_context_threshold,
            long_input_price_per_million=payload.long_input_price_per_million,
            long_output_price_per_million=payload.long_output_price_per_million,
            clear_long_input_price=payload.clear_long_input_price,
            clear_long_output_price=payload.clear_long_output_price,
            cache_read_price_per_million=payload.cache_read_price_per_million,
            cache_write_price_per_million=payload.cache_write_price_per_million,
            long_cache_read_price_per_million=payload.long_cache_read_price_per_million,
            long_cache_write_price_per_million=payload.long_cache_write_price_per_million,
            clear_cache_read_price=payload.clear_cache_read_price,
            clear_cache_write_price=payload.clear_cache_write_price,
            clear_long_cache_read_price=payload.clear_long_cache_read_price,
            clear_long_cache_write_price=payload.clear_long_cache_write_price,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception:
        logger.exception("Could not update profile %s", profile_id)
        raise HTTPException(
            status_code=400, detail="Could not update connection profile."
        )
    return profile_service.get_summary()


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    """Delete a connection profile or report that it does not exist."""
    deleted = profile_service.delete_profile(profile_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Profile not found.")
    return profile_service.get_summary()


@app.post("/api/profiles/{profile_id}/active")
async def set_active_profile(profile_id: str):
    """Select an existing connection profile for subsequent requests."""
    try:
        profile_service.set_active_profile(profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return profile_service.get_summary()


@app.get("/api/conversations")
async def conversations():
    """Return conversation summaries ordered by their latest update."""
    return conversation_manager.list()


@app.post("/api/conversations")
async def create_conversation():
    """Create and persist an empty conversation."""
    return conversation_manager.create().to_dict()


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    """Return a conversation with its complete transcript or report it missing."""
    conversation = conversation_manager.get(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation.to_dict()


@app.patch("/api/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: str, payload: RenameConversationRequest
):
    """Save a validated conversation title and return its summary."""
    try:
        conversation = conversation_manager.rename(
            conversation_id, payload.title
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation.to_dict(include_messages=False)


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    """Delete a conversation from memory and disk, reporting storage failures."""
    try:
        deleted = conversation_manager.delete(conversation_id)
    except OSError:
        logger.exception("Could not delete conversation: %s", conversation_id)
        raise HTTPException(
            status_code=500, detail="Could not delete conversation from disk."
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"deleted": True}


@app.patch("/api/conversations/{conversation_id}/messages/{message_index}")
async def edit_message(
    conversation_id: str, message_index: int, payload: EditMessageRequest
):
    """Edit a user message and return the truncated conversation branch."""
    try:
        conversation = conversation_manager.edit_user_message(
            conversation_id, message_index, payload.content
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation.to_dict()


@app.get("/api/workspace")
async def workspace_tree():
    """Return the configured workspace root and its accessible file tree."""
    return {
        "root": str(workspace_service.root),
        "entries": workspace_service.tree(),
    }


@app.put("/api/workspace/root")
async def update_workspace_root(payload: WorkspaceRootRequest):
    """Select an existing workspace folder and return its file tree."""
    try:
        workspace_service.set_root(payload.path)
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info("Workspace root changed to %s", workspace_service.root)
    return {
        "root": str(workspace_service.root),
        "entries": workspace_service.tree(),
    }


@app.websocket("/api/workspace/picker/{token}")
async def workspace_picker(websocket: WebSocket, token: UUID):
    """Relay picker state and commands for a same-origin browser tab."""
    # Reject cross-origin pages before allowing access to native dialogs.
    origin = websocket.headers.get("origin", "")
    if urlsplit(origin).netloc != websocket.headers.get("host"):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    session = None
    key = str(token)
    try:
        command = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        if command.get("action") not in {"start", "resume"}:
            await websocket.close(code=1008)
            return
        session = folder_picker_service.attach(
            key,
            str(workspace_service.root),
            start=command["action"] == "start",
        )
        if session is None:
            await websocket.send_json({"status": "missing"})
            return
        previous = None
        while True:
            if session.result != previous:
                await websocket.send_json(session.result)
                previous = dict(session.result)
            try:
                command = await asyncio.wait_for(
                    websocket.receive_json(), timeout=1
                )
            except asyncio.TimeoutError:
                continue
            if command.get("action") == "cancel":
                try:
                    await folder_picker_service.cancel(session)
                except Exception:
                    logger.exception("Could not cancel folder picker")
                    await websocket.send_json(
                        {
                            "status": "cancel-error",
                            "detail": "Could not close folder picker. Try Cancel again.",
                        }
                    )
    except PickerReconnectingError:
        await websocket.send_json({"status": "reconnecting"})
    except WebSocketDisconnect:
        pass
    except (asyncio.TimeoutError, ValueError):
        await websocket.close(code=1008)
    except Exception:
        logger.exception("Folder picker connection failed")
        try:
            await websocket.send_json(
                {
                    "status": "error",
                    "detail": "Could not connect to folder picker. Enter the path manually.",
                }
            )
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        if session is not None:
            folder_picker_service.detach(key, session)
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass


@app.post("/api/read-file")
async def read_file(payload: ReadFileRequest):
    """Read a workspace text file and translate access failures into HTTP errors."""
    try:
        return {
            "path": payload.path,
            "content": workspace_service.read_text(payload.path),
        }
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.")
    except PermissionError:
        logger.warning(
            "Permission denied reading workspace file: %s", payload.path
        )
        raise HTTPException(status_code=403, detail="Permission denied.")
    except WorkspaceAccessError as exc:
        logger.warning("Workspace access rejected: %s", payload.path)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Unexpected workspace read error")
        raise HTTPException(status_code=500, detail="Could not read file.")


@app.post("/api/chat")
async def chat(payload: ChatRequest):
    """Prepare a versioned conversation turn and return its server-sent response stream."""
    try:
        profile_id = ai_service.resolve_profile_id(payload.profile_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background_tasks = BackgroundTasks()
    if payload.regenerate_message_index is None:
        conversation = conversation_manager.get_or_create(
            payload.conversation_id
        )
        history_end = len(conversation.messages)
        user_prompt = payload.prompt
    else:
        conversation = conversation_manager.get(payload.conversation_id or "")
        if not conversation:
            raise HTTPException(
                status_code=404, detail="Conversation not found."
            )
        if (
            payload.expected_conversation_version is not None
            and payload.expected_conversation_version != conversation.version
        ):
            raise HTTPException(
                status_code=409,
                detail="Conversation changed. Reload and retry regeneration.",
            )
        if payload.regenerate_message_index >= len(conversation.messages):
            raise HTTPException(status_code=400, detail="Message not found.")
        source_message = conversation.messages[
            payload.regenerate_message_index
        ]
        if source_message.role != "user":
            raise HTTPException(
                status_code=400,
                detail="Only a user message can be regenerated.",
            )
        history_end = payload.regenerate_message_index
        user_prompt = source_message.content
    workspace_context = workspace_service.project_context()
    if payload.workspace_file:
        try:
            workspace_context += f"\n\nSelected file: {payload.workspace_file}\n{workspace_service.read_text(payload.workspace_file)}"
        except (
            FileNotFoundError,
            PermissionError,
            WorkspaceAccessError,
        ) as exc:
            raise HTTPException(
                status_code=400, detail=f"Cannot load workspace file: {exc}"
            )

    try:
        instructions, input_messages = await memory_service.prepare_context(
            conversation,
            user_prompt,
            workspace_context,
            profile_id,
            history_end=history_end,
            persist_memory=payload.regenerate_message_index is None,
        )
    except ContextBudgetExceeded as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ContextChangedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SummaryUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if payload.regenerate_message_index is None:
        conversation_manager.add_message(conversation, "user", user_prompt)
    turn_version = conversation.version
    turn_messages = conversation.messages

    async def events():
        """Stream reply events, save a completed reply, and schedule memory compaction."""
        answer = ""
        usage_data = None
        usage_call_id = None
        profile_data = None
        completed = False
        request_instructions, request_messages = instructions, input_messages
        try:
            yield f"data: {json.dumps({'type': 'start', 'conversation_id': conversation.id})}\n\n"
            for attempt in range(2):
                try:
                    async with aclosing(ai_service.stream(
                        request_instructions,
                        request_messages,
                        profile_id=profile_id,
                        conversation_id=conversation.id,
                        call_kind=(
                            "regeneration" if payload.regenerate_message_index
                            is not None else "chat"
                        ),
                    )) as reply_stream:
                        async for event in reply_stream:
                            if event["type"] == "delta":
                                answer += event["text"]
                            elif event["type"] == "usage":
                                usage_data = event.get("usage")
                                profile_data = event.get("profile")
                                usage_call_id = event.get("call_id")
                            elif event["type"] == "done":
                                completed = True
                                continue
                            yield f"data: {json.dumps(event)}\n\n"
                    break
                except ContextWindowExceeded:
                    # Retry a confirmed context-limit error only before streaming text, using a smaller input.
                    if (
                        attempt
                        or answer
                        or usage_data is not None
                        or completed
                    ):
                        raise
                    if conversation.version != turn_version:
                        raise ContextChangedError(
                            "Conversation changed before context recovery. Please retry."
                        )
                    reduced_limit = (
                        context_budget.estimate(
                            request_instructions, request_messages
                        )
                        * 3
                        // 4
                    )
                    request_instructions, request_messages = (
                        await memory_service.prepare_context(
                            conversation,
                            user_prompt,
                            workspace_context,
                            profile_id,
                            history_end=history_end,
                            persist_memory=payload.regenerate_message_index
                            is None,
                            input_limit=reduced_limit,
                        )
                    )

            if not completed:
                raise RuntimeError("Response stream ended before completion.")
            msg_kwargs = {}
            if usage_call_id is not None:
                msg_kwargs["usage_call_id"] = usage_call_id
            if profile_data:
                msg_kwargs["profile_id"] = profile_data.get("id")
                msg_kwargs["profile_name"] = profile_data.get("name")
                msg_kwargs["deployment"] = profile_data.get("deployment")
            if usage_data:
                msg_kwargs["input_tokens"] = usage_data.get("input_tokens")
                msg_kwargs["output_tokens"] = usage_data.get("output_tokens")
                msg_kwargs["total_tokens"] = usage_data.get("total_tokens")
                msg_kwargs["response_time_ms"] = usage_data.get(
                    "response_time_ms"
                )
                msg_kwargs["estimated_cost"] = usage_data.get("estimated_cost")
                msg_kwargs["is_long_context"] = bool(
                    usage_data.get("is_long_context", False)
                )
                for name in (
                    "uncached_input_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "usage_status",
                    "cost_status",
                    "provider_usage",
                ):
                    msg_kwargs[name] = usage_data.get(name)

            if payload.regenerate_message_index is None:
                if conversation.messages is not turn_messages:
                    raise ContextChangedError(
                        "Conversation changed during generation. Please retry."
                    )
                conversation_manager.add_message(
                    conversation, "assistant", answer, **msg_kwargs
                )
            else:
                conversation_manager.replace_reply(
                    conversation,
                    payload.regenerate_message_index,
                    answer,
                    expected_messages=turn_messages,
                    **msg_kwargs,
                )
            background_tasks.add_task(
                memory_service.compact_if_needed,
                conversation,
                profile_id=profile_id,
            )
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except ConversationUnavailableError:
            logger.info(
                "Discarded response for an unavailable conversation: %s",
                conversation.id,
            )
        except Exception as exc:
            logger.exception("Chat stream failed")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return ChatStreamingResponse(
        events(),
        media_type="text/event-stream",
        background=background_tasks,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ChatStreamingResponse(StreamingResponse):
    """Close the reply generator deterministically when an ASGI client disconnects."""

    async def __call__(self, scope, receive, send) -> None:
        """Shield final generator cleanup from the streaming task's cancellation scope."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_excludes=["logs/*", "conversation/*", "workspace/*"],
    )
