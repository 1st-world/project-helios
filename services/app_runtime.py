"""Open and bind the services owned by one Helios application lifespan."""

import asyncio
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from fastapi.templating import Jinja2Templates

from config import Settings
from models.app_settings import AppSettings, DEFAULT_APP_SETTINGS
from services.ai_service import AIService
from services.attachment_cleanup import AttachmentCleanup
from services.attachment_service import AttachmentService
from services.attachment_store import AttachmentStore
from services.call_usage_store import CallUsageStore
from services.context_budget import ContextBudget
from services.context_recovery import ContextRecoveryRegistry
from services.conversation_profiles import ConversationProfiles
from services.conversation_service import ConversationManager
from services.conversation_workspaces import ConversationWorkspaces
from services.file_picker_service import FilePickerService
from services.folder_picker_service import FolderPickerService
from services.memory_service import ConversationMemoryService
from services.profile_service import ProfileService
from services.settings_service import SettingsService
from services.storage_maintenance import MaintenanceSession
from services.storage_references import CALL_USAGE, MANIFEST, SUMMARY_USAGE, WORK_RUNS
from services.summary_usage_store import SummaryUsageStore
from services.token_estimator import use_tokenizer_path
from services.usage_service import UsageService
from services.work_run_store import WorkRunStore
from services.workspace_service import WorkspaceService

logger = logging.getLogger(__name__)


