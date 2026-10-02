"""Define validated application policy defaults independently of connection profiles."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AppSettings:
    """Hold immutable user preferences for context work and local file resources."""

    max_context_messages: int = 16
    keep_recent_messages: int = 10
    context_token_budget: int = 32_768
    context_output_reserve: int = 4096
    max_summary_calls: int = 8
    context_preflight_mode: str = "warn"
    # Resource defaults are provisional policies without workload benchmarks.
    # Match Azure's image count initially, but count every local file here.
    attachment_max_files: int = 50
    # Begin below Azure's decimal 50 MB native-input file ceiling.
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
        for name in AppSettings.__dataclass_fields__:
            if name == "context_preflight_mode":
                continue
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        if self.context_token_budget - self.context_output_reserve < 1024:
            raise ValueError("Context token budget must exceed the output reserve by at least 1024.")
        if not 2 <= self.keep_recent_messages < self.max_context_messages:
            raise ValueError("Keep recent messages must be at least 2 and smaller than max context messages.")

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
