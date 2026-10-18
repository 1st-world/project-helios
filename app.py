"""Serve the Helios web client and API, connecting application services and managing their lifecycle."""

import asyncio
import json
import logging
import mimetypes
import sqlite3
from copy import copy
from contextlib import aclosing, asynccontextmanager
from dataclasses import replace
from datetime import date
from logging.handlers import RotatingFileHandler
from typing import ClassVar, Literal
from urllib.parse import urlsplit
from uuid import UUID

import anyio
from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.requests import ClientDisconnect

from config import settings
from models.app_settings import AppSettings, DEFAULT_APP_SETTINGS
from models.attachment import (
    ATTACHMENT_SOURCES, AttachmentPath, AttachmentSource, WorkspaceAttachment,
)
from models.input_budget import normalize_input_budget_settings
from models.profile import (
    PROFILE_POLICY_FIELDS, DEFAULT_LONG_CONTEXT_THRESHOLD, ConnectionProfile,
    normalize_output_limit_settings,
)
from services.ai_service import AIService
from services.attachment_errors import ProviderAttachmentError
from services.attachment_service import (
    AttachmentError, AttachmentService, PreparedAttachments,
)
from services.attachment_store import AttachmentStore
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
from services.context_policy import ContextPolicy
from services.call_usage_store import CallUsageStore
from services.work_run_store import WorkRunStore
from services.folder_picker_service import (
    FolderPickerService,
    PickerReconnectingError,
)
from services.context_recovery import ContextRecoveryRegistry, RecoveryError
from services.memory_service import (
    ConversationMemoryService, MemoryPolicyChangedError,
)
from services.local_work import check_local_cancelled, run_local
from services.prompt_builder import PromptBuilder
from services.profile_service import (
    ProfileNotFoundError, ProfileService, ProfileStorageError,
)
from services.provider_errors import ConnectionConfigurationError, ProviderFailure
from services.settings_service import SettingsService, SettingsStorageError
from services.summary_usage_store import SummaryUsageStore
from services.usage_service import UsageService
from services.token_estimator import estimation_metadata
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

