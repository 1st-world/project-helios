"""Serve the Helios web client and API, connecting application services and managing their lifecycle."""

import asyncio
import json
import logging
import mimetypes
import sqlite3
from copy import copy
from contextlib import aclosing, asynccontextmanager
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from hashlib import sha256
from logging.handlers import RotatingFileHandler
from pathlib import Path
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
from services.attachment_cleanup import AttachmentCleanup
from services.context_budget import (
    ContextBudget,
    ContextBudgetExceeded,
    ContextChangedError,
    ContextWindowExceeded,
    SummaryUnavailableError,
)
from services.conversation_service import (
    ConversationManager,
    ConversationProfileConflictError,
    ConversationUnavailableError,
)
from services.context_policy import ContextPolicy
from services.call_usage_store import CallUsageStore
from services.work_run_store import (
    ACTIVE_STATES, WorkContextUnavailableError, WorkRunStore,
)
from services.work_resume import (
    CONNECTION_FINGERPRINT_VERSION, connection_fingerprint,
    selected_files_fingerprint, workspace_fingerprint,
)
from services.file_picker_service import FilePickerService
from services.folder_picker_service import (
    FolderPickerService,
    PickerReconnectingError,
)
from services.context_recovery import ContextRecoveryRegistry, RecoveryError
from services.conversation_workspaces import ConversationWorkspaces
from services.conversation_profiles import ConversationProfiles
from services.memory_service import (
    ConversationMemoryService, MemoryPolicyChangedError,
)
from services.local_work import check_local_cancelled, run_local
from services.prompt_builder import PromptBuilder
from services.project_context import build_project_overview
from services.project_exploration import (
    EXPLORATION_INSTRUCTIONS, ExplorationError, ExplorationLimits,
    ProjectExplorer, project_function_tools,
)
from services.project_reader import ProjectReadLimits
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
conversation_manager = ConversationManager(
    settings.conversations_root,
    default_profile_id=lambda: getattr(
        profile_service.get_active_profile(), "id", None,
    ),
)
workspace_service = WorkspaceService(settings.workspace_root)
conversation_workspaces = ConversationWorkspaces(conversation_manager)
attachment_store = AttachmentStore(getattr(
    settings, "attachments_root", settings.profiles_path.parent / "data" / "attachments",
), referenced=lambda identity: conversation_manager.attachment_is_referenced(identity) or context_recovery.attachment_is_referenced(identity) or work_run_store.attachment_is_referenced(identity))
attachment_service = AttachmentService(
    workspace_service,
    limits=application_settings.attachment_limits(), store=attachment_store,
)
folder_picker_service = FolderPickerService()
file_picker_service = FilePickerService(attachment_service)
context_recovery = ContextRecoveryRegistry()
attachment_cleanup = AttachmentCleanup(attachment_store, context_recovery)
attachment_store.on_change = attachment_cleanup.notify
conversation_manager.on_attachments_released = attachment_cleanup.notify
context_recovery.on_attachments_changed = attachment_cleanup.notify
workspace_operations: dict[object, str] = {}
usage_service = UsageService()
summary_usage_store = SummaryUsageStore(
    settings.logs_root / "summary_usage.sqlite3"
)
call_usage_store = CallUsageStore(settings.logs_root / "usage_calls.sqlite3")
work_run_store = WorkRunStore(getattr(
    settings, "work_runs_path", settings.profiles_path.parent / "data" / "work_runs.sqlite3",
))
conversation_profiles = ConversationProfiles(
    conversation_manager, profile_service, work_run_store,
)
work_run_store.on_attachments_released = attachment_cleanup.notify
pause_deadline_changed = asyncio.Event()
work_run_store.on_pause_changed = pause_deadline_changed.set
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
    """Collect unused attachment copies and close background resources on shutdown."""
    global pause_deadline_changed

    logger.info("Helios started")
    pause_deadline_changed = asyncio.Event()
    work_run_store.on_pause_changed = pause_deadline_changed.set
    cleanup = asyncio.create_task(attachment_cleanup.run())
    pause_expiry = asyncio.create_task(expire_paused_runs())
    try:
        yield
    finally:
        cleanup.cancel()
        pause_expiry.cancel()
        await asyncio.gather(cleanup, pause_expiry, return_exceptions=True)
        await folder_picker_service.close()
        await file_picker_service.close()
        await ai_service.close()
        logger.info("Helios stopped")


