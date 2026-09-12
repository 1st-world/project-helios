"""Application settings loaded from the environment."""

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    max_context_messages: int = int(os.getenv("HELIOS_MAX_CONTEXT_MESSAGES", "16"))
    keep_recent_messages: int = int(os.getenv("HELIOS_KEEP_RECENT_MESSAGES", "10"))
    context_token_budget: int = int(os.getenv("HELIOS_CONTEXT_TOKEN_BUDGET", "32768"))
    context_output_reserve: int = int(os.getenv("HELIOS_CONTEXT_OUTPUT_RESERVE", "4096"))
    max_summary_calls: int = int(os.getenv("HELIOS_MAX_SUMMARY_CALLS", "8"))
    workspace_root: Path = PROJECT_ROOT / "workspace"
    conversations_root: Path = PROJECT_ROOT / "conversation"
    logs_root: Path = PROJECT_ROOT / "logs"
    profiles_path: Path = PROJECT_ROOT / "profiles.json"
    static_root: Path = PROJECT_ROOT / "static"
    templates_root: Path = PROJECT_ROOT / "templates"


settings = Settings()
