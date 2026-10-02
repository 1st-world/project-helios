"""Load environment-based application settings and define project storage and asset paths."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    """Hold environment-derived context limits and project resource paths."""

    max_context_messages: int = int(
        os.getenv("HELIOS_MAX_CONTEXT_MESSAGES", "16")
    )
    keep_recent_messages: int = int(
        os.getenv("HELIOS_KEEP_RECENT_MESSAGES", "10")
    )
    context_token_budget: int = int(
        os.getenv("HELIOS_CONTEXT_TOKEN_BUDGET", "32768")
    )
    context_output_reserve: int = int(
        os.getenv("HELIOS_CONTEXT_OUTPUT_RESERVE", "4096")
    )
    max_summary_calls: int = int(
        os.getenv("HELIOS_MAX_SUMMARY_CALLS", "8")
    )
    # Environment values currently supply application-wide defaults.
    # Profiles store model declarations and per-profile preflight overrides.
    context_preflight_mode: str = os.getenv(
        "HELIOS_CONTEXT_PREFLIGHT_MODE", "warn"
    )
    # Local parser/read policies are separate from provider input requirements.
    # See `AttachmentService` constants for each provisional default's rationale.
    # User resource preferences belong in persistent app settings for the GUI.
    # Connection profiles and `.env` are separate from those app preferences.
    attachment_max_files: int = int(
        os.getenv("HELIOS_ATTACHMENT_MAX_FILES", "50")
    )
    attachment_max_file_bytes: int = int(
        os.getenv("HELIOS_ATTACHMENT_MAX_FILE_BYTES", "49999999")
    )
    attachment_max_total_bytes: int = int(
        os.getenv("HELIOS_ATTACHMENT_MAX_TOTAL_BYTES", "49999999")
    )
    attachment_max_text_bytes: int = int(
        os.getenv("HELIOS_ATTACHMENT_MAX_TEXT_BYTES", "4194304")
    )
    attachment_max_zip_bytes: int = int(
        os.getenv("HELIOS_ATTACHMENT_MAX_ZIP_BYTES", "134217728")
    )
    attachment_max_zip_members: int = int(
        os.getenv("HELIOS_ATTACHMENT_MAX_ZIP_MEMBERS", "10000")
    )
    attachment_max_image_pixels: int = int(
        os.getenv("HELIOS_ATTACHMENT_MAX_IMAGE_PIXELS", "50000000")
    )
    attachment_max_pdf_pages: int = int(
        os.getenv("HELIOS_ATTACHMENT_MAX_PDF_PAGES", "1024")
    )
    attachment_max_sheet_cells: int = int(
        os.getenv("HELIOS_ATTACHMENT_MAX_SHEET_CELLS", "100000")
    )
    tokenizer_path: Path = Path(
        os.getenv("HELIOS_TOKENIZER_PATH",
            str(PROJECT_ROOT / "logs/tokenizers/o200k_base.tiktoken"),
        )
    )
    workspace_root: Path = PROJECT_ROOT / "workspace"
    conversations_root: Path = PROJECT_ROOT / "conversation"
    logs_root: Path = PROJECT_ROOT / "logs"
    profiles_path: Path = PROJECT_ROOT / "profiles.json"
    static_root: Path = PROJECT_ROOT / "static"
    templates_root: Path = PROJECT_ROOT / "templates"

    def __post_init__(self) -> None:
        """Reject invalid policy and local resource settings during configuration."""
        if self.context_preflight_mode not in {"warn", "block", "off"}:
            raise ValueError("HELIOS_CONTEXT_PREFLIGHT_MODE must be warn, block, or off.")
        if any(
            value < 1 for name, value in vars(self).items()
            if name.startswith("attachment_max_")
        ):
            raise ValueError("Attachment resource limits must be positive.")


settings = Settings()