async def expire_paused_runs() -> None:
    """Release paused payloads at their exact deadline without periodic polling."""
    failures = 0
    while True:
        pause_deadline_changed.clear()
        try:
            work_run_store.prune_paused()
            deadline = work_run_store.next_pause_expiry()
            failures = 0
        except (sqlite3.Error, OSError):
            failures += 1
            logger.warning("Paused checkpoint expiry was deferred because storage is unavailable.")
            deadline = datetime.now(timezone.utc).timestamp() + min(2 ** min(failures, 6), 60)
        if deadline is None:
            await pause_deadline_changed.wait()
        else:
            delay = max(0, deadline - datetime.now(timezone.utc).timestamp())
            try:
                await asyncio.wait_for(pause_deadline_changed.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


async def collect_attachments(candidates: set[str] | None = None) -> None:
    """Defer cleanup when storage or any saved reference cannot be verified safely."""
    try:
        await run_local(attachment_store.collect, candidates)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        logger.warning("Attachment cleanup was deferred because stored references could not be verified.", exc_info=True)


# Serve WOFF2 consistently when the host does not register its MIME type.
mimetypes.add_type("font/woff2", ".woff2")

app = FastAPI(
    title="Helios BYOK AI Assistant", version="0.3.0-alpha", lifespan=lifespan,
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
    conversation_id: str | None = None
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
    conversation_id: str | None = None
    workspace_session_id: str | None = Field(default=None, min_length=1, max_length=64)


class WorkspaceContextPreviewRequest(BaseModel):
    """Pin the automatic project overview preview to the browser's selected workspace."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = None
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
    explore_project: bool = Field(default=False, strict=True)
    exploration_limits: ExplorationLimits | None = None
    profile_id: str | None = Field(default=None, min_length=1, strict=True)
    expected_profile_revision: int | None = Field(default=None, ge=0, strict=True)
    regenerate_message_index: int | None = Field(default=None, ge=0)
    expected_conversation_version: int | None = Field(default=None, ge=0)
    recovery_id: UUID | None = None
    operation_id: UUID | None = None

    @model_validator(mode="after")
    def validate_recovery_operation(self):
        """Require an explicit operation identifier alongside a recovery receipt."""
        if (self.recovery_id is None) != (self.operation_id is None):
            raise ValueError("Recovery and operation identifiers must be supplied together.")
        if self.explore_project and (
            self.workspace_revision is None or self.workspace_session_id is None
            or self.recovery_id is not None
        ):
            raise ValueError("Project exploration requires the current Workspace revision and session, and cannot reuse a recovery receipt.")
        if self.exploration_limits is not None and not self.explore_project:
            raise ValueError("Exploration limits require explicit project exploration.")
        return self


class WorkRunControlRequest(BaseModel):
    """Authorize a saved run action against its conversation and visible version."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1)
    expected_conversation_version: int | None = Field(default=None, ge=0, strict=True)


class AttachmentSelectionRequest(ContextRequestOptions):
    """Validate files for the backend attachment API without changing GUI selection."""

    paths: list[AttachmentPath] = Field(default_factory=list)
    conversation_id: str | None = None
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


class AttachmentDraftRequest(BaseModel):
    """Retain opaque upload identifiers used by one browser tab's current selection."""

    model_config = ConfigDict(extra="forbid")
    attachment_ids: list[UUID] = Field(default_factory=list)
    released_attachment_ids: list[UUID] = Field(default_factory=list)


class RenameConversationRequest(BaseModel):
    """Validate the requested conversation title."""

    title: str = Field(min_length=1, max_length=100)


class ConversationProfileRequest(BaseModel):
    """Validate an explicit conversation model selection against its own revision."""

    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, strict=True)
    expected_profile_revision: int = Field(ge=0, strict=True)


class EditMessageRequest(BaseModel):
    """Validate replacement content for a user message."""

    content: str = Field(min_length=1)


class WorkspaceRootRequest(BaseModel):
    """Validate the submitted workspace folder path."""

    path: str | None = Field(min_length=1, max_length=4096)
    conversation_id: str | None = None
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
    reasoning_summary: Literal["off", "auto"] = "off"
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
    reasoning_summary: Literal["off", "auto"] | None = None
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
            reasoning_summary=payload.reasoning_summary,
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
    return profile_service.get_summary()


@app.put("/api/profiles/{profile_id}")
async def update_profile(profile_id: str, payload: UpdateProfileRequest):
    """Apply profile changes and return the refreshed profile summary."""
    previous = profile_service.get_profile(profile_id)
    previous_settings = (
        previous.request_settings() if previous is not None else None
    )
    try:
        updated = profile_service.update_profile(
            profile_id,
            name=payload.name,
            endpoint=payload.endpoint,
            api_key=payload.api_key,
            deployment=payload.deployment,
            reasoning_summary=payload.reasoning_summary,
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
    if previous_settings != updated.request_settings():
        memory_service.invalidate_policy(profile_id)
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
    memory_service.invalidate_policy(profile_id)
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
    items = conversation_manager.list()
    for item in items:
        conversation = conversation_manager.get(item["id"])
        item.update(conversation_profile_selection(conversation))
    return items


@app.post("/api/conversations")
async def create_conversation():
    """Create and persist an empty conversation."""
    conversation = conversation_manager.create()
    return {
        **conversation.to_dict(), **conversation_profile_selection(conversation),
    }


def conversation_profile_selection(conversation) -> dict:
    """Initialize a durable choice without publishing uncommitted selection metadata."""
    try:
        return conversation_profiles.selection(conversation)
    except OSError as exc:
        logger.error("Could not save conversation profile (error_type=%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail={
            "code": "conversation_profile_save_failed",
            "message": "Could not save this conversation's model selection. Please try again.",
        }) from None


def resolve_conversation_profile(
    conversation, explicit_id: str | None, expected_revision: int | None = None,
) -> str:
    """Pin an explicit selection or the durable preference without a default fallback."""
    selection = conversation_profile_selection(conversation)
    if (
        expected_revision is not None
        and expected_revision != selection["profile_revision"]
    ):
        raise HTTPException(status_code=409, detail={
            "code": "conversation_profile_changed",
            "message": "The model selection changed. Reload it before sending again.",
        })
    selected_id = (
        explicit_id if explicit_id is not None else selection["profile_id"]
    )
    if selected_id is None or (
        explicit_id is None and not selection["profile_available"]
    ):
        raise HTTPException(status_code=409, detail={
            "code": "conversation_profile_unavailable",
            "message": "Choose an available model profile for this conversation before sending a message.",
        })
    return ai_service.resolve_profile_id(selected_id)


@app.get("/api/conversations/{conversation_id}/profile")
async def get_conversation_profile(conversation_id: str):
    """Read the next-response model selection without loading the full transcript."""
    conversation = conversation_manager.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation_profile_selection(conversation)


@app.patch("/api/conversations/{conversation_id}/profile")
async def select_conversation_profile(
    conversation_id: str, payload: ConversationProfileRequest,
):
    """Change the next response's saved model without invalidating active or paused work."""
    conversation = conversation_manager.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    try:
        return conversation_profiles.select(
            conversation, payload.profile_id,
            expected_revision=payload.expected_profile_revision,
        )
    except ConversationProfileConflictError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "conversation_profile_changed", "message": str(exc),
        }) from None
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except OSError as exc:
        logger.error("Could not save conversation profile (error_type=%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail={
            "code": "conversation_profile_save_failed",
            "message": "Could not save this conversation's model selection. Your previous selection is unchanged.",
        }) from None


