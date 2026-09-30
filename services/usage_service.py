"""Normalize provider token usage and estimate costs from profile-specific pricing."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass


def _value(source: object | None, name: str) -> object | None:
    """Read a usage field from a provider object or a replayed JSON mapping."""
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _tokens(source: object | None, name: str) -> int | None:
    """Preserve missing or invalid counts instead of converting them to zero."""
    value = _value(source, name)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _provider_usage(usage: object | None) -> dict | None:
    """Snapshot provider usage, including fields the local schema does not yet expose."""
    if usage is None:
        return None
    if isinstance(usage, Mapping):
        return deepcopy(dict(usage))
    if callable(getattr(usage, "model_dump", None)):
        return usage.model_dump(mode="json")
    data = deepcopy(vars(usage))
    for key in ("input_tokens_details", "output_tokens_details"):
        details = data.get(key)
        if details is not None and not isinstance(details, Mapping):
            data[key] = vars(details).copy()
    return data


@dataclass
class UsageSummary:
    """Hold normalized token counts, response duration, and optional estimated cost."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    response_time_ms: int = 0
    estimated_cost: float | None = None
    is_long_context: bool = False
    total_tokens: int | None = None
    uncached_input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    usage_status: str = "unavailable"
    cost_status: str = "unavailable"
    provider_usage: dict | None = None

    def to_dict(self) -> dict:
        """Serialize reported counts while keeping unavailable usage explicit."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "response_time_ms": self.response_time_ms,
            "estimated_cost": self.estimated_cost,
            "is_long_context": self.is_long_context,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "usage_status": self.usage_status,
            "cost_status": self.cost_status,
            "provider_usage": deepcopy(self.provider_usage),
        }


class UsageService:
    """Calculate response usage and cost using configured standard or long-context rates."""

    def summarize(
        self,
        usage: object | None,
        response_time_ms: int,
        input_price_per_million: float | None = None,
        output_price_per_million: float | None = None,
        long_context_threshold: int | None = None,
        long_input_price_per_million: float | None = None,
        long_output_price_per_million: float | None = None,
        cache_read_price_per_million: float | None = None,
        cache_write_price_per_million: float | None = None,
        long_cache_read_price_per_million: float | None = None,
        long_cache_write_price_per_million: float | None = None,
    ) -> UsageSummary:
        """Price reported token categories while distinguishing partial and unavailable estimates."""
        input_tokens = _tokens(usage, "input_tokens")
        output_tokens = _tokens(usage, "output_tokens")
        total_tokens = _tokens(usage, "total_tokens")
        if (
            _value(usage, "total_tokens") is None
            and input_tokens is not None
            and output_tokens is not None
        ):
            total_tokens = input_tokens + output_tokens
        input_details = _value(usage, "input_tokens_details")
        output_details = _value(usage, "output_tokens_details")
        cache_read_tokens = _tokens(input_details, "cached_tokens")
        cache_write_tokens = _tokens(input_details, "cache_write_tokens")
        reasoning_tokens = _tokens(output_details, "reasoning_tokens")
        uncached_input_tokens = None
        if (
            input_tokens is not None
            and cache_read_tokens is not None
            and cache_write_tokens is not None
        ):
            ordinary = input_tokens - cache_read_tokens - cache_write_tokens
            if ordinary >= 0:
                uncached_input_tokens = ordinary

        counts = (input_tokens, output_tokens, total_tokens)
        if all(count is None for count in counts):
            usage_status = "unavailable"
        elif (
            all(count is not None for count in counts)
            and total_tokens == input_tokens + output_tokens
        ):
            usage_status = "available"
        else:
            usage_status = "partial"
        invalid_cache = any(
            _value(input_details, name) is not None and count is None
            for name, count in (
                ("cached_tokens", cache_read_tokens),
                ("cache_write_tokens", cache_write_tokens),
            )
        ) or input_tokens is not None and (
            (cache_read_tokens is not None and cache_read_tokens > input_tokens)
            or (
                cache_write_tokens is not None
                and cache_write_tokens > input_tokens
            )
            or (
                cache_read_tokens is not None
                and cache_write_tokens is not None
                and cache_read_tokens + cache_write_tokens > input_tokens
            )
        )
        invalid_reasoning = (
            _value(output_details, "reasoning_tokens") is not None
            and reasoning_tokens is None
        ) or (
            output_tokens is not None
            and reasoning_tokens is not None
            and reasoning_tokens > output_tokens
        )
        if usage_status == "available" and (invalid_cache or invalid_reasoning):
            usage_status = "partial"

        threshold = (
            long_context_threshold
            if long_context_threshold is not None
            else 128_000
        )
        has_long_rates = (
            long_input_price_per_million is not None
            or long_output_price_per_million is not None
            or long_cache_read_price_per_million is not None
            or long_cache_write_price_per_million is not None
        )
        is_long_tier = bool(
            has_long_rates
            and input_tokens is not None
            and input_tokens >= threshold
        )

        if is_long_tier:
            in_price = (
                long_input_price_per_million
                if long_input_price_per_million is not None
                else input_price_per_million
            )
            out_price = (
                long_output_price_per_million
                if long_output_price_per_million is not None
                else output_price_per_million
            )
            read_price = (
                long_cache_read_price_per_million
                if long_cache_read_price_per_million is not None
                else cache_read_price_per_million
            )
            write_price = (
                long_cache_write_price_per_million
                if long_cache_write_price_per_million is not None
                else cache_write_price_per_million
            )
        else:
            in_price = input_price_per_million
            out_price = output_price_per_million
            read_price = cache_read_price_per_million
            write_price = cache_write_price_per_million

        estimated_cost = None
        cost_status = "unavailable"
        priced_input = uncached_input_tokens
        if priced_input is None and not (
            cache_read_tokens or cache_write_tokens
        ):
            # Keep legacy input estimates partial when cache details are missing.
            priced_input = input_tokens
        categories = (
            (priced_input, in_price),
            (cache_read_tokens, read_price),
            (cache_write_tokens, write_price),
            (output_tokens, out_price),
        )
        priced_categories = [
            (count, price) for count, price in categories
            if count is not None and price is not None
        ]
        if usage_status == "available" and priced_categories:
            cost = sum(
                count / 1_000_000 * price
                for count, price in priced_categories
            )
            estimated_cost = round(cost, 8)
            missing_rate = any(
                count is not None and count > 0 and price is None
                for count, price in categories
            )
            missing_details = input_tokens > 0 and (
                cache_read_tokens is None or cache_write_tokens is None
            )
            cost_status = (
                "partial" if missing_rate or missing_details else "complete"
            )

        return UsageSummary(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            response_time_ms=response_time_ms,
            estimated_cost=estimated_cost,
            is_long_context=is_long_tier,
            total_tokens=total_tokens,
            uncached_input_tokens=uncached_input_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
            usage_status=usage_status,
            cost_status=cost_status,
            provider_usage=_provider_usage(usage),
        )
