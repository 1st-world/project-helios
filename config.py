"""Define local storage and asset paths with bootstrap application policy defaults."""

from dataclasses import dataclass
from pathlib import Path

from models.app_settings import AppSettings

PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings(AppSettings):
    """Keep project paths separate from preferences persisted in settings.json."""

    tokenizer_path: Path = PROJECT_ROOT / "logs/tokenizers/o200k_base.tiktoken"
    workspace_root: Path = PROJECT_ROOT / "workspace"
    conversations_root: Path = PROJECT_ROOT / "conversation"
    logs_root: Path = PROJECT_ROOT / "logs"
    profiles_path: Path = PROJECT_ROOT / "profiles.json"
    static_root: Path = PROJECT_ROOT / "static"
    templates_root: Path = PROJECT_ROOT / "templates"

    @property
    def app_settings_path(self) -> Path:
        """Place application preferences alongside connection-profile storage."""
        return self.profiles_path.with_name("settings.json")


settings = Settings()
