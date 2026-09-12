"""Conservative text budgeting, independent of model names and pricing tiers."""

from dataclasses import dataclass


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
    # These are local working limits, not advertised model context sizes.
    token_budget: int = 32_768
    output_reserve: int = 4_096
    max_summary_calls: int = 8

    def __post_init__(self) -> None:
        if self.output_reserve < 1 or self.token_budget - self.output_reserve < 1_024:
            raise ValueError("Context token budget must exceed the output reserve by at least 1024.")
        if self.max_summary_calls < 1:
            raise ValueError("At least one summary call must be allowed.")

    @property
    def input_limit(self) -> int:
        return self.token_budget - self.output_reserve

    @staticmethod
    def estimate(instructions: str, inputs: list[dict[str, str]]) -> int:
        """UTF-8 byte estimate plus framing slack; not model tokenization or billing."""
        return 256 + len(instructions.encode("utf-8")) + sum(
            32 + len(item["role"].encode("utf-8")) + len(item["content"].encode("utf-8")) for item in inputs)
