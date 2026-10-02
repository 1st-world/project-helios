"""Define local context limits and text/visual estimates independent of model pricing."""

from dataclasses import dataclass

from services.token_estimator import text_tokens


class ContextBudgetExceeded(ValueError):
    """The request cannot fit without discarding unsummarized content."""


class ContextChangedError(RuntimeError):
    """The conversation changed while its request was being prepared."""


class SummaryUnavailableError(RuntimeError):
    """A complete summary could not be produced within the work budget."""


class ContextWindowExceeded(RuntimeError):
    """The provider explicitly rejected the request's context length."""


@dataclass(frozen=True)
class ContextBudget:
    """Reserve output capacity within local working limits and bound summary calls."""

    # These are local working limits, not advertised model context sizes.
    token_budget: int = 32_768
    output_reserve: int = 4_096
    max_summary_calls: int = 8

    def __post_init__(self) -> None:
        """Reject limits without enough input capacity or an allowed summary call."""
        if (
            self.output_reserve < 1
            or self.token_budget - self.output_reserve < 1_024
        ):
            raise ValueError(
                "Context token budget must exceed the output reserve by at least 1024."
            )
        if self.max_summary_calls < 1:
            raise ValueError("At least one summary call must be allowed.")

    @property
    def input_limit(self) -> int:
        """Return the input allowance after reserving output capacity."""
        return self.token_budget - self.output_reserve

    @staticmethod
    def estimate(
        instructions: str, inputs: list[dict], *, visual_tokens: int = 0
    ) -> int:
        """Estimate text and framing with supplied visual proxies, excluding encoded payloads."""
        estimate = 256 + text_tokens(instructions) + visual_tokens
        has_visual = False
        for item in inputs:
            estimate += 32 + text_tokens(item["role"])
            content = item["content"]
            if isinstance(content, str):
                estimate += text_tokens(content)
                continue
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