class AppRuntime:
    """Keep the configured services, locks, and request policy of one running app."""

    def __init__(
        self,
        settings: Settings,
        resources: ExitStack,
        *,
        maintenance: MaintenanceSession | None = None,
    ) -> None:
        """Build services and register each persistent connection for cleanup as it opens."""
        self.settings = settings
        self.maintenance = maintenance
        self.storage_root = settings.profiles_path.parent
        self.databases = {
            MANIFEST: settings.attachments_root / "manifest.sqlite3",
            WORK_RUNS: settings.work_runs_path,
            CALL_USAGE: settings.logs_root / "usage_calls.sqlite3",
            SUMMARY_USAGE: settings.logs_root / "summary_usage.sqlite3",
        }
        self.settings_service = SettingsService(
            settings.app_settings_path,
            AppSettings(**{
                name: getattr(settings, name, default)
                for name, default in DEFAULT_APP_SETTINGS.to_dict().items()
            }),
        )
        policy = self.settings_service.current
        self.profile_service = ProfileService(
            settings.profiles_path,
            app_settings=(self.settings_service.migration_origin or policy)
            if self.settings_service.source_exists else None,
        )
        self.conversation_manager = ConversationManager(
            settings.conversations_root, default_profile_id=self.default_profile_id,
        )
        self.workspace_service = WorkspaceService(settings.workspace_root)
        self.conversation_workspaces = ConversationWorkspaces(self.conversation_manager)
        self.context_recovery = ContextRecoveryRegistry()
        self.summary_usage_store = SummaryUsageStore(self.databases[SUMMARY_USAGE])
        resources.callback(self.summary_usage_store.close)
        self.call_usage_store = CallUsageStore(self.databases[CALL_USAGE])
        resources.callback(self.call_usage_store.close)
        self.work_run_store = WorkRunStore(self.databases[WORK_RUNS])
        resources.callback(self.work_run_store.close)
        self.conversation_profiles = ConversationProfiles(
            self.conversation_manager, self.profile_service, self.work_run_store,
        )

        self.attachment_store = AttachmentStore(
            settings.attachments_root, referenced=self.attachment_is_referenced,
        )
        self.attachment_service = AttachmentService(
            self.workspace_service, limits=policy.attachment_limits(), store=self.attachment_store,
        )
        self.folder_picker_service = FolderPickerService()
        self.file_picker_service = FilePickerService(self.attachment_service)
        self.attachment_cleanup = AttachmentCleanup(self.attachment_store, self.context_recovery)
        self.attachment_store.on_change = self.attachment_cleanup.notify
        self.conversation_manager.on_attachments_released = self.attachment_cleanup.notify
        self.context_recovery.on_attachments_changed = self.attachment_cleanup.notify
        self.work_run_store.on_attachments_released = self.attachment_cleanup.notify
        self.workspace_operations: dict[object, str] = {}
        self.storage_check_lock = asyncio.Lock()
        self.pause_deadline_changed = asyncio.Event()
        self.work_run_store.on_pause_changed = self.pause_deadline_changed.set

        self.import_usage()
        self.usage_service = UsageService()
        self.ai_service = AIService(
            self.usage_service, self.profile_service, summary_usage_store=self.summary_usage_store,
            call_usage_store=self.call_usage_store, work_run_store=self.work_run_store,
            max_output_tokens=policy.context_output_reserve,
        )
        self.context_budget = ContextBudget(
            policy.context_token_budget, policy.context_output_reserve, policy.max_summary_calls, reserve_output=False,
        )
        self.memory_service = ConversationMemoryService(
            self.ai_service, self.conversation_manager, policy.summary_trigger_tokens, policy.keep_recent_tokens,
            self.context_budget, memory_generation_enabled=policy.memory_generation_enabled,
            memory_use_saved_summaries=policy.memory_use_saved_summaries,
            memory_background_enabled=policy.memory_background_enabled,
        )
        self.templates = Jinja2Templates(directory=settings.templates_root)

    def default_profile_id(self) -> str | None:
        """Use the current default when a conversation is created."""
        return getattr(self.profile_service.get_active_profile(), "id", None)

    def attachment_is_referenced(self, identity: str) -> bool:
        """Combine durable references and active work before an attachment is released."""
        return (
            self.conversation_manager.attachment_is_referenced(identity)
            or self.context_recovery.attachment_is_referenced(identity)
            or self.work_run_store.attachment_is_referenced(identity)
        )

    def import_usage(self) -> None:
        """Import historical usage while leaving an incomplete ledger available for review."""
        try:
            self.call_usage_store.import_summaries(self.summary_usage_store)
        except Exception:
            self.call_usage_store.import_errors += 1
            logger.exception("Could not import historical summary usage")
        for conversation in self.conversation_manager.list():
            try:
                self.call_usage_store.import_conversation(self.conversation_manager.get(conversation["id"]))
            except Exception:
                self.call_usage_store.import_errors += 1
                logger.exception("Could not import historical conversation usage")

    async def expire_paused_runs(self) -> None:
        """Release paused payloads at their exact deadline without periodic polling."""
        failures = 0
        while True:
            self.pause_deadline_changed.clear()
            try:
                self.work_run_store.prune_paused()
                deadline = self.work_run_store.next_pause_expiry()
                failures = 0
            except (sqlite3.Error, OSError):
                failures += 1
                logger.warning("Paused checkpoint expiry was deferred because storage is unavailable.")
                deadline = datetime.now(timezone.utc).timestamp() + min(2 ** min(failures, 6), 60)
            if deadline is None:
                await self.pause_deadline_changed.wait()
            else:
                delay = max(0, deadline - datetime.now(timezone.utc).timestamp())
                try:
                    await asyncio.wait_for(self.pause_deadline_changed.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass


_runtime = ContextVar[AppRuntime]("helios_app_runtime")


def current_runtime() -> AppRuntime:
    """Return the runtime bound to the current request."""
    return _runtime.get()


@contextmanager
def bind_runtime(runtime: AppRuntime) -> Iterator[None]:
    """Keep handlers and their child tasks on the same app and tokenizer settings."""
    token = _runtime.set(runtime)
    try:
        with use_tokenizer_path(runtime.settings.tokenizer_path):
            yield
    finally:
        _runtime.reset(token)


def configure_logging(settings: Settings, resources: ExitStack) -> None:
    """Set up server logging when the host has not already configured it."""
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return
    settings.logs_root.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        settings.logs_root / "helios.log", maxBytes=5 * 1024 * 1024, backupCount=10, encoding="utf-8",
    )
    handlers = [file_handler, logging.StreamHandler()]
    previous_level = root_logger.level
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers,
    )
    watch_logger = logging.getLogger("watchfiles")
    resources.callback(watch_logger.setLevel, watch_logger.level)
    watch_logger.setLevel(logging.WARNING)
    resources.callback(root_logger.setLevel, previous_level)
    for handler in handlers:
        resources.callback(handler.close)
        resources.callback(root_logger.removeHandler, handler)
