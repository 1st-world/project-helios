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
    input_budget: int
    declared_model_input_limit: int | None = None
    max_output_tokens: int = 4096

    def __post_init__(self) -> None:
        """Reject invalid estimate policy settings before provider access."""
        if (
            self.mode not in PREFLIGHT_MODES
            or self.input_budget < 1
            or self.max_output_tokens < 1
        ):
            raise ValueError("Context policy requires warn, block, or off and a positive input budget.")

    @classmethod
    def resolve(
        cls,
        profile: ConnectionProfile,
        budget: ContextBudget,
        default_mode: str,
        mode: str | None = None,
        input_budget: int | None = None,
    ) -> "ContextPolicy":
        """Apply request, profile, and application settings in that order."""
        limits = []
        output = min(
            budget.output_reserve,
            profile.model_max_output_tokens or budget.output_reserve,
        )
        if profile.model_max_input_tokens:
            limits.append(profile.model_max_input_tokens)
        if profile.model_context_window:
            limits.append(max(0, profile.model_context_window - output))
        return cls(
            mode or profile.context_preflight_mode or default_mode,
            input_budget or profile.context_input_budget or budget.input_limit,
            min(limits) if limits else None,
            output,
        )

    def assess(
        self, instructions: str, inputs: list[dict], *, visual_tokens: int = 0
    ) -> dict:
        """Report uncertain estimates; only explicit block mode rejects an estimate."""
        estimate = (
            None
            if self.mode == "off"
            else ContextBudget.estimate(
                instructions, inputs, visual_tokens=visual_tokens
            )
        )
        exceeded = estimate is not None and estimate > self.input_budget
        model_exceeded = (
            estimate is not None
            and self.declared_model_input_limit is not None
            and estimate > self.declared_model_input_limit
        )
        return {
            "mode": self.mode,
            "input_budget": self.input_budget,
            "declared_model_input_limit": self.declared_model_input_limit,
            "max_output_tokens": self.max_output_tokens,
            "estimated_input_tokens": estimate,
            "estimate_is_exact": False,
            "estimation_method": (
                estimation_method() if estimate is not None else "disabled"
            ),
            "visual_estimate_is_exact": False if visual_tokens else None,
            "budget_exceeded": exceeded,
            "declared_model_limit_exceeded": model_exceeded,
            "blocked": self.mode == "block" and (exceeded or model_exceeded),
            "warning": self.mode == "warn" and (exceeded or model_exceeded),
        }
