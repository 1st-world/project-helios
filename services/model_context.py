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


@dataclass(frozen=True)
class MemoryPlan:
    """Pair a summary trigger with bounded retention and soft compaction targets."""

    trigger_tokens: int | None = None
    keep_recent_tokens: int | None = None
    summary_target_tokens: int | None = None
    compaction_target_tokens: int | None = None
    trigger_adjusted: bool = False

    @classmethod
    def resolve(
        cls, capacity: MemoryCapacity, trigger_tokens: int | None,
        keep_recent_tokens: int, summary_input_limit: int | None,
        *, output_limit: int | None = None, declared_max_output: int | None = None,
    ) -> "MemoryPlan":
        """Preserve preferences while fitting effective targets to declared capacity.

        A fifth of the trigger is shared by recent originals and summary text.
        Whole turns and essential memory may exceed these advisory targets.
        These are local planning heuristics, not model or Provider limits.
        """
        allowance = capacity.input_limit
        if allowance is None or summary_input_limit is None or summary_input_limit < 4 or trigger_tokens == 0:
            return cls()
        trigger = allowance * 9 // 10 if trigger_tokens is None else min(trigger_tokens, allowance)
        if trigger < 10:
            return cls()
        target = trigger // 5
        recent_target = min(keep_recent_tokens, target // 2)
        summary_target = min(
            value for value in (
                target // 2, summary_input_limit // 4,
                capacity.headroom, output_limit, declared_max_output,
            ) if value is not None
        )
        return cls(
            trigger_tokens=trigger,
            keep_recent_tokens=recent_target,
            summary_target_tokens=summary_target,
            compaction_target_tokens=target,
            trigger_adjusted=trigger_tokens is not None and trigger_tokens != trigger,
        )