def response_display_metadata(run_id: str, conversation_id: str) -> dict:
    """Keep a saved answer readable when its independent accounting is unavailable."""
    try:
        return work_run_store.response_metadata(
            run_id, conversation_id, call_usage_store,
        )
    except (WorkContextUnavailableError, sqlite3.Error):
        return {
            "usage_scope": "work_run", "usage_status": "unavailable",
            "cost_status": "unavailable", "coverage_incomplete": True,
        }


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    """Return a conversation with its complete transcript or report it missing."""
    conversation = conversation_manager.get(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    selection = conversation_profile_selection(conversation)
    data = conversation.to_dict()
    data.update(selection)
    data["paused_run"] = paused_work_metadata(conversation)
    for message in data["messages"]:
        if message["role"] != "assistant" or not message.get("run_id"):
            continue
        message["response_metadata"] = response_display_metadata(
            message["run_id"], conversation_id,
        )
    workspace = conversation_workspaces.for_conversation(conversation)
    data["workspace_available"] = await run_local(workspace.is_available)
    return data


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


def require_work_run(conversation_id: str, run_id: str) -> dict:
    """Require an existing conversation and its own run before returning saved work."""
    if not conversation_manager.get(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    try:
        return work_run_store.get(run_id, conversation_id)
    except WorkContextUnavailableError as exc:
        raise HTTPException(status_code=404, detail="Work run not found.") from exc


def paused_work_metadata(conversation) -> dict | None:
    """Expose the unresolved pause independently of paged work history."""
    run = work_run_store.paused_run(conversation.id)
    if run is None:
        return None
    return {
        **work_run_store.public_metadata(run, messages=conversation.messages),
        "usage": work_run_store.usage(run["id"], call_usage_store),
    }


def require_resolved_pause(conversation_id: str) -> None:
    """Protect a saved pause from new turns, regeneration, recovery, and message edits."""
    run = work_run_store.paused_run(conversation_id)
    if run is not None:
        raise HTTPException(status_code=409, detail={
            "code": "work_run_paused", "run_id": run["id"],
            "message": "This conversation has a paused run. Resume or discard it before sending another message or editing this branch.",
        })


def work_resume_configuration(app_settings=None) -> dict:
    """Fingerprint execution policy independently of the saved private model input."""
    return {
        "version": 1,
        "settings": (app_settings or settings_service.current).to_dict(),
        "tools": project_function_tools(),
        "resource_limits": asdict(ProjectReadLimits()),
    }


def work_connection_fingerprint(
    profile_id: str, *, version: int = CONNECTION_FINGERPRINT_VERSION,
    legacy_name: str | None = None,
) -> str:
    """Compare current credentials and memory permission without persisting either."""
    profile = profile_service.get_profile(profile_id)
    if profile is None:
        raise ContextChangedError("The saved connection is no longer available. Prepare a new request.")
    return connection_fingerprint(
        profile, memory_service.capture_policy(profile_id),
        context=work_resume_configuration(),
        version=version, legacy_name=legacy_name,
    )


def require_resume_authority(run: dict, conversation, workspace, saved: dict | None = None) -> dict:
    """Reject changed branches, bindings, tools, connections, and memory permissions."""
    snapshot = run.get("snapshot", {})
    if saved is None:
        saved = work_run_store.resume_context(run["id"]) or {}
    binding = snapshot.get("workspace", {})
    if (not conversation_manager.is_active(conversation)
            or conversation.version != run["base_version"]
            or WorkRunStore.message_link(run, conversation.messages)["user_index"] is None
            or saved.get("version") != 1
            or not isinstance(saved.get("instructions"), str)
            or not isinstance(saved.get("input_messages"), list)
            or not saved.get("input_messages")
            or not all(isinstance(item, dict) for item in saved["input_messages"])
            or str(workspace.root) != binding.get("root")
            or workspace.revision != binding.get("revision")
            or workspace.session_id != binding.get("session_id")
            or work_connection_fingerprint(
                snapshot.get("profile", {}).get("id"),
                version=saved.get("connection_fingerprint_version", 1),
                legacy_name=snapshot.get("profile", {}).get("name"),
            )
            != saved.get("connection_fingerprint")):
        raise ContextChangedError("The conversation, Workspace, connection, or input policy changed. Prepare a new request instead of resuming this run.")
    return saved


@app.post("/api/work-runs/{run_id}/pause")
async def pause_work_run(run_id: str, payload: WorkRunControlRequest):
    """Request a safe boundary while allowing the current paid step to finish."""
    run = require_work_run(payload.conversation_id, run_id)
    if not run.get("snapshot", {}).get("has_resume_context"):
        raise HTTPException(status_code=409, detail={
            "code": "pause_unavailable", "message": "A safe checkpoint is unavailable for this run. Stop remains available.",
        })
    if not work_run_store.request_pause(run_id):
        raise HTTPException(status_code=409, detail="This run is no longer accepting a pause request.")
    conversation = conversation_manager.get(payload.conversation_id)
    return {"run": work_run_store.public_metadata(
        work_run_store.get(run_id), messages=conversation.messages,
    )}


@app.post("/api/work-runs/{run_id}/stop")
async def stop_work_run(run_id: str, payload: WorkRunControlRequest):
    """Persist explicit Stop intent without releasing an active request's cleanup leases."""
    run = require_work_run(payload.conversation_id, run_id)
    if run["status"] in ACTIVE_STATES | {"paused"}:
        work_run_store.finish(run_id, "cancelled", reason="user_stopped")
    conversation = conversation_manager.get(payload.conversation_id)
    return {"run": work_run_store.public_metadata(
        work_run_store.get(run_id), messages=conversation.messages,
    ), "usage": work_run_store.usage(run_id, call_usage_store)}


@app.post("/api/work-runs/{run_id}/resume")
async def resume_work_run(run_id: str, payload: WorkRunControlRequest, request: Request = None):
    """Validate an explicit continuation before claiming any new tool or Provider work."""
    operation = None
    try:
        run = require_work_run(payload.conversation_id, run_id)
        conversation = conversation_manager.get(payload.conversation_id)
        if run["status"] != "paused":
            raise HTTPException(status_code=409, detail="This run has no paused checkpoint to resume.")
        if payload.expected_conversation_version != conversation.version:
            raise ContextChangedError("Conversation changed. Reload before resuming saved work.")
        workspace = conversation_workspaces.for_conversation(conversation)
        saved = require_resume_authority(run, conversation, workspace)
        operation = context_recovery.begin(conversation.id)
        operation.release_workspace = claim_workspace(workspace)
        upload_ids = [
            source["attachment_id"] for source in saved.get("attachment_sources", [])
            if source.get("source") == "upload"
        ]
        operation.release_attachments = attachment_store.claim(upload_ids)

        def validate_checkpoint():
            """Recheck local input identities without opening a Provider or MCP session."""
            if workspace_fingerprint(workspace, project_protected_paths()) != saved.get("workspace_fingerprint"):
                raise ContextChangedError("Workspace files changed while paused. Prepare a new request instead of resuming saved work.")
            if selected_files_fingerprint(
                workspace, saved.get("selected_files", []),
                max_total_bytes=attachment_service.MAX_TOTAL_BYTES,
            ) != saved.get("selected_files_fingerprint"):
                raise ContextChangedError("Selected Workspace files changed while paused. Prepare a new request.")
            attachment_store.validate_originals(upload_ids)

        await until_disconnect(request, run_local, validate_checkpoint)
        require_resume_authority(run, conversation, workspace, saved)
        request_messages = conversation.messages
        profile_id = run["snapshot"]["profile"]["id"]
        profile = replace(
            profile_service.get_profile(profile_id),
            name=run["snapshot"]["profile"]["name"],
        )
        request_ai = ai_service.with_profile(profile)
        policy = ContextPolicy(**run["snapshot"]["context_policy"])
        request_memory = memory_service
        resume_memory_policy = request_memory.capture_policy(profile_id)
        background_tasks = BackgroundTasks()

        def authority():
            """Require the validated branch and permissions before every resumed action."""
            require_resume_authority(run, conversation, workspace, saved)
            if conversation.messages is not request_messages:
                raise ContextChangedError("Conversation changed during resumed work.")
            if not request_memory.policy_is_current(resume_memory_policy):
                raise ContextChangedError("The saved connection or memory policy changed during resumed work. Prepare a new request.")

        def inputs():
            """Replay the exact prepared base input without generating a fresh summary."""
            authority()
            return saved["instructions"], saved["input_messages"]

        async def events():
            """Stream the claimed continuation and adopt its answer exactly once."""
            claimed = False
            completed = False
            try:
                authority()
                work_run_store.claim_resume(run_id)
                claimed = True
                yield f"data: {json.dumps({'type': 'start', 'conversation_id': conversation.id, 'run_id': run_id, 'user_index': run['snapshot']['source_message']['user_index'], 'pause_available': True, 'active_elapsed_seconds': work_run_store.active_elapsed(run_id)})}\n\n"
                explorer = ProjectExplorer(
                    request_ai, work_run_store, run_id, workspace,
                    prepare_input=inputs, check_authority=authority, policy=policy,
                    visual_tokens=saved.get("visual_tokens", 0),
                    limits=ExplorationLimits(**run["snapshot"].get("goals", {})),
                    protected=project_protected_paths(), resumed=True,
                )
                async with aclosing(explorer.stream(profile_id)) as source:
                    async for event in source:
                        if event["type"] == "done":
                            completed = True
                            continue
                        if event["type"] == "paused":
                            operation.close()
                            event["run"] = work_run_store.public_metadata(
                                work_run_store.get(run_id), messages=conversation.messages,
                            )
                        yield f"data: {json.dumps(event)}\n\n"
                        if event["type"] == "paused":
                            return
                if not completed:
                    raise RuntimeError("Resumed work ended without a saved boundary.")
                authority()
                message = conversation_manager.adopt_run_reply(
                    conversation, run_id, work_run_store,
                    expected_messages=request_messages,
                    user_attachment_sources=saved.get("attachment_sources"),
                )
                background_tasks.add_task(
                    request_memory.compact_if_needed, conversation,
                    profile_id=profile_id, summary_policy=resume_memory_policy,
                )
                yield f"data: {json.dumps({'type': 'done', 'run_id': run_id, 'created_at': message.created_at.isoformat(), 'response_metadata': response_display_metadata(run_id, conversation.id)})}\n\n"
            except (ContextChangedError, ContextBudgetExceeded, ExplorationError, ProviderFailure) as exc:
                code = getattr(exc, "code", None) or (
                    "input_policy_blocked" if isinstance(exc, ContextBudgetExceeded) else "authority_changed"
                )
                error = exc.detail() if isinstance(exc, ProviderFailure) else {"code": code, "message": str(exc)}
                current_status = work_run_store.get(run_id)["status"]
                if ((claimed and current_status in ACTIVE_STATES)
                        or (isinstance(exc, ContextChangedError) and current_status == "paused")):
                    work_run_store.finish(run_id, "stopped" if isinstance(exc, (ContextChangedError, ContextBudgetExceeded)) else "failed", reason=code)
                work_run_store.record_failure(run_id, error)
                yield f"data: {json.dumps({'type': 'error', **error, 'run_id': run_id, 'run_status': work_run_store.get(run_id)['status'], 'run_usage': work_run_store.usage(run_id, call_usage_store)})}\n\n"
            except Exception as exc:
                logger.error("Resumed work failed (error_type=%s)", type(exc).__name__)
                if claimed and work_run_store.get(run_id)["status"] in ACTIVE_STATES:
                    work_run_store.finish(run_id, "failed", reason="request_failed")
                yield f"data: {json.dumps({'type': 'error', 'code': 'request_failed', 'message': 'Saved work could not finish. Recorded usage is retained.', 'run_id': run_id, 'run_status': work_run_store.get(run_id)['status']})}\n\n"
            finally:
                try:
                    if claimed and work_run_store.get(run_id)["status"] in ACTIVE_STATES:
                        work_run_store.finish(run_id, "cancelled", reason="request_closed")
                except Exception as exc:
                    logger.error("Could not finalize resumed work (error_type=%s)", type(exc).__name__)
                finally:
                    operation.close()

        return ChatStreamingResponse(
            events(), operation=operation, media_type="text/event-stream",
            background=background_tasks,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except (ContextChangedError, RecoveryError, AttachmentError) as exc:
        if operation is not None:
            operation.close()
        if (isinstance(exc, (ContextChangedError, AttachmentError))
                and run["status"] == "paused"
                and (conversation.version != run["base_version"]
                     or payload.expected_conversation_version == conversation.version)):
            work_run_store.finish(run_id, "stopped", reason="resume_context_changed")
        raise HTTPException(status_code=getattr(exc, "status", getattr(exc, "status_code", 409)), detail={
            "code": "resume_context_changed", "message": str(exc),
        }) from None
    except BaseException:
        if operation is not None:
            operation.close()
        raise


@app.get("/api/conversations/{conversation_id}/work-runs")
async def work_history(conversation_id: str, before: int | None = None):
    """Return lightweight saved work, including unsuccessful and replaced attempts."""
    conversation = conversation_manager.get(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    try:
        page = work_run_store.history(
            conversation_id, before=before, messages=conversation.messages,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    adopted = {
        message.run_id for message in conversation.messages if message.run_id
    }
    for run in page["runs"]:
        run["adopted"] = run["id"] in adopted
        run["usage"] = work_run_store.usage(run["id"], call_usage_store)
    page["paused_run"] = paused_work_metadata(conversation)
    return page


@app.get("/api/conversations/{conversation_id}/work-runs/{run_id}")
async def work_activity(conversation_id: str, run_id: str, offset: int = 0):
    """Return one bounded activity page without private provider reasoning or output."""
    run = require_work_run(conversation_id, run_id)
    conversation = conversation_manager.get(conversation_id)
    try:
        page = work_run_store.activity(run_id, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for step in page["steps"]:
        if step["kind"] == "model":
            step["record"] = call_usage_store.get_record(step["call_id"])
    return {
        **page, "run": work_run_store.public_metadata(
            run, messages=conversation.messages,
        ),
        "usage": work_run_store.usage(run_id, call_usage_store),
    }


@app.get("/api/conversations/{conversation_id}/work-runs/{run_id}/evidence")
async def work_evidence(
    conversation_id: str, run_id: str, response_id: str, call_id: str,
):
    """Open exact persisted evidence without reading current files or starting tools."""
    require_work_run(conversation_id, run_id)
    try:
        return work_run_store.evidence(run_id, response_id, call_id)
    except WorkContextUnavailableError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    """Delete a conversation from memory and disk, reporting storage failures."""
    conversation = conversation_manager.get(conversation_id)
    candidates = {
        source["attachment_id"] for message in conversation.messages
        for source in message.attachment_sources if source["source"] == "upload"
    } if conversation else set()
    try:
        deleted = conversation_manager.delete(conversation_id)
    except OSError:
        logger.exception("Could not delete conversation: %s", conversation_id)
        raise HTTPException(
            status_code=500, detail="Could not delete conversation from disk."
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    conversation_workspaces.forget(conversation_id)
    context_recovery.forget_conversation(conversation_id)
    work_run_store.forget_conversation(conversation_id)
    await collect_attachments(candidates)
    return {"deleted": True}


@app.patch("/api/conversations/{conversation_id}/messages/{message_index}")
async def edit_message(
    conversation_id: str, message_index: int, payload: EditMessageRequest
):
    """Edit a user message and return the truncated conversation branch."""
    require_resolved_pause(conversation_id)
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


def resolve_workspace(
    conversation_id: str | None = None, session_id: str | None = None,
) -> WorkspaceService:
    """Resolve explicit conversation or browser draft scope before interpreting paths."""
    if conversation_id is not None:
        conversation = conversation_manager.get(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return conversation_workspaces.for_conversation(conversation)
    if session_id is None or session_id == workspace_service.session_id:
        return workspace_service
    draft = conversation_workspaces.draft(session_id)
    if draft is None:
        raise HTTPException(status_code=409, detail={
            "code": "workspace_draft_unavailable",
            "message": "This workspace draft expired. Open a new chat or reopen the saved conversation.",
        })
    if draft.conversation_id is not None:
        return resolve_workspace(draft.conversation_id)
    return draft.workspace


def require_workspace_root(
    root, revision: int | None = None, workspace: WorkspaceService = workspace_service,
) -> None:
    """Reject local results prepared for a workspace that is no longer selected."""
    if (
        workspace.root != root
        or (revision is not None and workspace.revision != revision)
    ):
        raise HTTPException(status_code=409, detail={
            "code": "attachment_workspace_changed",
            "message": "Workspace changed while preparing this request. Try again.",
        })


def require_workspace_revision(
    revision: int | None, session_id: str | None = None,
    workspace: WorkspaceService = workspace_service,
) -> None:
    """Reject stale browser references even after a folder is selected again."""
    require_workspace_root(workspace.root, revision, workspace)
    if session_id is not None and workspace.session_id != session_id:
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
    if not workspace.is_available():
        raise HTTPException(status_code=409, detail={
            "code": "workspace_unavailable",
            "message": "The saved Workspace folder is unavailable. Choose an existing folder or disconnect it in Workspace.",
        })


def claim_workspace(workspace: WorkspaceService):
    """Keep folder changes blocked until project work and its cleanup finish."""
    token = object()
    if workspace.root is not None:
        workspace_operations[token] = workspace.session_id

    def release() -> None:
        """Release an operation idempotently after streaming or local worker cleanup."""
        workspace_operations.pop(token, None)

    return release


def require_workspace_idle(workspace: WorkspaceService = workspace_service) -> None:
    """Leave the selected folder intact while a project request still owns it."""
    owner = conversation_workspaces.owner(workspace)
    if workspace.session_id in workspace_operations.values() or (
        owner is not None and context_recovery.is_busy(owner)
    ):
        raise HTTPException(status_code=409, detail={
            "code": "workspace_request_in_progress",
            "message": "A request is running. Stop it and wait for cleanup before changing this Workspace.",
        })


def workspace_payload(workspace: WorkspaceService, entries: list[dict] | None) -> dict:
    """Represent an unselected folder as null without a fallback path."""
    payload = {
        "root": str(workspace.root) if workspace.root is not None else None,
        "revision": workspace.revision,
        "session_id": workspace.session_id,
        "entries": entries if entries is not None else [],
        "available": workspace.is_available(),
    }
    if entries is None:
        payload["entries_loaded"] = False
    owner = conversation_workspaces.owner(workspace)
    if owner is not None:
        conversation = conversation_manager.get(owner)
        if conversation is not None:
            payload["conversation_id"] = owner
            payload["conversation_version"] = conversation.version
    return payload


@app.get("/api/chat/status")
async def chat_status(
    conversation_id: str | None = None, workspace_session_id: str | None = None,
):
    """Confirm disconnect cleanup and recover a draft's saved identity without filesystem work."""
    workspace = resolve_workspace(conversation_id, workspace_session_id)
    owner = conversation_workspaces.owner(workspace)
    if owner is not None:
        await context_recovery.wait_for_idle(owner)
    return {
        "conversation_id": owner,
        "in_progress": owner is not None and context_recovery.is_busy(owner),
    }


def build_workspace_structure(
    workspace: WorkspaceService, options: WorkspaceStructureRequest,
) -> str:
    """Build only the explicitly selected folder-structure context."""
    return workspace.project_context(
        relative_path=options.path, max_depth=options.max_depth,
        max_entries=options.max_entries,
        include_absolute_path=options.include_absolute_path,
    )


def project_protected_paths() -> list[Path]:
    """Use the same host runtime exclusions for automatic overview and explicit exploration."""
    return [
        profile_service.storage_path, settings_service.storage_path,
        settings.conversations_root, settings.logs_root,
        getattr(
            settings, "attachments_root",
            settings.profiles_path.parent / "data" / "attachments",
        ),
        getattr(
            settings, "work_runs_path",
            settings.profiles_path.parent / "data" / "work_runs.sqlite3",
        ),
    ]


def chat_uses_workspace(payload: ChatRequest) -> bool:
    """Distinguish explicit project input from a folder-independent chat request."""
    return bool(
        payload.explore_project or payload.workspace_file or payload.workspace_files
        or payload.attachment_files or payload.workspace_structure is not None
        or any(source.source == "workspace" for source in payload.attachment_sources or [])
    )


@app.get("/api/workspace")
async def workspace_tree(
    request: Request = None, include_entries: bool = True,
    conversation_id: str | None = None, workspace_session_id: str | None = None,
):
    """Return folder metadata and list files only when the caller requests them."""
    live_workspace = resolve_workspace(conversation_id, workspace_session_id)
    workspace = copy(live_workspace)
    entries = (
        await until_disconnect(request, run_local, workspace.tree)
        if include_entries and workspace.is_available() else None
    )
    require_workspace_root(workspace.root, workspace.revision, live_workspace)
    return workspace_payload(live_workspace, entries)


@app.post("/api/workspace")
async def create_workspace_draft():
    """Create a separate folder-free browser draft without saving a blank chat."""
    return workspace_payload(conversation_workspaces.create_draft(), None)


@app.put("/api/workspace/root")
async def update_workspace_root(
    payload: WorkspaceRootRequest, request: Request = None
):
    """Select or disconnect a folder once all project requests have finished."""
    live_workspace = resolve_workspace(
        payload.conversation_id, payload.workspace_session_id,
    )
    require_workspace_revision(
        payload.workspace_revision, payload.workspace_session_id, live_workspace,
    )
    require_workspace_idle(live_workspace)
    owner = conversation_workspaces.owner(live_workspace)
    workspace = copy(live_workspace)
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
    current_workspace = resolve_workspace(
        payload.conversation_id, payload.workspace_session_id,
    )
    if current_workspace is not live_workspace:
        raise HTTPException(status_code=409, detail={
            "code": "attachment_workspace_changed",
            "message": "This draft was saved while choosing a folder. Reopen the conversation and select its Workspace again.",
        })
    require_workspace_root(previous_root, previous_revision, live_workspace)
    require_workspace_idle(live_workspace)
    if owner is not None:
        conversation = conversation_manager.get(owner)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        conversation_manager.set_workspace(
            conversation, conversation_workspaces.binding(workspace),
        )
    live_workspace.root = workspace.root
    live_workspace.revision = workspace.revision
    logger.info("Workspace root changed to %s", live_workspace.root)
    return workspace_payload(live_workspace, entries)


@app.post("/api/workspace/structure")
async def preview_workspace_structure(
    payload: WorkspaceStructurePreviewRequest, request: Request = None,
):
    """Preview the exact selected structure locally without reading file bodies."""
    live_workspace = resolve_workspace(
        payload.conversation_id, payload.workspace_session_id,
    )
    require_workspace_revision(
        payload.workspace_revision, payload.workspace_session_id, live_workspace,
    )
    workspace = copy(live_workspace)
    require_selected_workspace(workspace)
    release = claim_workspace(workspace)
    try:
        context = await until_disconnect(
            request, run_local, build_workspace_structure, workspace, payload,
        )
        require_workspace_root(workspace.root, workspace.revision, live_workspace)
        return {
            "context": context, "root": str(workspace.root),
            "revision": workspace.revision,
            "session_id": workspace.session_id,
        }
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        release()


@app.post("/api/workspace/context")
async def preview_workspace_context(
    payload: WorkspaceContextPreviewRequest, request: Request = None,
):
    """Preview the same bounded overview used by chat without starting model or MCP work."""
    live_workspace = resolve_workspace(
        payload.conversation_id, payload.workspace_session_id,
    )
    require_workspace_revision(
        payload.workspace_revision, payload.workspace_session_id, live_workspace,
    )
    workspace = copy(live_workspace)
    release = claim_workspace(workspace)
    try:
        context = await until_disconnect(
            request, run_local, build_project_overview,
            workspace, project_protected_paths(),
        )
        require_workspace_root(workspace.root, workspace.revision, live_workspace)
        return {
            "context": context, "revision": workspace.revision,
            "session_id": workspace.session_id,
        }
    finally:
        release()


@app.websocket("/api/workspace/picker/{token}")
async def workspace_picker(websocket: WebSocket, token: UUID):
    """Relay native folder selection without changing its tab-owned lifecycle."""
    await relay_native_picker(
        websocket, token, folder_picker_service, "folder",
        "Could not connect to folder picker. Enter the path manually.",
    )


@app.websocket("/api/attachments/picker/{token}")
async def attachment_picker(websocket: WebSocket, token: UUID):
    """Add host-selected attachment copies without accepting browser-supplied paths."""
    await relay_native_picker(
        websocket, token, file_picker_service, "file",
        "Could not connect to file picker. Try Choose files again.",
    )


async def relay_native_picker(
    websocket: WebSocket, token: UUID, service: FolderPickerService,
    label: str, connection_error: str,
):
    """Relay same-origin picker sessions using the selected conversation's start folder."""
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
        initial_dir = ""
        if command["action"] == "start":
            picker_workspace = resolve_workspace(
                command.get("conversation_id"), command.get("workspace_session_id"),
            )
            if picker_workspace.root is not None:
                initial_dir = str(picker_workspace.root)
        # A tab must still be able to cancel its owned picker after its conversation is removed.
        session = service.attach(
            key, initial_dir,
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
                    await service.cancel(session)
                except Exception:
                    logger.exception("Could not cancel %s picker", label)
                    await websocket.send_json(
                        {
                            "status": "cancel-error",
                            "detail": f"Could not close {label} picker. Try Cancel again.",
                        }
                    )
    except PickerReconnectingError:
        await websocket.send_json({"status": "reconnecting"})
    except WebSocketDisconnect:
        pass
    except (asyncio.TimeoutError, ValueError):
        await websocket.close(code=1008)
    except Exception:
        logger.exception("%s picker connection failed", label.capitalize())
        try:
            await websocket.send_json(
                {
                    "status": "error",
                    "detail": connection_error,
                }
            )
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        if session is not None:
            service.detach(key, session)
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass


@app.post("/api/read-file")
async def read_file(payload: ReadFileRequest, request: Request = None):
    """Read a workspace text file and translate access failures into HTTP errors."""
    live_workspace = resolve_workspace(
        payload.conversation_id, payload.workspace_session_id,
    )
    require_workspace_revision(
        payload.workspace_revision, payload.workspace_session_id, live_workspace,
    )
    workspace = copy(live_workspace)
    require_selected_workspace(workspace)
    release = claim_workspace(workspace)
    try:
        content = await until_disconnect(
            request, run_local, workspace.read_text, payload.path,
        )
        require_workspace_root(workspace.root, workspace.revision, live_workspace)
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
    except AttachmentError:
        raise
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
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


@app.put("/api/attachments/drafts/{draft_id}")
async def retain_attachment_draft(
    draft_id: UUID, payload: AttachmentDraftRequest, request: Request = None,
):
    """Renew a tab's selected copies or release them after removal or discard."""
    await attachment_storage_work(
        request, attachment_store.replace_draft, str(draft_id),
        [str(identity) for identity in payload.attachment_ids],
        [str(identity) for identity in payload.released_attachment_ids],
    )
    return {"retained": True}


@app.get("/api/attachments/uploads/{attachment_id}")
async def get_uploaded_attachment(attachment_id: UUID, request: Request = None):
    """Return one live upload's metadata using an opaque identifier."""
    return await attachment_storage_work(request, attachment_store.get, str(attachment_id))


@app.delete("/api/attachments/uploads/{attachment_id}")
async def delete_uploaded_attachment(attachment_id: UUID, request: Request = None):
    """Delete an unreferenced copy; conversations, drafts, and requests retain theirs."""
    await attachment_storage_work(request, attachment_store.delete, str(attachment_id))
    context_recovery.invalidate_attachment(str(attachment_id))
    return {"deleted": True}


@app.post("/api/attachments/inspect")
async def inspect_attachments(
    payload: AttachmentSelectionRequest, request: Request = None
):
    """Validate local attachments without provider calls or returning binary contents."""
    request_attachments = copy(attachment_service)
    live_workspace = resolve_workspace(
        payload.conversation_id, payload.workspace_session_id,
    )
    request_attachments.workspace = copy(live_workspace)
    request_budget = context_budget
    request_settings = settings_service.current
    sources = [
        WorkspaceAttachment(source="workspace", path=path) for path in payload.paths
    ] + payload.attachment_sources
    workspace_bound = any(source.source == "workspace" for source in sources)
    if workspace_bound:
        require_workspace_revision(
            payload.workspace_revision, payload.workspace_session_id, live_workspace,
        )
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
            live_workspace,
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
    requested_profile_id = payload.profile_id
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
            live_workspace = conversation_workspaces.for_conversation(conversation)
            if data.get("workspace_root") is not None:
                require_workspace_root(
                    data["workspace_root"], data["workspace_revision"],
                    live_workspace,
                )
            payload = ChatRequest(
                prompt=data["prompt"], conversation_id=conversation.id,
                profile_id=data["profile_id"],
                regenerate_message_index=data["user_index"],
                expected_conversation_version=data["version"],
                attachment_sources=data.get("attachment_sources"),
            )
        else:
            live_workspace = resolve_workspace(
                payload.conversation_id, payload.workspace_session_id,
            )
            if (
                payload.conversation_id is not None
                or payload.workspace_session_id is not None
                or chat_uses_workspace(payload)
            ):
                require_workspace_revision(
                    payload.workspace_revision, payload.workspace_session_id, live_workspace,
                )
            if chat_uses_workspace(payload):
                require_selected_workspace(live_workspace)
            conversation = (
                conversation_manager.get(payload.conversation_id or "")
                if payload.conversation_id is not None or payload.regenerate_message_index is not None
                else None
            )
            draft = conversation_workspaces.draft(live_workspace.session_id)
            if (
                conversation is None and payload.conversation_id is None
                and draft is not None and draft.conversation_id is not None
            ):
                conversation = conversation_manager.get(draft.conversation_id)
            if conversation is None and (
                payload.conversation_id is not None
                or payload.regenerate_message_index is not None
                or (draft is not None and draft.conversation_id is not None)
            ):
                raise HTTPException(status_code=404, detail="Conversation not found.")
            if conversation is not None:
                request_profile_id = resolve_conversation_profile(
                    conversation, payload.profile_id,
                    payload.expected_profile_revision,
                )
            else:
                if payload.expected_profile_revision not in {None, 0}:
                    raise HTTPException(status_code=409, detail={
                        "code": "conversation_profile_changed",
                        "message": "A new conversation has no saved model revision. Review the selected model and try again.",
                    })
                request_profile_id = ai_service.resolve_profile_id(payload.profile_id)
                conversation = conversation_workspaces.adopt(
                    live_workspace, profile_id=request_profile_id,
                )
            payload = payload.model_copy(update={"profile_id": request_profile_id})
            live_workspace = conversation_workspaces.for_conversation(conversation)
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
                if any(
                    source.source == "workspace" and (
                        source.workspace_revision is None or source.workspace_session_id is None
                    ) for source in payload.attachment_sources
                ):
                    raise HTTPException(status_code=409, detail={
                        "code": "attachment_workspace_changed",
                        "message": "Saved Workspace attachments have no connection identity. Select the files again or remove them before regenerating.",
                    })
            if chat_uses_workspace(payload):
                require_workspace_revision(
                    payload.workspace_revision, payload.workspace_session_id, live_workspace,
                )
                require_selected_workspace(live_workspace)
        require_resolved_pause(conversation.id)
        operation = context_recovery.begin(
            conversation.id, ticket=ticket,
            operation_id=operation_id,
        )
        if ticket is None and requested_profile_id is not None:
            try:
                conversation_manager.set_profile(
                    conversation, request_profile_id,
                    expected_revision=payload.expected_profile_revision,
                )
            except OSError as exc:
                logger.error("Could not save request profile selection (error_type=%s)", type(exc).__name__)
                raise HTTPException(status_code=500, detail={
                    "code": "conversation_profile_save_failed",
                    "message": "Could not save this conversation's model selection. No request was sent.",
                }) from None
        workspace_bound = (
            data.get("workspace_bound", data.get("workspace_root") is not None)
            if ticket is not None else (
                chat_uses_workspace(payload) or live_workspace.root is not None
            )
        )
        if workspace_bound:
            operation.release_workspace = claim_workspace(live_workspace)
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
    live_workspace = conversation_workspaces.for_conversation(conversation)
    request_workspace = copy(live_workspace)
    workspace_bound = (
        data.get("workspace_bound", data.get("workspace_root") is not None)
        if data else (
            chat_uses_workspace(payload) or request_workspace.root is not None
        )
    )
    request_attachments.workspace = request_workspace
    request_budget = context_budget
    request_settings = settings_service.current
    resume_workspace_fingerprint = None
    resume_selected_fingerprint = None
    resume_selected_files = (
        payload.workspace_files + payload.attachment_files
        + ([payload.workspace_file] if payload.workspace_file else [])
        + [source.path for source in payload.attachment_sources or [] if source.source == "workspace"]
    )
    if payload.explore_project:
        try:
            resume_workspace_fingerprint = await run_local(
                workspace_fingerprint, request_workspace, project_protected_paths(),
            )
            resume_selected_fingerprint = await run_local(
                selected_files_fingerprint, request_workspace, resume_selected_files,
                max_total_bytes=request_attachments.MAX_TOTAL_BYTES,
            )
        except ContextChangedError:
            resume_workspace_fingerprint = None
            logger.info("Pause is unavailable because the Workspace checkpoint could not be verified.")
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
            """Pin the automatic overview and explicitly selected files to one request root."""
            workspace_context = build_project_overview(
                request_workspace, project_protected_paths(),
            )
            if payload.workspace_structure is not None:
                workspace_context += "\n\n" + build_workspace_structure(
                    request_workspace, payload.workspace_structure,
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
            require_workspace_root(workspace_root, request_workspace.revision, live_workspace)
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
    exploration_tools = project_function_tools() if payload.explore_project else None
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
                **({
                    "reply_tools": exploration_tools,
                    "reply_instructions_suffix": EXPLORATION_INSTRUCTIONS,
                } if payload.explore_project else {}),
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

    def assess_inputs(instructions, messages) -> dict:
        """Count enabled exploration definitions without changing ordinary chat estimates."""
        if payload.explore_project:
            instructions += EXPLORATION_INSTRUCTIONS
        return policy.assess(
            instructions, messages, visual_tokens=attachments.visual_tokens,
            **({"tools": exploration_tools} if payload.explore_project else {}),
        )

    context_assessment = assess_inputs(instructions, input_messages)
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
            context_assessment = assess_inputs(*original_context)
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
    turn_version = conversation.version

    async def events():
        """Stream reply events, save a completed reply, and schedule memory compaction."""
        answer = ""
        usage_data = None
        usage_call_id = None
        profile_data = None
        completed = False
        reported_assessment = None
        run_id = None
        memory_submitted = False
        reasoning_summaries = {}

        def exploration_authority() -> None:
            """Stop stale authority and prevent using responses derived from revoked memory."""
            if (not conversation_manager.is_active(conversation)
                    or conversation.messages is not turn_messages
                    or conversation.version != turn_version):
                raise ContextChangedError("Conversation changed during exploration. Start a new request.")
            if (live_workspace.root != request_workspace.root
                    or live_workspace.revision != request_workspace.revision
                    or live_workspace.session_id != request_workspace.session_id):
                raise ContextChangedError("Workspace changed during exploration. Start a new request.")
            if ((memory_submitted or (run_id is not None and work_run_store.pause_requested(run_id)))
                    and not request_memory.policy_is_current(request_memory_policy)):
                raise ContextChangedError("Conversation memory permission changed during exploration. Start a new request.")

        def exploration_input() -> tuple[str, list[dict]]:
            """Allow original-history fallback only before summarized input is submitted."""
            nonlocal memory_submitted
            selected = current_inputs()
            memory_submitted = memory_submitted or selected != original_context
            return selected

        def exploration_assessment(assessment: dict) -> None:
            """Publish the actual per-call estimate to existing policy events and errors."""
            nonlocal context_assessment
            context_assessment = assessment

        def failure_packet(error: dict) -> str:
            """Attach independent run accounting to an exploration failure without new calls."""
            if run_id is not None:
                status = "stopped" if error.get("code") in {
                    "conversation_changed", "context_recovery_unavailable", "context_preflight_blocked",
                } else "failed"
                recorded = stop_run(status, error.get("code", "request_failed"))
                if recorded:
                    try:
                        work_run_store.record_failure(run_id, error)
                    except Exception as exc:
                        logger.warning("Could not retain work feedback (error_type=%s)", type(exc).__name__)
                try:
                    run_status = work_run_store.get(run_id)["status"] if recorded else "unavailable"
                    run_usage = work_run_store.usage(run_id, call_usage_store)
                except Exception:
                    run_status, run_usage = "unavailable", None
                error = {**error, "run_id": run_id,
                         "run_status": run_status, "run_usage": run_usage}
            return f"data: {json.dumps(error)}\n\n"

        def stop_run(status: str, reason: str) -> bool:
            """Release request ownership even if terminal run storage is unavailable."""
            try:
                if work_run_store.get(run_id)["status"] in ACTIVE_STATES:
                    work_run_store.finish(run_id, status, reason=reason)
                return True
            except Exception as exc:
                logger.error("Could not finalize work run (error_type=%s)", type(exc).__name__)
                return False

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
            if payload.explore_project:
                source_user_index = (
                    payload.regenerate_message_index
                    if payload.regenerate_message_index is not None
                    else len(turn_messages) - 1
                )
                run_id = work_run_store.create(
                    conversation.id, turn_version,
                    kind="regeneration" if payload.regenerate_message_index is not None else "explore",
                    user_index=payload.regenerate_message_index,
                    snapshot={
                        "source_message": {
                            "user_index": source_user_index,
                            "created_at": user_message.created_at.isoformat(),
                            "content_sha256": sha256(
                                user_message.content.encode("utf-8")
                            ).hexdigest(),
                        },
                        "workspace": {
                            "root": str(request_workspace.root),
                            "revision": request_workspace.revision,
                            "session_id": request_workspace.session_id,
                        },
                        "profile": {name: getattr(pinned_profile, name) for name in (
                            "id", "name", "deployment", "reasoning_summary", "input_price_per_million",
                            "output_price_per_million", "long_context_threshold",
                            "long_input_price_per_million", "long_output_price_per_million",
                            "cache_read_price_per_million", "cache_write_price_per_million",
                            "long_cache_read_price_per_million", "long_cache_write_price_per_million",
                        )},
                        "context_policy": asdict(policy),
                        "memory_policy": request_memory_policy.policy,
                        "tools": exploration_tools,
                        "resource_limits": asdict(ProjectReadLimits()),
                        "goals": (payload.exploration_limits or ExplorationLimits()).model_dump(),
                        **({"resume_context": {
                            "version": 1,
                            "instructions": outgoing_instructions,
                            "input_messages": outgoing_messages,
                            "visual_tokens": attachments.visual_tokens,
                            "attachment_sources": saved_attachment_sources or [],
                            "workspace_fingerprint": resume_workspace_fingerprint,
                            "selected_files": resume_selected_files,
                            "selected_files_fingerprint": resume_selected_fingerprint,
                            "selected_files_max_bytes": request_attachments.MAX_TOTAL_BYTES,
                            "connection_fingerprint_version": CONNECTION_FINGERPRINT_VERSION,
                            "connection_fingerprint": connection_fingerprint(
                                pinned_profile, request_memory_policy,
                                context=work_resume_configuration(request_settings),
                            ),
                        }} if resume_workspace_fingerprint else {}),
                    },
                )
            start_event = {
                "type": "start", "conversation_id": conversation.id,
                "user_created_at": user_message.created_at.isoformat(),
            }
            if run_id is not None:
                start_event["run_id"] = run_id
                start_event["user_index"] = source_user_index
                start_event["pause_available"] = bool(resume_workspace_fingerprint)
                start_event["active_elapsed_seconds"] = work_run_store.active_elapsed(run_id)
            yield f"data: {json.dumps(start_event)}\n\n"
            if attachments.metadata:
                metadata_event = {
                    "type": "attachments", "files": attachments.metadata,
                }
                yield f"data: {json.dumps(metadata_event)}\n\n"
            for notice in policy_events():
                yield notice
            outgoing_instructions, outgoing_messages = current_inputs()
            if run_id is not None:
                explorer = ProjectExplorer(
                    request_ai, work_run_store, run_id, live_workspace,
                    prepare_input=exploration_input,
                    check_authority=exploration_authority, policy=policy,
                    visual_tokens=attachments.visual_tokens,
                    limits=payload.exploration_limits,
                    protected=project_protected_paths(),
                    on_assessment=exploration_assessment,
                )
                source = explorer.stream(profile_id)
            else:
                source = request_ai.stream(
                    outgoing_instructions, outgoing_messages,
                    profile_id=profile_id, conversation_id=conversation.id,
                    call_kind=("regeneration" if payload.regenerate_message_index is not None else "chat"),
                )
            async with aclosing(source) as reply_stream:
                async for event in reply_stream:
                    for notice in policy_events():
                        yield notice
                    if event["type"] == "delta":
                        answer += event["text"]
                    elif event["type"] == "reasoning_summary":
                        reasoning_summaries[event["call_id"]] = event["parts"]
                    elif event["type"] == "usage":
                        usage_data = event.get("usage")
                        profile_data = event.get("profile")
                        usage_call_id = event.get("call_id")
                    elif event["type"] == "done":
                        completed = True
                        continue
                    elif event["type"] == "paused":
                        operation.close()
                        event["run"] = work_run_store.public_metadata(
                            work_run_store.get(run_id), messages=conversation.messages,
                        )
                    yield f"data: {json.dumps(event)}\n\n"
                    if event["type"] == "paused":
                        return

            if not completed:
                raise RuntimeError("Response stream ended before completion.")
            if data and (
                conversation.version != data["version"]
                or not request_memory.policy_is_current(request_memory_policy)
            ):
                raise ContextChangedError("The recovery request became stale during generation. Its response was not saved; recorded API usage is retained.")
            msg_kwargs = {}
            if reasoning_summaries:
                msg_kwargs["reasoning_summary"] = [
                    part for parts in reasoning_summaries.values() for part in parts
                ]
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

            if run_id is not None:
                exploration_authority()
                saved_message = conversation_manager.adopt_run_reply(
                    conversation, run_id, work_run_store,
                    expected_messages=turn_messages,
                    user_attachment_sources=saved_attachment_sources,
                )
            elif payload.regenerate_message_index is None:
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
            if run_id is not None:
                done_event["run_id"] = run_id
                done_event["response_metadata"] = response_display_metadata(
                    run_id, conversation.id,
                )
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
            yield failure_packet(error)
        except ContextWindowExceeded as exc:
            for notice in policy_events():
                yield notice
            error = {"type": "error", **exc.detail()}
            if not data and not payload.explore_project:
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
            if payload.explore_project:
                error["recovery"] = {
                    "available": False,
                    "reason": "Request stopped. Review the input and prepare a new request; this attempt is not automatically resumed.",
                }
            yield failure_packet(error)
        except ProviderAttachmentError as exc:
            for notice in policy_events():
                yield notice
            error = {
                "type": "error", **exc.detail(),
                "paths": [item["path"] for item in attachments.metadata],
            }
            yield failure_packet(error)
        except ProviderFailure as exc:
            for notice in policy_events():
                yield notice
            error = {"type": "error", **exc.detail()}
            yield failure_packet(error)
        except ExplorationError as exc:
            yield failure_packet({"type": "error", "code": exc.code, "message": str(exc)})
        except ContextChangedError as exc:
            error = {
                "type": "error", "message": str(exc),
                "code": (
                    "context_recovery_unavailable" if data else "conversation_changed"
                ),
            }
            yield failure_packet(error)
        except Exception as exc:
            for notice in policy_events():
                yield notice
            logger.error("Chat stream failed (error_type=%s)", type(exc).__name__)
            error = {"type": "error", **ProviderFailure("request_failed").detail()}
            yield failure_packet(error)
        finally:
            if run_id is not None:
                stop_run("cancelled", "request_closed")

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
        """Watch silent calls for disconnect and shield final generator cleanup."""
        try:
            version = tuple(map(
                int, scope.get("asgi", {}).get("spec_version", "2.0").split("."),
            ))
            if version < (2, 4):
                await super().__call__(scope, receive, send)
            else:
                failure = None
                # ASGI 2.4 send errors alone cannot interrupt a silent provider or tool await.
                async with anyio.create_task_group() as group:
                    async def watch_disconnect():
                        """Cancel inference promptly without waiting for another output frame."""
                        await self.listen_for_disconnect(receive)
                        group.cancel_scope.cancel()

                    group.start_soon(watch_disconnect)
                    try:
                        await self.stream_response(send)
                    except OSError:
                        failure = ClientDisconnect()
                    except Exception as exc:
                        failure = exc
                    finally:
                        group.cancel_scope.cancel()
                if failure is not None:
                    raise failure
                if self.background is not None:
                    await self.background()
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
