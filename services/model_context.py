"""Plan summary input capacity from explicit declarations without enabling memory."""

from dataclasses import dataclass

from models.profile import ConnectionProfile


@dataclass(frozen=True)
class MemoryCapacity:
    """Describe an advisory summary allowance without imposing an API output cap."""

    input_limit: int | None = None
    source: str = "unknown"
    headroom: int | None = None
    headroom_source: str = "unknown"
    reason: str = "missing_input_capacity"

    @classmethod
    def resolve(
        cls, profile: ConnectionProfile, output_limit: int | None,
    ) -> "MemoryCapacity":
        """Reserve planning headroom without inferring capacity from a deployment or response name."""
        window = profile.model_context_window
        maximum_input = profile.model_max_input_tokens
        headroom = profile.memory_output_headroom
        headroom_source = "planning" if headroom is not None else "unknown"
        if headroom is None and output_limit is not None:
            headroom, headroom_source = output_limit, "output_limit"
        if headroom is None and profile.declared_model_max_output_tokens is not None:
            headroom = profile.declared_model_max_output_tokens
            headroom_source = "declared_output"
        if window is not None and headroom is None:
            return cls(reason="missing_output_headroom")
        limits = []
        if maximum_input is not None:
            limits.append(maximum_input)
        if window is not None:
            limits.append(max(0, window - headroom))
        allowance = min(limits) if limits else None
        if allowance is not None and allowance <= 0:
            reason = "invalid_capacity"
        elif window is not None:
            reason = "ready"
        elif allowance is not None:
            reason = "input_only"
        else:
            reason = "missing_input_capacity"
        return cls(
            allowance if allowance is not None and allowance > 0 else None,
            "profile" if limits else "unknown",
            headroom, headroom_source,
            reason,
        )
