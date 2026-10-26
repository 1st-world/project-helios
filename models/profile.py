"""Represent Azure OpenAI connection settings and optional usage pricing."""

import math
from dataclasses import asdict, dataclass, field
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


@dataclass(frozen=True)
class LegacyInputBoundary:
    """Retain the original effective input boundary separately from editable model facts."""

    input_limit: int | None
    declared_model_input_limit: int | None
    app_output_token_limit: int | None
    profile_output_token_limit: int | None
    output_token_limit: int | None
    app_input_limit: int | None
    profile_input_budget: int | None

    def __post_init__(self) -> None:
        """Reject malformed historical boundaries without echoing stored values."""
        for name, value in self.to_dict().items():
            minimum = 0 if name in {
                "input_limit", "declared_model_input_limit", "app_input_limit",
            } else 1
            if value is not None and (type(value) is not int or value < minimum):
                raise ValueError("Invalid legacy input boundary.")
        output = min(
            (
                value for value in (
                    self.app_output_token_limit, self.profile_output_token_limit,
                ) if value is not None
            ),
            default=None,
        )
        budget = (
            self.profile_input_budget
            if self.profile_input_budget is not None else self.app_input_limit
        )
        effective = min(
            (
                value for value in (budget, self.declared_model_input_limit)
                if value is not None
            ),
            default=None,
        )
        if self.output_token_limit != output or self.input_limit != effective:
            raise ValueError("Legacy input evidence is inconsistent.")

    def to_dict(self) -> dict:
        """Serialize policy evidence without including connection credentials."""
        return asdict(self)


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
    legacy_input_boundary: LegacyInputBoundary | None = field(default=None, repr=False)
    reasoning_summary: str = "off"

    def __post_init__(self) -> None:
        """Validate optional declarations and user limits without guessing from deployment names."""
        if self.reasoning_summary not in {"off", "auto"}:
            raise ValueError("Reasoning summary must be off or auto.")
        for name in ("id", "name", "endpoint", "api_key", "deployment"):
            if not isinstance(getattr(self, name), str):
                raise ValueError("Connection fields must be strings.")
        if not self.id.strip():
            raise ValueError("Profile identifiers must not be empty.")
        if self.legacy_input_boundary is not None and not isinstance(
            self.legacy_input_boundary, LegacyInputBoundary,
        ):
            raise ValueError("Invalid legacy input boundary.")
        for name in self.__dataclass_fields__:
            if not name.endswith("price_per_million"):
                continue
            value = getattr(self, name)
            if value is not None and (
                type(value) not in {int, float} or not math.isfinite(value) or value < 0
            ):
                raise ValueError("Usage prices must be finite nonnegative numbers or null.")
        if self.long_context_threshold is not None and (
            type(self.long_context_threshold) is not int
            or self.long_context_threshold < 1
        ):
            raise ValueError("The long-context threshold must be a positive integer or null.")
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

    def request_settings(self) -> dict:
        """Return private request settings without the display-only profile name."""
        return {
            key: value for key, value in asdict(self).items() if key != "name"
        }

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
            "reasoning_summary": self.reasoning_summary,
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
            reasoning_summary=data.get("reasoning_summary", "off"),
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
