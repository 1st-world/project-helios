"""Validate application preferences and publish versioned saves without losing legacy originals."""

from dataclasses import replace
from pathlib import Path

from models.app_settings import AppSettings, DEFAULT_APP_SETTINGS
from models.input_budget import normalize_input_budget_settings
from services.json_storage import JsonDocumentStore, JsonStorageError


class SettingsStorageError(JsonStorageError):
    """Report settings persistence failures without discarding saved preferences."""


class SettingsService:
    """Manage immutable preferences separately from model connection profiles."""

    def __init__(
        self, storage_path: Path, defaults: AppSettings = DEFAULT_APP_SETTINGS,
    ) -> None:
        """Load and validate preferences without writing a migration during startup."""
        self.storage_path = storage_path
        self.defaults = defaults
        self.current = defaults
        self.migration_origin: AppSettings | None = None
        self.source_exists = False
        try:
            self._document = JsonDocumentStore(
                storage_path, "settings", max_bytes=65_536,
            )
            if self._document.payload is None:
                return
            self.source_exists = True
            values = self._document.payload
            if self._document.version == 0:
                values = self._migrate_legacy(values)
                self.current = self.prepare_update(values)
                self.migration_origin = self.current
            else:
                if set(values) != set(defaults.to_dict()):
                    raise ValueError("Stored settings fields do not match their version.")
                self.current = AppSettings(**values)
                if self._document.migration is not None:
                    policy = self._document.migration["policy"]
                    if (
                        set(policy) != {"settings"}
                        or set(policy["settings"]) != set(defaults.to_dict())
                    ):
                        raise ValueError("Invalid settings migration policy.")
                    self.migration_origin = AppSettings(**policy["settings"])
        except JsonStorageError as exc:
            raise SettingsStorageError(str(exc), reason=exc.reason) from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise SettingsStorageError("Application settings are invalid; repair settings.json before starting.", reason="invalid_document") from None

    @staticmethod
    def _migrate_legacy(values: dict) -> dict:
        """Preserve explicit token policy and mark ambiguous historical input defaults."""
        values = dict(values)
        legacy = {"max_context_messages", "keep_recent_messages"} & values.keys()
        if legacy:
            trigger = values.get("max_context_messages", 16)
            recent = values.get("keep_recent_messages", 10)
            if (
                type(trigger) is not int or type(recent) is not int
                or not 2 <= recent < trigger
            ):
                raise ValueError("Legacy message-count settings are invalid.")
            # Original counts stay in the exact backup; they have no reliable token equivalent.
            values = {
                name: value for name, value in values.items() if name not in legacy
            }
        if not {"context_input_budget_mode", "context_input_budget"} & values.keys():
            values["context_input_budget_mode"] = "legacy"
        return values

    def prepare_update(self, changes: dict) -> AppSettings:
        """Validate a partial update; omitted fields stay and API null restores defaults."""
        if set(changes) - set(self.defaults.to_dict()):
            raise ValueError("Unknown application setting.")
        changes = normalize_input_budget_settings(changes, default_mode="unlimited")
        values = {
            name: getattr(self.defaults, name) if value is None else value
            for name, value in changes.items()
        }
        return replace(self.current, **values)

    def save(self, candidate: AppSettings) -> None:
        """Preserve the legacy original and publish only after a successful atomic save."""
        try:
            candidate = AppSettings(**candidate.to_dict())
            self._document.save(
                candidate.to_dict(),
                migration_policy={"settings": self.migration_origin.to_dict()}
                if self.migration_origin is not None else None,
            )
        except JsonStorageError as exc:
            raise SettingsStorageError(str(exc), reason=exc.reason) from None
        self.current = candidate
        self.source_exists = True
