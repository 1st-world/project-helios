"""Usage and cost calculations isolated from provider code."""

from dataclasses import dataclass


@dataclass
class UsageSummary:
    input_tokens: int = 0
    output_tokens: int = 0
    response_time_ms: int = 0
    estimated_cost: float | None = None
    is_long_context: bool = False

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "response_time_ms": self.response_time_ms,
            "estimated_cost": self.estimated_cost,
            "is_long_context": self.is_long_context,
        }


class UsageService:
    def summarize(
        self,
        usage: object | None,
        response_time_ms: int,
        input_price_per_million: float | None = None,
        output_price_per_million: float | None = None,
        long_context_threshold: int | None = None,
        long_input_price_per_million: float | None = None,
        long_output_price_per_million: float | None = None,
    ) -> UsageSummary:
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

        threshold = long_context_threshold if long_context_threshold is not None else 128_000
        has_long_rates = long_input_price_per_million is not None or long_output_price_per_million is not None
        is_long_tier = bool(has_long_rates and input_tokens >= threshold)

        if is_long_tier:
            in_price = long_input_price_per_million if long_input_price_per_million is not None else input_price_per_million
            out_price = long_output_price_per_million if long_output_price_per_million is not None else output_price_per_million
        else:
            in_price = input_price_per_million
            out_price = output_price_per_million

        if in_price is not None or out_price is not None:
            actual_in = in_price if in_price is not None else 0.0
            actual_out = out_price if out_price is not None else 0.0
            cost = (input_tokens / 1_000_000 * actual_in) + (output_tokens / 1_000_000 * actual_out)
            estimated_cost = round(cost, 8)
        else:
            estimated_cost = None

        return UsageSummary(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            response_time_ms=response_time_ms,
            estimated_cost=estimated_cost,
            is_long_context=is_long_tier,
        )