settings_service = SettingsService(
    getattr(
        settings, "app_settings_path",
        settings.profiles_path.with_name("settings.json"),
    ),
    AppSettings(**{
        name: getattr(settings, name, value)
        for name, value in DEFAULT_APP_SETTINGS.to_dict().items()
    }),
)
application_settings = settings_service.current
profile_service = ProfileService(
    settings.profiles_path,
    app_settings=(settings_service.migration_origin or settings_service.current)
    if settings_service.source_exists else None,
)
conversation_manager = ConversationManager(settings.conversations_root)
workspace_service = WorkspaceService(settings.workspace_root)
attachment_store = AttachmentStore(getattr(
    settings, "attachments_root", settings.profiles_path.parent / "data" / "attachments",
))
attachment_service = AttachmentService(
    workspace_service,
    limits=application_settings.attachment_limits(), store=attachment_store,
)
folder_picker_service = FolderPickerService()
context_recovery = ContextRecoveryRegistry()
workspace_operations: set[object] = set()
usage_service = UsageService()
summary_usage_store = SummaryUsageStore(
    settings.logs_root / "summary_usage.sqlite3"
)
call_usage_store = CallUsageStore(settings.logs_root / "usage_calls.sqlite3")
work_run_store = WorkRunStore(getattr(
    settings, "work_runs_path", settings.profiles_path.parent / "data" / "work_runs.sqlite3",
))
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
    work_run_store=work_run_store,
    max_output_tokens=application_settings.context_output_reserve,
)
context_budget = ContextBudget(
    application_settings.context_token_budget,
    application_settings.context_output_reserve,
    application_settings.max_summary_calls,
    reserve_output=False,
)
memory_service = ConversationMemoryService(
    ai_service,
    conversation_manager,
    application_settings.summary_trigger_tokens,
    application_settings.keep_recent_tokens,
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


# Serve WOFF2 consistently when the host does not register its MIME type.
mimetypes.add_type("font/woff2", ".woff2")

app = FastAPI(
    title="Helios BYOK AI Assistant", version="0.2.0", lifespan=lifespan,
)
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
        conversation = (
            conversation_manager.get(conversation_id)
            if conversation_id is not None else None
        )
        report = call_usage_store.totals(
            conversation_id=conversation_id, kind=kind,
            timezone_name=timezone, start_date=start_date, end_date=end_date,
            conversation_created_at=(
                conversation.created_at if conversation is not None else None
            ),
        )
        report["revision"] = call_usage_store.revision(conversation_id)
        return report
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.websocket("/api/usage/changes")
async def call_usage_changes(
    websocket: WebSocket, revision: str, conversation_id: str | None = None,
):
    """Notify a same-origin browser when recorded usage changes over one connection."""
    origin = websocket.headers.get("origin", "")
    if urlsplit(origin).netloc != websocket.headers.get("host"):
        await websocket.close(code=1008)
        return
    await websocket.accept()

    async def wait_for_disconnect() -> None:
        """Release the usage observer when the browser closes its connection."""
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass

    update = None
    disconnected = asyncio.create_task(wait_for_disconnect())
    try:
        while not disconnected.done():
            update = asyncio.create_task(call_usage_store.wait_for_change(
                revision, conversation_id,
            ))
            finished, _ = await asyncio.wait(
                (update, disconnected), return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnected in finished:
                break
            current = update.result()
            if current != revision:
                revision = current
                await websocket.send_json({"revision": revision})
    except WebSocketDisconnect:
        pass
    finally:
        if update is not None:
            update.cancel()
        disconnected.cancel()
        pending = [disconnected] if update is None else [update, disconnected]
        await asyncio.gather(*pending, return_exceptions=True)
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass


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
    workspace_revision: int | None = Field(default=None, ge=0, strict=True)
    workspace_session_id: str | None = Field(default=None, min_length=1, max_length=64)


class ContextRequestOptions(BaseModel):
    """Allow request-level estimate policy overrides without changing profile settings."""

    allow_partial_input_budget: ClassVar[bool] = False
    context_preflight_mode: Literal["warn", "block", "off"] | None = None
    context_input_budget: int | None = Field(default=None, ge=1, strict=True)
    context_input_budget_mode: Literal["inherit", "unlimited", "custom"] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_input_budget(cls, values: object) -> object:
        """Preserve numeric request compatibility without exposing complete request bodies."""
        if not isinstance(values, dict):
            return values
        try:
            normalized = normalize_input_budget_settings(values)
            if (
                not cls.allow_partial_input_budget
                and normalized.get("context_input_budget_mode") == "custom"
                and normalized.get("context_input_budget") is None
            ):
                raise ValueError("A custom input budget requires a positive token limit.")
            return normalized
        except ValueError as exc:
            raise ValidationError.from_exception_data(cls.__name__, [{
                "type": "value_error", "loc": ("context_input_budget",),
                "input": values.get("context_input_budget"), "ctx": {"error": exc},
            }]) from exc


class UpdateSettingsRequest(BaseModel):
    """Validate partial app preferences while allowing null to restore defaults."""

    model_config = ConfigDict(extra="forbid")
    summary_trigger_tokens: int | None = Field(default=None, ge=0, strict=True)
    keep_recent_tokens: int | None = Field(default=None, ge=1, strict=True)
    context_token_budget: int | None = Field(default=None, ge=1, strict=True)
    context_output_reserve: int | None = Field(default=None, ge=1, strict=True)
    max_summary_calls: int | None = Field(default=None, ge=1, strict=True)
    context_preflight_mode: Literal["warn", "block", "off"] | None = None
    context_input_budget_mode: Literal["unlimited", "custom", "legacy"] | None = None
    context_input_budget: int | None = Field(default=None, ge=1, strict=True)
    attachment_max_files: int | None = Field(default=None, ge=1, strict=True)
    attachment_max_file_bytes: int | None = Field(
        default=None, ge=1, strict=True
    )
    attachment_max_total_bytes: int | None = Field(
        default=None, ge=1, strict=True
    )
    attachment_max_text_bytes: int | None = Field(
        default=None, ge=1, strict=True
    )
    attachment_max_zip_bytes: int | None = Field(
        default=None, ge=1, strict=True
    )
    attachment_max_zip_members: int | None = Field(
        default=None, ge=1, strict=True
    )
    attachment_max_image_pixels: int | None = Field(
        default=None, ge=1, strict=True
    )
    attachment_max_pdf_pages: int | None = Field(
        default=None, ge=1, strict=True
    )
    attachment_max_sheet_cells: int | None = Field(
        default=None, ge=1, strict=True
    )


class WorkspaceStructureRequest(BaseModel):
    """Select the relative scope and visible range of an optional project manifest."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(default="", max_length=4096)
    max_depth: int | None = Field(default=None, ge=1, strict=True)
    max_entries: int | None = Field(default=None, ge=1, strict=True)
    include_absolute_path: bool = Field(default=False, strict=True)


class WorkspaceStructurePreviewRequest(WorkspaceStructureRequest):
    """Pin a local structure preview to the workspace shown in the browser."""

    workspace_revision: int | None = Field(default=None, ge=0, strict=True)
    workspace_session_id: str | None = Field(default=None, min_length=1, max_length=64)


class ChatRequest(ContextRequestOptions):
    """Validate a new chat turn or regeneration request and its context selection."""

    prompt: str = Field(min_length=1)
    conversation_id: str | None = None
    workspace_file: str | None = None
    workspace_files: list[str] = Field(default_factory=list)
    attachment_files: list[AttachmentPath] = Field(default_factory=list)
    attachment_sources: list[AttachmentSource] | None = None
    workspace_revision: int | None = Field(default=None, ge=0, strict=True)
    workspace_session_id: str | None = Field(default=None, min_length=1, max_length=64)
    workspace_structure: WorkspaceStructureRequest | None = None
    profile_id: str | None = None
    regenerate_message_index: int | None = Field(default=None, ge=0)
    expected_conversation_version: int | None = Field(default=None, ge=0)
    recovery_id: UUID | None = None
    operation_id: UUID | None = None

    @model_validator(mode="after")
    def validate_recovery_operation(self):
        """Require an explicit operation identifier alongside a recovery receipt."""
        if (self.recovery_id is None) != (self.operation_id is None):
            raise ValueError("Recovery and operation identifiers must be supplied together.")
        return self


class AttachmentSelectionRequest(ContextRequestOptions):
    """Validate files for the backend attachment API without changing GUI selection."""

    paths: list[AttachmentPath] = Field(default_factory=list)
    attachment_sources: list[AttachmentSource] = Field(default_factory=list)
    profile_id: str | None = None
    workspace_revision: int | None = Field(default=None, ge=0, strict=True)
    workspace_session_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def require_selection(self):
        """Require at least one legacy path or explicit source reference."""
        if not self.paths and not self.attachment_sources:
            raise ValueError("Select at least one attachment.")
        return self


class RenameConversationRequest(BaseModel):
    """Validate the requested conversation title."""

    title: str = Field(min_length=1, max_length=100)


class EditMessageRequest(BaseModel):
    """Validate replacement content for a user message."""

    content: str = Field(min_length=1)


class WorkspaceRootRequest(BaseModel):
    """Validate the submitted workspace folder path."""

    path: str | None = Field(min_length=1, max_length=4096)
    workspace_revision: int | None = Field(default=None, ge=0, strict=True)
    workspace_session_id: str | None = Field(default=None, min_length=1, max_length=64)
    include_entries: bool = Field(default=True, strict=True)


class ProfileContextSettings(ContextRequestOptions):
    """Validate separate user limits and model declarations with legacy cap aliases."""

    allow_partial_input_budget: ClassVar[bool] = True
    output_token_limit: int | None = Field(default=None, ge=1, strict=True)
    model_context_window: int | None = Field(default=None, ge=1, strict=True)
    model_max_input_tokens: int | None = Field(default=None, ge=1, strict=True)
    declared_model_max_output_tokens: int | None = Field(default=None, ge=1, strict=True)
    context_input_budget_mode: Literal["inherit", "unlimited", "custom", "legacy"] | None = None
    memory_enabled: bool | None = Field(default=False, strict=True)
    memory_output_headroom: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_output_limit(cls, values: object) -> object:
        """Keep legacy output cap requests compatible without reinterpreting metadata."""
        if not isinstance(values, dict):
            return values
        try:
            return normalize_output_limit_settings(values)
        except ValueError as exc:
            # Field-scoped errors must not echo credentials from the full profile body.
            raise ValidationError.from_exception_data(cls.__name__, [{
                "type": "value_error",
                "loc": ("output_token_limit",),
                "input": values.get("output_token_limit"),
                "ctx": {"error": exc},
            }]) from exc


class CreateProfileRequest(ProfileContextSettings):
    """Validate connection credentials, deployment, and optional pricing for a profile."""

    name: str = Field(min_length=1, max_length=100)
    endpoint: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(min_length=1, max_length=1024)
    deployment: str = Field(min_length=1, max_length=256)
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)
    long_context_threshold: int | None = Field(
        default=DEFAULT_LONG_CONTEXT_THRESHOLD, ge=1,
    )
    long_input_price_per_million: float | None = Field(default=None, ge=0)
    long_output_price_per_million: float | None = Field(default=None, ge=0)
    cache_read_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cache_write_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    long_cache_read_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    long_cache_write_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class UpdateProfileRequest(ProfileContextSettings):
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
            **{name: getattr(payload, name) for name in PROFILE_POLICY_FIELDS},
        )
    except ProfileStorageError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("Could not create profile")
        raise HTTPException(
            status_code=400, detail="Could not create connection profile."
        )
    memory_service.invalidate_policy()
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
            context_settings={
                name: getattr(payload, name) for name in PROFILE_POLICY_FIELDS
                if name in payload.model_fields_set
            },
        )
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProfileStorageError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("Could not update profile %s", profile_id)
        raise HTTPException(
            status_code=400, detail="Could not update connection profile."
        )
    memory_service.invalidate_policy()
    return profile_service.get_summary()


@app.exception_handler(ProfileStorageError)
async def profile_storage_failure(_: Request, exc: ProfileStorageError):
    """Report a failed or conflicting save without echoing connection data."""
    conflict = exc.reason in {"conflict", "locked"}
    return JSONResponse(status_code=409 if conflict else 500, content={"detail": {
        "code": "profiles_storage_conflict" if conflict else "profiles_save_failed",
        "message": str(exc),
    }})


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    """Delete a connection profile or report that it does not exist."""
    deleted = profile_service.delete_profile(profile_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Profile not found.")
    memory_service.invalidate_policy()
    return profile_service.get_summary()


@app.post("/api/profiles/{profile_id}/active")
async def set_active_profile(profile_id: str):
    """Select an existing connection profile for subsequent requests."""
    try:
        profile_service.set_active_profile(profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    memory_service.invalidate_policy()
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


async def until_disconnect(request, action, *args, **kwargs):
    """Cancel preparation on disconnect and join its local workers before returning."""
    if request is None:
        return await action(*args, **kwargs)
    result = None
    failure = None
    disconnected = False
    async with anyio.create_task_group() as tasks:
        async def watch_disconnect():
            """Observe the consumed request body until preparation hands off the connection."""
            nonlocal disconnected
            while True:
                event = await request.receive()
                if event["type"] == "http.disconnect":
                    disconnected = True
                    tasks.cancel_scope.cancel()
                    return

        tasks.start_soon(watch_disconnect)
        try:
            await anyio.lowlevel.checkpoint()
            result = await action(*args, **kwargs)
            await anyio.lowlevel.checkpoint()
        except Exception as exc:
            failure = exc
        finally:
            tasks.cancel_scope.cancel()
    if disconnected:
        if isinstance(result, StreamingResponse):
            await result.body_iterator.aclose()
        raise HTTPException(status_code=499, detail="Request cancelled.")
    if failure is not None:
        raise failure
    return result


def require_workspace_root(root, revision: int | None = None) -> None:
    """Reject local results prepared for a workspace that is no longer selected."""
    if (
        workspace_service.root != root
        or (revision is not None and workspace_service.revision != revision)
    ):
        raise HTTPException(status_code=409, detail={
            "code": "attachment_workspace_changed",
            "message": "Workspace changed while preparing this request. Try again.",
        })


def require_workspace_revision(
    revision: int | None, session_id: str | None = None,
) -> None:
    """Reject stale browser references even after a folder is selected again."""
    require_workspace_root(workspace_service.root, revision)
    if session_id is not None and workspace_service.session_id != session_id:
        raise HTTPException(status_code=409, detail={
            "code": "attachment_workspace_changed",
            "message": "Workspace session changed. Refresh the Workspace and select project input again.",
        })


def require_selected_workspace(workspace: WorkspaceService) -> None:
    """Require an explicit folder before interpreting project-relative input."""
    if workspace.root is None:
        raise HTTPException(status_code=400, detail={
            "code": "workspace_not_selected",
            "message": "Choose a Workspace folder before using project files or folder structure.",
        })


def claim_workspace(workspace: WorkspaceService):
    """Keep folder changes blocked until project work and its cleanup finish."""
    token = object()
    if workspace.root is not None:
        workspace_operations.add(token)

    def release() -> None:
        """Release an operation idempotently after streaming or local worker cleanup."""
        workspace_operations.discard(token)

    return release


def require_workspace_idle() -> None:
    """Leave the selected folder intact while a project request still owns it."""
    if workspace_operations:
        raise HTTPException(status_code=409, detail={
            "code": "workspace_request_in_progress",
            "message": "A project request is running. Stop it and wait for cleanup before changing the Workspace.",
        })


def workspace_payload(workspace: WorkspaceService, entries: list[dict] | None) -> dict:
    """Represent an unselected folder as null without a fallback path."""
    payload = {
        "root": str(workspace.root) if workspace.root is not None else None,
        "revision": workspace.revision,
        "session_id": workspace.session_id,
        "entries": entries if entries is not None else [],
    }
    if entries is None:
        payload["entries_loaded"] = False
    return payload


def build_workspace_structure(
    workspace: WorkspaceService, options: WorkspaceStructureRequest,
) -> str:
    """Build only the explicitly selected folder-structure context."""
    return workspace.project_context(
        relative_path=options.path, max_depth=options.max_depth,
        max_entries=options.max_entries,
        include_absolute_path=options.include_absolute_path,
    )


def chat_uses_workspace(payload: ChatRequest) -> bool:
    """Distinguish explicit project input from a folder-independent chat request."""
    return bool(
        payload.workspace_file or payload.workspace_files
        or payload.attachment_files or payload.workspace_structure is not None
        or any(source.source == "workspace" for source in payload.attachment_sources or [])
    )


@app.get("/api/workspace")
async def workspace_tree(request: Request = None, include_entries: bool = True):
    """Return folder metadata and list files only when the caller requests them."""
    workspace = copy(workspace_service)
    entries = (
        await until_disconnect(request, run_local, workspace.tree)
        if include_entries else None
    )
    require_workspace_root(workspace.root, workspace.revision)
    return workspace_payload(workspace, entries)


@app.put("/api/workspace/root")
async def update_workspace_root(
    payload: WorkspaceRootRequest, request: Request = None
):
    """Select or disconnect a folder once all project requests have finished."""
    require_workspace_revision(payload.workspace_revision, payload.workspace_session_id)
    require_workspace_idle()
    workspace = copy(workspace_service)
    previous_root = workspace.root
    previous_revision = workspace.revision

    def select_workspace():
        """Validate a candidate and optionally list it before publishing its root."""
        workspace.set_root(payload.path)
        return workspace.tree() if payload.include_entries else None

    try:
        entries = await until_disconnect(request, run_local, select_workspace)
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    require_workspace_root(previous_root, previous_revision)
    require_workspace_idle()
    workspace_service.root = workspace.root
    workspace_service.revision = workspace.revision
    logger.info("Workspace root changed to %s", workspace_service.root)
    return workspace_payload(workspace_service, entries)


@app.post("/api/workspace/structure")
async def preview_workspace_structure(
    payload: WorkspaceStructurePreviewRequest, request: Request = None,
):
    """Preview the exact selected structure locally without reading file bodies."""
    require_workspace_revision(payload.workspace_revision, payload.workspace_session_id)
    workspace = copy(workspace_service)
    require_selected_workspace(workspace)
    release = claim_workspace(workspace)
    try:
        context = await until_disconnect(
            request, run_local, build_workspace_structure, workspace, payload,
        )
        require_workspace_root(workspace.root, workspace.revision)
        return {
            "context": context, "root": str(workspace.root),
            "revision": workspace.revision,
            "session_id": workspace.session_id,
        }
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        release()


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
            str(workspace_service.root) if workspace_service.root is not None else "",
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
async def read_file(payload: ReadFileRequest, request: Request = None):
    """Read a workspace text file and translate access failures into HTTP errors."""
    require_workspace_revision(payload.workspace_revision, payload.workspace_session_id)
    workspace = copy(workspace_service)
    require_selected_workspace(workspace)
    release = claim_workspace(workspace)
    try:
        content = await until_disconnect(
            request, run_local, workspace.read_text, payload.path,
        )
        require_workspace_root(workspace.root, workspace.revision)
        return {
            "path": payload.path,
            "content": content,
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
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected workspace read error")
        raise HTTPException(status_code=500, detail="Could not read file.")
    finally:
        release()


def application_settings_response() -> dict:
    """Return saved preferences, reset defaults, and separately enforced limits."""
    return {
        "settings": settings_service.current.to_dict(),
        "defaults": settings_service.defaults.to_dict(),
        "local_attachment_limits": {
            name: getattr(attachment_service, name)
            for name in vars(AttachmentService) if name.startswith("MAX_")
        },
        "provider_limits": {"enforcement": "provider"},
        "summary_policy": memory_service.summary_policy(),
        "applies_to": "new_requests",
    }


@app.get("/api/settings")
async def get_application_settings():
    """Expose application preferences for future Settings GUI integration."""
    return application_settings_response()


@app.put("/api/settings")
async def update_application_settings(payload: UpdateSettingsRequest):
    """Persist an update before publishing a new immutable request policy."""
    global attachment_service, ai_service, context_budget, memory_service
    try:
        candidate = settings_service.prepare_update(
            payload.model_dump(exclude_unset=True)
        )
        new_budget = ContextBudget(
            candidate.context_token_budget, candidate.context_output_reserve,
            candidate.max_summary_calls,
            reserve_output=False,
        )
        new_attachments = AttachmentService(
            workspace_service, limits=candidate.attachment_limits(), store=attachment_store,
        )
        new_ai = ai_service.with_output_limit(candidate.context_output_reserve)
        new_memory = memory_service.with_settings(
            new_ai, new_budget,
            candidate.summary_trigger_tokens, candidate.keep_recent_tokens,
        )
        settings_service.save(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "settings_invalid", "message": str(exc),
        }) from exc
    except SettingsStorageError as exc:
        logger.exception("Could not save application settings")
        conflict = exc.reason in {"conflict", "locked"}
        raise HTTPException(status_code=409 if conflict else 500, detail={
            "code": (
                "settings_storage_conflict" if conflict else "settings_save_failed"
            ),
            "message": str(exc),
        }) from exc
    # No await separates disk publication from the event-loop policy swap.
    new_memory.invalidate_policy()
    attachment_service, ai_service, context_budget, memory_service = (
        new_attachments, new_ai, new_budget, new_memory,
    )
    return application_settings_response()


def validate_selection_entries(
    paths: list[str], limits: AttachmentService
) -> None:
    """Bound raw path aliases using the resource policy captured for a request."""
    if len(paths) > max(200, limits.MAX_FILES * 4):
        raise HTTPException(status_code=422, detail={
            "code": "attachment_selection_too_large",
            "message": "Too many file selection entries.",
        })


def resolve_context_policy(
    profile_id: str | None = None,
    mode: str | None = None,
    input_budget: int | None = None,
    *,
    budget: ContextBudget | None = None,
    default_mode: str | None = None,
    input_budget_mode: str | None = None,
    app_settings: AppSettings | None = None,
    profile_data: ConnectionProfile | None = None,
) -> ContextPolicy:
    """Resolve defaults and optional profile settings without contacting the provider."""
    profile = profile_data or (
        profile_service.get_profile(profile_id)
        if profile_id else profile_service.get_active_profile()
    )
    if profile_id and profile is None:
        raise HTTPException(status_code=404, detail="Profile not found.")
    current = app_settings or settings_service.current
    try:
        return ContextPolicy.resolve(
            profile or ConnectionProfile(), budget or context_budget,
            default_mode or current.context_preflight_mode,
            mode, input_budget, input_budget_mode=input_budget_mode,
            default_input_budget_mode=current.context_input_budget_mode,
            default_input_budget=current.context_input_budget,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/context-policy")
async def context_policy_settings(profile_id: str | None = None):
    """Expose effective estimate and local resource settings for future GUI integration."""
    policy = resolve_context_policy(profile_id)
    return {
        **estimation_metadata(
            enabled=policy.mode != "off" and policy.input_budget is not None,
        ),
        "mode": policy.mode, "input_budget": policy.input_budget,
        "input_budget_source": policy.input_budget_source,
        "legacy_input_policy": policy.legacy_input_policy,
        "declared_model_input_limit": policy.declared_model_input_limit,
        "max_output_tokens": policy.max_output_tokens,
        "declared_model_max_output_tokens": policy.declared_model_max_output_tokens,
        "summary_policy": memory_service.summary_policy(profile_id),
        "local_attachment_limits": {
            name: getattr(attachment_service, name)
            for name in vars(AttachmentService) if name.startswith("MAX_")
        },
    }


@app.exception_handler(AttachmentError)
async def attachment_error_response(_: Request, exc: AttachmentError):
    """Return source errors without exposing original bytes or internal paths."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail()})


async def attachment_storage_work(request, action, *args):
    """Run storage access off the event loop and sanitize operating-system failures."""
    try:
        return await until_disconnect(request, run_local, action, *args)
    except (OSError, sqlite3.Error):
        raise AttachmentError(
            "attachment_storage_unavailable", "Attachment storage could not be accessed. Check local storage and try again.", "", 500,
        ) from None


@app.post("/api/attachments/uploads", status_code=201)
async def upload_attachment(
    request: Request, filename: str = Query(min_length=1, max_length=255),
):
    """Receive bounded raw bytes and publish only a fully validated reusable original."""
    service = copy(attachment_service)
    upload = attachment_store.stage(
        filename, min(service.MAX_FILE_BYTES, service.MAX_TOTAL_BYTES),
    )
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isdecimal():
            raise HTTPException(status_code=400, detail="Invalid Content-Length.")
        if int(length) > upload.max_bytes:
            raise AttachmentError(
                "attachment_too_large", f"Upload exceeds the local read limit of {upload.max_bytes} bytes.", filename, 413,
            )
    accepted = False
    try:
        await run_local(upload.open)
        async for chunk in request.stream():
            for offset in range(0, len(chunk), 65536):
                await run_local(upload.write, chunk[offset:offset + 65536])
        await run_local(upload.seal)
        metadata = await until_disconnect(request, run_local, service.validate_upload, upload)
        stored = await until_disconnect(request, run_local, upload.publish)
        accepted = True
        return {**stored, "inspection": metadata}
    except ClientDisconnect:
        raise HTTPException(status_code=499, detail="Upload cancelled.") from None
    except (OSError, sqlite3.Error):
        raise AttachmentError(
            "attachment_storage_unavailable", "The upload could not be stored. Check local storage and select the file again.", filename, 500,
        ) from None
    finally:
        with anyio.CancelScope(shield=True):
            await run_local(upload.abort, discard=not accepted)


@app.get("/api/attachments/uploads")
async def list_uploaded_attachments(request: Request = None):
    """List retained originals for reuse without loading their contents."""
    return {"uploads": await attachment_storage_work(request, attachment_store.list)}


@app.get("/api/attachments/uploads/{attachment_id}")
async def get_uploaded_attachment(attachment_id: UUID, request: Request = None):
    """Return one live upload's metadata using an opaque identifier."""
    return await attachment_storage_work(request, attachment_store.get, str(attachment_id))


@app.delete("/api/attachments/uploads/{attachment_id}")
async def delete_uploaded_attachment(attachment_id: UUID, request: Request = None):
    """Delete an idle original and discard recovery payloads that reference it."""
    await attachment_storage_work(request, attachment_store.delete, str(attachment_id))
    context_recovery.invalidate_attachment(str(attachment_id))
    return {"deleted": True}


@app.post("/api/attachments/inspect")
async def inspect_attachments(
    payload: AttachmentSelectionRequest, request: Request = None
):
    """Validate local attachments without provider calls or returning binary contents."""
    request_attachments = copy(attachment_service)
    request_attachments.workspace = copy(workspace_service)
    request_budget = context_budget
    request_settings = settings_service.current
    sources = [
        WorkspaceAttachment(source="workspace", path=path) for path in payload.paths
    ] + payload.attachment_sources
    workspace_bound = any(source.source == "workspace" for source in sources)
    if workspace_bound:
        require_workspace_revision(payload.workspace_revision, payload.workspace_session_id)
        require_selected_workspace(request_attachments.workspace)
    validate_selection_entries(sources, request_attachments)
    release = claim_workspace(request_attachments.workspace) if workspace_bound else None
    try:
        prepared = await until_disconnect(
            request, run_local,
            request_attachments.prepare_sources if payload.attachment_sources else request_attachments.prepare,
            sources if payload.attachment_sources else payload.paths,
        )
    except AttachmentError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail()
        ) from exc
    finally:
        if release is not None:
            release()
    if workspace_bound:
        require_workspace_root(
            request_attachments.workspace.root, request_attachments.workspace.revision,
        )
    policy = resolve_context_policy(
        payload.profile_id, payload.context_preflight_mode,
        payload.context_input_budget,
        budget=request_budget, app_settings=request_settings,
        input_budget_mode=payload.context_input_budget_mode,
    )
    assessment = policy.assess(
        "", [{"role": "user", "content": prepared.content}],
        visual_tokens=prepared.visual_tokens,
    )
    return {
        "files": prepared.metadata,
        "estimated_context_tokens": assessment["estimated_input_tokens"],
        "context_input_limit": policy.input_budget,
        "context_policy": assessment,
        "estimate_scope": "attachments_only; chat also includes the question, workspace, and history",
        "model_support": "validated_by_provider_on_chat",
    }


@app.post("/api/chat")
async def chat(payload: ChatRequest, request: Request = None):
    """Claim one conversation and honor explicit, one-use recovery authorization."""
    operation = None
    try:
        operation_id = str(payload.operation_id) if payload.operation_id else None
        ticket = (
            context_recovery.get(str(payload.recovery_id))
            if payload.recovery_id else None
        )
        if ticket is not None:
            data = ticket.data
            conversation = data["conversation"]
            if (
                not conversation_manager.is_active(conversation)
                or conversation.messages is not data["messages"]
                or conversation.version != data["version"]
                or not data["memory"].policy_is_current(data["summary_policy"])
            ):
                raise RecoveryError("The conversation or connection policy changed after this failure. Review the current input and send a new request.")
            if data.get("workspace_root") is not None:
                require_workspace_root(
                    data["workspace_root"], data["workspace_revision"],
                )
            payload = ChatRequest(
                prompt=data["prompt"], conversation_id=conversation.id,
                profile_id=data["profile_id"],
                regenerate_message_index=data["user_index"],
                expected_conversation_version=data["version"],
                attachment_sources=data.get("attachment_sources"),
            )
        else:
            if chat_uses_workspace(payload):
                require_workspace_revision(payload.workspace_revision, payload.workspace_session_id)
                require_selected_workspace(workspace_service)
            ai_service.resolve_profile_id(payload.profile_id)
            conversation = (
                conversation_manager.get(payload.conversation_id or "")
                if payload.regenerate_message_index is not None
                else conversation_manager.get_or_create(payload.conversation_id)
            )
            if conversation is None:
                raise HTTPException(status_code=404, detail="Conversation not found.")
            index = payload.regenerate_message_index
            if (
                index is not None and index < len(conversation.messages)
                and payload.attachment_sources is None
                and conversation.messages[index].attachment_sources
            ):
                payload = payload.model_copy(update={
                    "attachment_sources": ATTACHMENT_SOURCES.validate_python(
                        conversation.messages[index].attachment_sources,
                    ),
                })
            if chat_uses_workspace(payload):
                require_workspace_revision(payload.workspace_revision, payload.workspace_session_id)
                require_selected_workspace(workspace_service)
        operation = context_recovery.begin(
            conversation.id, ticket=ticket,
            operation_id=operation_id,
        )
        workspace_bound = (
            data.get("workspace_bound", data.get("workspace_root") is not None)
            if ticket is not None else chat_uses_workspace(payload)
        )
        if workspace_bound:
            operation.release_workspace = claim_workspace(workspace_service)
        upload_ids = [
            str(source.attachment_id) for source in payload.attachment_sources or []
            if source.source == "upload"
        ]
        operation.release_attachments = attachment_store.claim(upload_ids)
        if ticket is not None and upload_ids:
            await until_disconnect(
                request, run_local, attachment_store.validate_originals, upload_ids,
            )
        return await until_disconnect(
            request, prepare_chat, payload, conversation, operation,
        )
    except RecoveryError as exc:
        if operation is not None:
            operation.close()
        raise HTTPException(status_code=exc.status, detail={
            "code": exc.code, "message": str(exc),
        }) from None
    except AttachmentError as exc:
        if operation is not None:
            operation.close()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from None
    except ConnectionConfigurationError as exc:
        if operation is not None:
            operation.close()
        raise HTTPException(status_code=400, detail={
            "code": "connection_not_configured", "category": "configuration",
            "message": str(exc),
        }) from None
    except HTTPException:
        if operation is not None:
            operation.close()
        raise
    except Exception as exc:
        if operation is not None:
            operation.close()
        logger.error("Chat preparation failed (error_type=%s)", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail=ProviderFailure("request_failed").detail(phase="preparation"),
        ) from None
    except BaseException:
        if operation is not None:
            operation.close()
        raise


async def prepare_chat(payload: ChatRequest, conversation, operation):
    """Prepare a versioned conversation turn and return its server-sent response stream."""
    ticket = operation.ticket
    data = ticket.data if ticket is not None else None
    request_ai = data["ai"] if data else ai_service
    request_memory = data["memory"] if data else memory_service
    request_attachments = copy(attachment_service)
    request_workspace = copy(workspace_service)
    workspace_bound = (
        data.get("workspace_bound", data.get("workspace_root") is not None)
        if data else chat_uses_workspace(payload)
    )
    request_attachments.workspace = request_workspace
    request_budget = context_budget
    request_settings = settings_service.current
    validate_selection_entries(
        payload.workspace_files + payload.attachment_files + (payload.attachment_sources or [])
        + ([payload.workspace_file] if payload.workspace_file else []),
        request_attachments,
    )
    profile_id = request_ai.resolve_profile_id(payload.profile_id)
    pinned_profile = (
        data["profile"] if data else profile_service.get_profile(profile_id)
    )
    pinned_profile = replace(pinned_profile) if pinned_profile is not None else None
    request_memory_policy = (
        data["summary_policy"] if data else request_memory.capture_policy(profile_id)
    )
    recovery_policy = (
        request_memory_policy if data
        else request_memory.capture_policy(profile_id, one_shot=True)
    )
    background_tasks = BackgroundTasks()
    if payload.regenerate_message_index is None:
        history_end = len(conversation.messages)
        user_prompt = payload.prompt
    else:
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
    request_messages = conversation.messages
    if data:
        workspace_context, attachments = data["workspace_context"], data["attachments"]
    else:
        workspace_root = request_workspace.root
        selected_files = list(payload.workspace_files)
        if payload.workspace_file:
            selected_files.append(payload.workspace_file)

        def prepare_local_inputs():
            """Prepare only explicitly selected structure and files from a fixed root."""
            workspace_context = (
                build_workspace_structure(request_workspace, payload.workspace_structure)
                if payload.workspace_structure is not None else ""
            )
            if payload.attachment_sources is None:
                attachments = request_attachments.prepare(payload.attachment_files)
            else:
                attachments = request_attachments.prepare_sources([
                    WorkspaceAttachment(source="workspace", path=path)
                    for path in payload.attachment_files
                ] + payload.attachment_sources)
            attached_paths = {
                request_workspace._resolve(item["path"])
                for item in attachments.metadata if item["source"] == "workspace"
            }
            workspace_context += request_workspace.selected_context(
                selected_files, exclude_paths=attached_paths,
                max_file_bytes=min(
                    request_attachments.MAX_FILE_BYTES,
                    request_attachments.MAX_TEXT_BYTES,
                ),
                max_total_bytes=max(
                    0, request_attachments.MAX_TOTAL_BYTES - sum(
                        item["size_bytes"] for item in attachments.metadata
                    ),
                ),
                max_files=max(0, request_attachments.MAX_FILES - sum(
                    item["source"] == "upload" for item in attachments.metadata
                )),
            )
            check_local_cancelled()
            return workspace_context, attachments

        try:
            workspace_context, attachments = (
                await run_local(prepare_local_inputs) if workspace_bound or payload.attachment_sources
                else ("", PreparedAttachments())
            )
        except AttachmentError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=exc.detail()
            ) from exc
        except WorkspaceAccessError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if workspace_bound:
            require_workspace_root(workspace_root, request_workspace.revision)
    saved_attachment_sources = (
        data.get("attachment_sources") if data else (
            [item["reference"] for item in attachments.metadata]
            if payload.attachment_sources is not None else None
        )
    )
    if (
        not conversation_manager.is_active(conversation)
        or conversation.messages is not request_messages
    ):
        raise HTTPException(status_code=409, detail="Conversation changed while preparing input. Please retry.")
    if data:
        policy = data["context_policy"]
    else:
        policy = resolve_context_policy(
            profile_id, payload.context_preflight_mode, payload.context_input_budget,
            budget=request_budget, app_settings=request_settings,
            input_budget_mode=payload.context_input_budget_mode,
            profile_data=pinned_profile,
        )
    attachment_options = (
        {
            "attachment_content": attachments.content,
            "visual_tokens": attachments.visual_tokens,
        }
        if attachments.content else {}
    )
    try:
        original_context = PromptBuilder(work_run_store, conversation.id).build(
            [replace(message) for message in request_messages[:history_end]],
            user_prompt, workspace_context,
            attachment_content=attachments.content or None,
        )
        if data:
            instructions, input_messages = await request_memory.prepare_recovery(
                conversation, user_prompt, workspace_context, profile_id,
                history_end=history_end, summary_policy=request_memory_policy,
                input_limit=policy.input_budget if policy.mode == "block" else None,
                **attachment_options,
            )
        else:
            instructions, input_messages = await request_memory.prepare_context(
                conversation,
                user_prompt,
                workspace_context,
                profile_id,
                history_end=history_end,
                persist_memory=payload.regenerate_message_index is None,
                summary_policy=request_memory_policy,
                preflight_mode=policy.mode if policy.input_budget is not None else "off",
                input_limit=policy.input_budget,
                **attachment_options,
            )
    except ContextBudgetExceeded as exc:
        raise HTTPException(
            status_code=413,
            detail={
                "code": (
                    "context_recovery_unavailable" if data
                    else "context_preflight_blocked"
                ),
                "message": str(exc),
                "mode": policy.mode,
                "input_budget": policy.input_budget,
                "declared_model_input_limit": policy.declared_model_input_limit,
                **estimation_metadata(enabled=policy.mode != "off"),
            },
        ) from exc
    except ContextChangedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemoryPolicyChangedError:
        raise HTTPException(status_code=409, detail={
            "code": "context_recovery_unavailable",
            "message": "Connection or memory policy changed. The recovery request was stopped.",
        }) from None
    except ProviderFailure as exc:
        raise HTTPException(
            status_code=exc.http_status if exc.http_status and exc.http_status >= 400 else 502,
            detail=exc.detail(phase="summary"),
        ) from None
    except SummaryUnavailableError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "context_recovery_unavailable", "message": str(exc),
        }) from None
    except Exception:
        if not data:
            raise
        logger.warning("One-time recovery summary failed; no reply was submitted.")
        raise HTTPException(status_code=503, detail={
            "code": "context_recovery_unavailable",
            "message": "The one-time summary failed. No reply call was started; any summary usage remains recorded.",
        }) from None
    context_assessment = policy.assess(
        instructions, input_messages, visual_tokens=attachments.visual_tokens,
    )
    using_original_context = False

    def current_inputs() -> tuple[str, list[dict]]:
        """Bypass revoked memory immediately before submitting an unsent reply."""
        nonlocal context_assessment, using_original_context
        if (
            not conversation_manager.is_active(conversation)
            or conversation.messages is not turn_messages
        ):
            raise ContextChangedError("Conversation changed before the request was sent. Please retry.")
        if (
            not using_original_context
            and not request_memory.policy_is_current(request_memory_policy)
        ):
            if data:
                raise ContextChangedError("The recovery permission changed before submission. No further request was sent.")
            using_original_context = True
            context_assessment = policy.assess(
                *original_context, visual_tokens=attachments.visual_tokens,
            )
        if context_assessment["blocked"]:
            raise ContextBudgetExceeded("Original history exceeds the selected input budget after memory was stopped.")
        return original_context if using_original_context else (instructions, input_messages)

    if pinned_profile is not None and isinstance(request_ai, AIService):
        request_ai = request_ai.with_profile(pinned_profile, prepare_reply=current_inputs)
    await anyio.lowlevel.checkpoint()
    if payload.regenerate_message_index is None:
        user_message = conversation_manager.add_message(
            conversation, "user", user_prompt, attachment_sources=saved_attachment_sources,
        )
    else:
        user_message = conversation.messages[payload.regenerate_message_index]
    turn_messages = conversation.messages

    async def events():
        """Stream reply events, save a completed reply, and schedule memory compaction."""
        answer = ""
        usage_data = None
        usage_call_id = None
        profile_data = None
        completed = False
        reported_assessment = None

        def policy_events():
            """Report the final estimate again if memory revocation changed the actual input."""
            nonlocal reported_assessment
            if reported_assessment == context_assessment:
                return
            changed = reported_assessment is not None
            reported_assessment = dict(context_assessment)
            if (
                changed or attachments.metadata or context_assessment["warning"]
                or payload.context_preflight_mode is not None
                or payload.context_input_budget is not None
                or payload.context_input_budget_mode is not None
            ):
                event = {"type": "context_policy", **context_assessment}
                yield f"data: {json.dumps(event)}\n\n"
            if context_assessment["warning"]:
                warning = {
                    "type": "context_warning", "code": "context_estimate_exceeded",
                    "message": "The estimated input exceeds the selected input budget. The estimate is approximate; this request is allowed in warning mode.",
                }
                yield f"data: {json.dumps(warning)}\n\n"

        try:
            outgoing_instructions, outgoing_messages = current_inputs()
            start_event = {
                "type": "start", "conversation_id": conversation.id,
                "user_created_at": user_message.created_at.isoformat(),
            }
            yield f"data: {json.dumps(start_event)}\n\n"
            if attachments.metadata:
                metadata_event = {
                    "type": "attachments", "files": attachments.metadata,
                }
                yield f"data: {json.dumps(metadata_event)}\n\n"
            for notice in policy_events():
                yield notice
            outgoing_instructions, outgoing_messages = current_inputs()
            async with aclosing(request_ai.stream(
                outgoing_instructions,
                outgoing_messages,
                profile_id=profile_id,
                conversation_id=conversation.id,
                call_kind=(
                    "regeneration" if payload.regenerate_message_index
                    is not None else "chat"
                ),
            )) as reply_stream:
                async for event in reply_stream:
                    for notice in policy_events():
                        yield notice
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

            if not completed:
                raise RuntimeError("Response stream ended before completion.")
            if data and (
                conversation.version != data["version"]
                or not request_memory.policy_is_current(request_memory_policy)
            ):
                raise ContextChangedError("The recovery request became stale during generation. Its response was not saved; recorded API usage is retained.")
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
                saved_message = conversation_manager.add_message(
                    conversation, "assistant", answer, **msg_kwargs
                )
            else:
                saved_message = conversation_manager.replace_reply(
                    conversation,
                    payload.regenerate_message_index,
                    answer,
                    expected_messages=turn_messages,
                    user_attachment_sources=saved_attachment_sources,
                    **msg_kwargs,
                )
            if not data:
                background_tasks.add_task(
                    request_memory.compact_if_needed,
                    conversation,
                    profile_id=profile_id,
                    summary_policy=request_memory_policy,
                )
            done_event = {
                "type": "done", "created_at": saved_message.created_at.isoformat(),
            }
            yield f"data: {json.dumps(done_event)}\n\n"
        except ConversationUnavailableError:
            logger.info(
                "Discarded response for an unavailable conversation: %s",
                conversation.id,
            )
        except ContextBudgetExceeded as exc:
            for notice in policy_events():
                yield notice
            error = {
                "type": "error", "code": "context_preflight_blocked",
                "category": "preflight",
                "message": str(exc), "input_budget": policy.input_budget,
                **estimation_metadata(enabled=policy.mode != "off"),
            }
            yield f"data: {json.dumps(error)}\n\n"
        except ContextWindowExceeded as exc:
            for notice in policy_events():
                yield notice
            error = {"type": "error", **exc.detail()}
            if not data:
                offer = {"available": False}
                try:
                    if (
                        conversation.messages is not turn_messages
                        or not conversation_manager.is_active(conversation)
                        or not request_memory.policy_is_current(recovery_policy)
                    ):
                        raise ContextChangedError("The conversation or policy changed after this request.")
                    request_memory.recovery_plan(
                        request_messages[:history_end], user_prompt,
                        workspace_context, recovery_policy,
                        input_limit=(
                            policy.input_budget if policy.mode == "block" else None
                        ),
                        **attachment_options,
                        conversation_id=conversation.id,
                    )
                    user_index = (
                        payload.regenerate_message_index
                        if payload.regenerate_message_index is not None
                        else len(turn_messages) - 1
                    )
                    recovery_id = context_recovery.issue({
                        "conversation": conversation, "messages": turn_messages,
                        "version": conversation.version, "user_index": user_index,
                        "prompt": user_prompt, "profile_id": profile_id,
                        "profile": pinned_profile, "ai": request_ai,
                        "memory": request_memory, "summary_policy": recovery_policy,
                        "workspace_context": workspace_context,
                        "attachments": attachments, "context_policy": policy,
                        "attachment_sources": saved_attachment_sources,
                        "workspace_root": request_workspace.root if workspace_bound else None,
                        "workspace_revision": request_workspace.revision,
                        "workspace_bound": workspace_bound,
                    })
                    if recovery_id is None:
                        raise SummaryUnavailableError("This failed request is too large for the temporary recovery cache. Reduce the selected input or choose another deployment.")
                    offer = {
                        "available": True, "id": recovery_id,
                        "profile_name": (
                            pinned_profile.name if pinned_profile
                            else "Selected connection"
                        ),
                        "summary_call_limit": request_budget.max_summary_calls,
                        "expires_in_seconds": context_recovery.ttl,
                        "replaces_following": user_index + 1 < len(turn_messages),
                        "user_index": user_index,
                    }
                except (
                    ContextBudgetExceeded, SummaryUnavailableError,
                    ContextChangedError,
                ) as exc:
                    offer["reason"] = str(exc)
                error["recovery"] = offer
            yield f"data: {json.dumps(error)}\n\n"
        except ProviderAttachmentError as exc:
            for notice in policy_events():
                yield notice
            error = {
                "type": "error", **exc.detail(),
                "paths": [item["path"] for item in attachments.metadata],
            }
            yield f"data: {json.dumps(error)}\n\n"
        except ProviderFailure as exc:
            for notice in policy_events():
                yield notice
            error = {"type": "error", **exc.detail()}
            yield f"data: {json.dumps(error)}\n\n"
        except ContextChangedError as exc:
            error = {
                "type": "error", "message": str(exc),
                "code": (
                    "context_recovery_unavailable" if data else "conversation_changed"
                ),
            }
            yield f"data: {json.dumps(error)}\n\n"
        except Exception as exc:
            for notice in policy_events():
                yield notice
            logger.error("Chat stream failed (error_type=%s)", type(exc).__name__)
            error = {"type": "error", **ProviderFailure("request_failed").detail()}
            yield f"data: {json.dumps(error)}\n\n"

    async def operation_events():
        """Release the request slot before background work and close nested provider streams."""
        try:
            async with aclosing(events()) as source:
                async for packet in source:
                    yield packet
        finally:
            operation.close()

    return ChatStreamingResponse(
        operation_events(), operation=operation,
        media_type="text/event-stream",
        background=background_tasks,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ChatStreamingResponse(StreamingResponse):
    """Close the reply generator deterministically when an ASGI client disconnects."""

    def __init__(self, *args, operation=None, **kwargs):
        """Retain request ownership even if a client disconnects before iteration begins."""
        super().__init__(*args, **kwargs)
        self.operation = operation

    async def __call__(self, scope, receive, send) -> None:
        """Shield final generator cleanup from the streaming task's cancellation scope."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    if self.operation is not None:
                        self.operation.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_excludes=["logs/*", "conversation/*"],
    )
