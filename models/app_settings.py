"""Define validated application policy defaults independently of connection profiles."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AppSettings:
    """Hold immutable user preferences for context work and local file resources."""

    # Null suggests an automatic threshold for enabled profiles; zero pauses memory.
    summary_trigger_tokens: int | None = None
    keep_recent_tokens: int = 65_536
    context_token_budget: int = 262_144
    # Omit the provider output cap unless the user explicitly chooses one.
    context_output_reserve: int | None = None
    max_summary_calls: int = 8
    context_preflight_mode: str = "warn"
    context_input_budget_mode: str = "unlimited"
    context_input_budget: int | None = None
    # Resource defaults do not guarantee peak memory or latency across workloads.
    # Count every local file independently of provider image capacity.
    attachment_max_files: int = 50
    # Keep a provisional mixed-format raw-read budget of about 50 MB.
    # A shared raw-read budget does not bound base64/JSON/parser peak memory.
    attachment_max_file_bytes: int = 49_999_999
    attachment_max_total_bytes: int = 49_999_999
    # Bound text retained for tokenization and prompts, not model input tokens.
    attachment_max_text_bytes: int = 4 * 1024 * 1024
    # Bound Office ZIP expansion independently of compressed source size.
    attachment_max_zip_bytes: int = 128 * 1024 * 1024
    attachment_max_zip_members: int = 10_000
    # RGB/RGBA pixels alone need about 150-200 MB at this provisional count.
    attachment_max_image_pixels: int = 50_000_000
    # Bound page-object traversal, not Azure capacity or benchmarked latency.
    attachment_max_pdf_pages: int = 1024
    # Count stored cells instead of empty rectangular gaps as parser work.
    attachment_max_sheet_cells: int = 100_000

    def __post_init__(self) -> None:
        """Reject invalid individual values and inconsistent context work budgets."""
        if self.context_preflight_mode not in {"warn", "block", "off"}:
            raise ValueError("Context preflight mode must be warn, block, or off.")
        if self.context_input_budget_mode not in {"unlimited", "custom", "legacy"}:
            raise ValueError("Input budget mode must be unlimited, custom, or legacy.")
        if (self.context_input_budget_mode == "custom") != (
            self.context_input_budget is not None
        ):
            raise ValueError("Only a custom input budget requires a positive token limit.")
        for name in AppSettings.__dataclass_fields__:
            if name in {"context_preflight_mode", "context_input_budget_mode"}:
                continue
            value = getattr(self, name)
            if value is None and name in {
                "context_output_reserve", "summary_trigger_tokens",
                "context_input_budget",
            }:
                continue
            minimum = 0 if name == "summary_trigger_tokens" else 1
            if type(value) is not int or value < minimum:
                if minimum == 0:
                    raise ValueError("Summary trigger tokens must be a nonnegative integer.")
                raise ValueError(f"{name} must be a positive integer.")
        if self.context_token_budget < 1024:
            raise ValueError("Summary work budget must be at least 1024 tokens.")
        if (
            self.context_input_budget_mode == "legacy"
            and self.context_token_budget - (self.context_output_reserve or 0) < 1024
        ):
            raise ValueError("Context token budget must exceed the output reserve by at least 1024.")
        if self.summary_trigger_tokens and self.keep_recent_tokens >= self.summary_trigger_tokens:
            raise ValueError("Keep recent tokens must be smaller than the enabled summary trigger.")

    def to_dict(self) -> dict:
        """Return policy fields without including subclass storage or asset paths."""
        return {
            name: getattr(self, name)
            for name in AppSettings.__dataclass_fields__
        }

    def attachment_limits(self) -> dict:
        """Translate resource preferences into AttachmentService limit names."""
        return {
            name.removeprefix("attachment_").upper(): value
            for name, value in self.to_dict().items()
            if name.startswith("attachment_max_")
        }


DEFAULT_APP_SETTINGS = AppSettings()
