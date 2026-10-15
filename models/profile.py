"""Represent Azure OpenAI connection settings and optional usage pricing."""

from dataclasses import dataclass, field
from uuid import uuid4


# GPT-6 long-context pricing starts above this input-token breakpoint.
DEFAULT_LONG_CONTEXT_THRESHOLD = 272_000


CONTEXT_FIELDS = (
    "context_preflight_mode", "context_input_budget_mode", "context_input_budget",
    "model_context_window",
    "model_max_input_tokens", "output_token_limit",
    "declared_model_max_output_tokens",
    "memory_output_headroom",
)

PROFILE_POLICY_FIELDS = (*CONTEXT_FIELDS, "memory_enabled")


def normalize_output_limit_settings(settings: dict) -> dict:
    """Move the deprecated output-cap alias into its canonical field, rejecting conflicts."""
    normalized = dict(settings)
    if "model_max_output_tokens" not in normalized:
        return normalized
    legacy_limit = normalized.pop("model_max_output_tokens")
    if (
        "output_token_limit" in normalized
        and (
            type(normalized["output_token_limit"]) is not type(legacy_limit)
            or normalized["output_token_limit"] != legacy_limit
        )
    ):
        raise ValueError("Output token limit and its deprecated alias must match.")
    normalized.setdefault("output_token_limit", legacy_limit)
    return normalized


@dataclass
class ConnectionProfile:
    """Hold connection credentials and optional ordinary, cache, and long-context rates."""

    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = "Default Azure Profile"
    endpoint: str = ""
    api_key: str = ""
    deployment: str = ""
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    long_context_threshold: int | None = DEFAULT_LONG_CONTEXT_THRESHOLD
    long_input_price_per_million: float | None = None
    long_output_price_per_million: float | None = None
    cache_read_price_per_million: float | None = None
    cache_write_price_per_million: float | None = None
    long_cache_read_price_per_million: float | None = None
    long_cache_write_price_per_million: float | None = None
    context_preflight_mode: str | None = None
    context_input_budget: int | None = None
    model_context_window: int | None = None
    model_max_input_tokens: int | None = None
    output_token_limit: int | None = None
    declared_model_max_output_tokens: int | None = None
    context_input_budget_mode: str | None = None
    memory_enabled: bool | None = False
    memory_output_headroom: int | None = None

    def __post_init__(self) -> None:
        """Validate optional declarations and user limits without guessing from deployment names."""
        if self.memory_enabled is not None and type(self.memory_enabled) is not bool:
            raise ValueError("Memory enabled must be a boolean or null for unreviewed settings.")
        if self.context_preflight_mode not in {None, "warn", "block", "off"}:
            raise ValueError("Context preflight mode must be warn, block, off, or null.")
        if self.context_input_budget_mode is None:
            self.context_input_budget_mode = (
                "custom" if self.context_input_budget is not None else "inherit"
            )
        if self.context_input_budget_mode not in {
            "inherit", "unlimited", "custom", "legacy",
        }:
            raise ValueError("Input budget mode must be inherit, unlimited, custom, or legacy.")
        if (
            self.context_input_budget_mode == "custom"
            and self.context_input_budget is None
        ):
            raise ValueError("A custom input budget requires a positive token limit.")
        if (
            self.context_input_budget_mode in {"inherit", "unlimited"}
            and self.context_input_budget is not None
        ):
            raise ValueError("Only a custom input budget can specify a token limit.")
        for name in CONTEXT_FIELDS[2:]:
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError("Context limits must be positive integers or null.")

    @property
    def is_configured(self) -> bool:
        """Return whether endpoint, API key, and deployment are all populated."""
        return bool(
            self.endpoint.strip()
            and self.api_key.strip()
            and self.deployment.strip()
        )

    def to_dict(
        self,
        include_sensitive: bool = False,
        *,
        include_legacy_output_limit: bool = False,
    ) -> dict:
        """Serialize profile settings, exposing the API key only when explicitly requested."""
        data = {
            "id": self.id,
            "name": self.name,
            "endpoint": self.endpoint,
            "deployment": self.deployment,
            "input_price_per_million": self.input_price_per_million,
            "output_price_per_million": self.output_price_per_million,
            "long_context_threshold": self.long_context_threshold,
            "long_input_price_per_million": self.long_input_price_per_million,
            "long_output_price_per_million": self.long_output_price_per_million,
            "cache_read_price_per_million": self.cache_read_price_per_million,
            "cache_write_price_per_million": self.cache_write_price_per_million,
            "long_cache_read_price_per_million": self.long_cache_read_price_per_million,
            "long_cache_write_price_per_million": self.long_cache_write_price_per_million,
            "is_configured": self.is_configured,
            "has_api_key": bool(self.api_key.strip()),
            **{name: getattr(self, name) for name in PROFILE_POLICY_FIELDS},
        }
        if include_sensitive:
            data["api_key"] = self.api_key
        if include_legacy_output_limit:
            data["model_max_output_tokens"] = self.output_token_limit
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ConnectionProfile":
        """Restore profile settings, normalizing strings and optional numeric rates."""
        input_price = data.get("input_price_per_million")
        output_price = data.get("output_price_per_million")
        threshold = data.get("long_context_threshold")
        long_in = data.get("long_input_price_per_million")
        long_out = data.get("long_output_price_per_million")
        cache_read = data.get("cache_read_price_per_million")
        cache_write = data.get("cache_write_price_per_million")
        long_cache_read = data.get("long_cache_read_price_per_million")
        long_cache_write = data.get("long_cache_write_price_per_million")
        context_settings = {name: data.get(name) for name in CONTEXT_FIELDS}
        context_settings["memory_enabled"] = data.get("memory_enabled")
        context_settings["context_input_budget_mode"] = data.get(
            "context_input_budget_mode", "legacy"
        )
        if "output_token_limit" not in data:
            context_settings["output_token_limit"] = data.get(
                "model_max_output_tokens"
            )
        return cls(
            id=str(data.get("id") or uuid4()),
            name=str(data.get("name") or "Azure Profile").strip(),
            endpoint=str(data.get("endpoint") or "").strip(),
            api_key=str(data.get("api_key") or "").strip(),
            deployment=str(data.get("deployment") or "").strip(),
            input_price_per_million=(
                float(input_price)
                if input_price is not None and str(input_price).strip() != ""
                else None
            ),
            output_price_per_million=(
                float(output_price)
                if output_price is not None and str(output_price).strip() != ""
                else None
            ),
            long_context_threshold=(
                int(threshold)
                if threshold is not None and str(threshold).strip() != ""
                else DEFAULT_LONG_CONTEXT_THRESHOLD
            ),
            long_input_price_per_million=(
                float(long_in)
                if long_in is not None and str(long_in).strip() != ""
                else None
            ),
            long_output_price_per_million=(
                float(long_out)
                if long_out is not None and str(long_out).strip() != ""
                else None
            ),
            cache_read_price_per_million=(
                float(cache_read)
                if cache_read is not None and str(cache_read).strip() != ""
                else None
            ),
            cache_write_price_per_million=(
                float(cache_write)
                if cache_write is not None and str(cache_write).strip() != ""
                else None
            ),
            long_cache_read_price_per_million=(
                float(long_cache_read)
                if long_cache_read is not None
                and str(long_cache_read).strip() != ""
                else None
            ),
            long_cache_write_price_per_million=(
                float(long_cache_write)
                if long_cache_write is not None
                and str(long_cache_write).strip() != ""
                else None
            ),
            **context_settings,
        )
