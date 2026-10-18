"""Bound summary work and estimate text/visual input independently of provider admission."""

import json
from dataclasses import dataclass

from models.app_settings import DEFAULT_APP_SETTINGS
from services.token_estimator import text_tokens
from services.provider_errors import ContextWindowExceeded


class ContextBudgetExceeded(ValueError):
    """The request cannot fit without discarding unsummarized content."""


class ContextChangedError(RuntimeError):
    """The conversation changed while its request was being prepared."""


class SummaryUnavailableError(RuntimeError):
    """A complete summary could not be produced within the work budget."""


@dataclass(frozen=True)
class ContextBudget:
    """Bound summary work, retaining optional output reservation for legacy callers."""

    # These are local working limits, not advertised model context sizes.
    token_budget: int = DEFAULT_APP_SETTINGS.context_token_budget
    output_reserve: int | None = None
    max_summary_calls: int = 8
    reserve_output: bool = True

    def __post_init__(self) -> None:
        """Reject limits without enough input capacity or an allowed summary call."""
        if (
            (self.output_reserve is not None and self.output_reserve < 1)
            or self.input_limit < 1_024
        ):
            raise ValueError(
                "Context token budget must exceed the output reserve by at least 1024."
            )
        if self.max_summary_calls < 1:
            raise ValueError("At least one summary call must be allowed.")

    @property
    def input_limit(self) -> int:
        """Return the working input allowance after any explicit output reservation."""
        return self.token_budget - (
            (self.output_reserve or 0) if self.reserve_output else 0
        )

    @staticmethod
    def message_tokens(role: str, content: str) -> int:
        """Estimate one stored text message with the same framing used for requests."""
        return 32 + text_tokens(role) + text_tokens(content)

    @staticmethod
    def estimate(
        instructions: str, inputs: list[dict], *, visual_tokens: int = 0,
        tools: list[dict] | None = None,
    ) -> int:
        """Estimate text and framing with supplied visual proxies, excluding encoded payloads."""
        estimate = 256 + text_tokens(instructions) + visual_tokens
        if tools:
            estimate += text_tokens(json.dumps(tools, ensure_ascii=False))
        has_visual = False
        for item in inputs:
            if item.get("type") is not None:
                estimate += 32 + text_tokens(json.dumps(item, ensure_ascii=False))
                continue
            content = item["content"]
            if isinstance(content, str):
                estimate += ContextBudget.message_tokens(item["role"], content)
                continue
            estimate += 32 + text_tokens(item["role"])
            for part in content:
                estimate += 32
                if part["type"] == "input_text":
                    estimate += text_tokens(part["text"])
                elif part["type"] in {"input_image", "input_file"}:
                    has_visual = True
                else:
                    raise ValueError("Unsupported context content part.")
        if has_visual and visual_tokens <= 0:
            raise ValueError("Visual input requires an attachment token estimate.")
        return estimate
