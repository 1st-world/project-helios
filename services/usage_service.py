"""Usage and cost calculations isolated from provider code."""

from dataclasses import dataclass


@dataclass
class UsageSummary:
    input_tokens: int = 0
    output_tokens: int = 0
    response_time_ms: int = 0
    estimated_cost: float = 0.0

    def to_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
                "response_time_ms": self.response_time_ms, "estimated_cost": self.estimated_cost}


class UsageService:
    def __init__(self, input_price_per_million: float, output_price_per_million: float) -> None:
        self.input_price_per_million = input_price_per_million
        self.output_price_per_million = output_price_per_million

    def summarize(self, usage: object | None, response_time_ms: int) -> UsageSummary:
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cost = (input_tokens / 1_000_000 * self.input_price_per_million + output_tokens / 1_000_000 * self.output_price_per_million)
        return UsageSummary(input_tokens, output_tokens, response_time_ms, round(cost, 8))
