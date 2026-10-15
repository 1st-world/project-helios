"""Separate optional estimated-input admission from model declarations and resource limits."""

from dataclasses import dataclass

from models.profile import ConnectionProfile
from services.context_budget import ContextBudget
from services.token_estimator import estimation_method


PREFLIGHT_MODES = {"warn", "block", "off"}


@dataclass(frozen=True)
class ContextPolicy:
    """Describe a request's selected estimate policy without inferring deployment capabilities."""

    mode: str
    input_budget: int | None
    declared_model_input_limit: int | None = None
    max_output_tokens: int | None = None
    declared_model_max_output_tokens: int | None = None
    input_budget_source: str = "none"
    legacy_input_policy: bool = False

    def __post_init__(self) -> None:
        """Reject invalid estimate policy settings before provider access."""
        if (
            self.mode not in PREFLIGHT_MODES
            or (
                self.input_budget is not None
                and self.input_budget < (0 if self.legacy_input_policy else 1)
            )
            or (self.max_output_tokens is not None and self.max_output_tokens < 1)
        ):
            raise ValueError("Context policy requires a valid mode and an optional positive input budget.")

    @classmethod
    def resolve(
        cls,
        profile: ConnectionProfile,
        budget: ContextBudget,
        default_mode: str,
        mode: str | None = None,
        input_budget: int | None = None,
        *,
        input_budget_mode: str | None = None,
        default_input_budget_mode: str = "unlimited",
        default_input_budget: int | None = None,
    ) -> "ContextPolicy":
        """Apply request, profile, and application settings in that order."""
        limits = []
        output_limits = (
            value
            for value in (budget.output_reserve, profile.output_token_limit)
            if value is not None
        )
        output = min(output_limits, default=None)
        if profile.model_max_input_tokens:
            limits.append(profile.model_max_input_tokens)
        if profile.model_context_window:
            limits.append(max(0, profile.model_context_window - (output or 0)))
        declared = min(limits) if limits else None
        legacy = default_input_budget_mode == "legacy"
        if legacy:
            selected = budget.token_budget - (budget.output_reserve or 0)
        elif default_input_budget_mode == "custom":
            selected = default_input_budget
        else:
            selected = None
        source = "app" if selected is not None else "none"
        profile_mode = profile.context_input_budget_mode
        if profile_mode in {"custom", "unlimited", "legacy"}:
            legacy = profile_mode == "legacy"
            if profile_mode == "unlimited":
                selected, source = None, "profile"
            elif profile.context_input_budget is not None:
                selected, source = profile.context_input_budget, "profile"
        request_mode = input_budget_mode or (
            "custom" if input_budget is not None else "inherit"
        )
        if request_mode not in {"inherit", "custom", "unlimited"}:
            raise ValueError("Request input budget mode must be inherit, custom, or unlimited.")
        if request_mode != "inherit":
            legacy = False
            selected = input_budget if request_mode == "custom" else None
            source = "request"
            if request_mode == "custom" and selected is None:
                raise ValueError("A custom input budget requires a positive token limit.")
        if legacy and declared is not None:
            selected = min(selected, declared) if selected is not None else declared
        if legacy:
            source = "legacy"
        return cls(
            mode or profile.context_preflight_mode or default_mode,
            selected,
            declared,
            output,
            profile.declared_model_max_output_tokens,
            source,
            legacy,
        )

    def assess(
        self, instructions: str, inputs: list[dict], *, visual_tokens: int = 0
    ) -> dict:
        """Report uncertain estimates; only explicit block mode rejects an estimate."""
        estimate = (
            None
            if self.mode == "off" or self.input_budget is None
            else ContextBudget.estimate(
                instructions, inputs, visual_tokens=visual_tokens
            )
        )
        exceeded = (
            estimate is not None and self.input_budget is not None
            and estimate > self.input_budget
        )
        model_exceeded = (
            estimate is not None
            and self.declared_model_input_limit is not None
            and estimate > self.declared_model_input_limit
        )
        return {
            "mode": self.mode,
            "input_budget": self.input_budget,
            "input_budget_source": self.input_budget_source,
            "legacy_input_policy": self.legacy_input_policy,
            "declared_model_input_limit": self.declared_model_input_limit,
            "max_output_tokens": self.max_output_tokens,
            "declared_model_max_output_tokens": self.declared_model_max_output_tokens,
            "estimated_input_tokens": estimate,
            "estimate_is_exact": False,
            "estimation_method": (
                estimation_method() if estimate is not None else "disabled"
            ),
            "visual_estimate_is_exact": False if visual_tokens else None,
            "budget_exceeded": exceeded,
            "declared_model_limit_exceeded": model_exceeded,
            "blocked": self.mode == "block" and exceeded,
            "warning": self.mode == "warn" and exceeded,
        }
