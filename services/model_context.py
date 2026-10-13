"""Resolve advisory summary capacity from explicitly declared model limits."""

from dataclasses import dataclass

from models.profile import ConnectionProfile


@dataclass(frozen=True)
class MemoryCapacity:
    """Describe an advisory summary allowance without imposing an API output cap."""

    input_limit: int | None = None
    source: str = "unknown"

    @classmethod
    def resolve(
        cls, profile: ConnectionProfile, output_limit: int | None,
    ) -> "MemoryCapacity":
        """Reserve planning headroom without inferring capacity from a deployment or response name."""
        window = profile.model_context_window
        maximum_input = profile.model_max_input_tokens
        # This is planning headroom, independent of the SDK's optional output cap.
        headroom = (
            output_limit if output_limit is not None
            else 25_000
        )
        limits = []
        if maximum_input is not None:
            limits.append(maximum_input)
        if window is not None:
            limits.append(max(0, window - headroom))
        declared = (
            profile.model_context_window is not None
            or profile.model_max_input_tokens is not None
        )
        return cls(
            min(limits) if limits else None,
            "profile" if declared else "unknown",
        )
