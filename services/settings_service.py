"""Persist validated application preferences atomically without environment overrides."""

import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import NamedTemporaryFile

from models.app_settings import AppSettings, DEFAULT_APP_SETTINGS


class SettingsStorageError(RuntimeError):
    """Report settings read or write failures without discarding saved preferences."""


class SettingsService:
    """Manage immutable preferences separately from model connection profiles."""

    def __init__(
        self, storage_path: Path, defaults: AppSettings = DEFAULT_APP_SETTINGS
    ) -> None:
        """Load saved preferences or use defaults without creating a file."""
        self.storage_path = storage_path
        self.defaults = defaults
        self.current = defaults
        try:
            with storage_path.open("rb") as source:
                data = source.read(65_537)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SettingsStorageError("Could not read application settings.") from exc
        try:
            if len(data) > 65_536:
                raise ValueError("Settings file exceeds 64 KiB.")
            values = json.loads(data)
            if not isinstance(values, dict):
                raise ValueError("Settings must be an object.")
            self.current = self.prepare_update(values)
        except (UnicodeError, ValueError, TypeError) as exc:
            raise SettingsStorageError("Application settings are invalid; repair settings.json before starting.") from exc

    def prepare_update(self, changes: dict) -> AppSettings:
        """Validate a partial update; omitted fields stay and null restores defaults."""
        if set(changes) - set(self.defaults.to_dict()):
            raise ValueError("Unknown application setting.")
        values = {
            name: getattr(self.defaults, name) if value is None else value
            for name, value in changes.items()
        }
        return replace(self.current, **values)

    def save(self, candidate: AppSettings) -> None:
        """Publish preferences only after replacing the saved file successfully."""
        temporary = None
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.storage_path.parent,
                prefix=".helios-settings-", suffix=".tmp", delete=False,
            ) as output:
                temporary = Path(output.name)
                json.dump(candidate.to_dict(), output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.storage_path)
        except OSError as exc:
            raise SettingsStorageError("Could not save application settings.") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # Cleanup must not conceal the failed settings write.
                    pass
        self.current = candidate
