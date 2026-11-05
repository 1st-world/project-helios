"""Apply selected memory thresholds separately from input admission and preserve completed-turn coverage."""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from copy import copy
from dataclasses import dataclass, field, replace

from models.app_settings import DEFAULT_APP_SETTINGS
from models.conversation import Conversation
from models.message import Message
from models.profile import ConnectionProfile
from services.ai_service import AIService
from services.context_budget import (
    ContextBudget,
    ContextBudgetExceeded,
    ContextChangedError,
    ContextWindowExceeded,
    SummaryUnavailableError,
)
from services.conversation_service import ConversationManager
from services.prompt_builder import PromptBuilder
from services.model_context import MemoryCapacity, MemoryPlan
from services.token_estimator import estimation_metadata

logger = logging.getLogger(__name__)


class MemoryPolicyChangedError(RuntimeError):
    """Stop unsubmitted summary work or adoption after permission or policy changes."""


@dataclass
class _MemoryPolicyRevision:
    """Share application and per-profile revocation across request settings snapshots."""

    value: int = 0
    generation: int = 0
    use_saved: int = 0
    profiles: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryPolicySnapshot:
    """Keep public planning details and private request authorization together."""

    policy: dict
    revision: int
    profile_revision: int
    settings: tuple
    profile: ConnectionProfile | None = field(repr=False)
    ai_service: AIService = field(repr=False)
    generation_revision: int = 0
    use_revision: int = 0
    use_settings: tuple = ()
    background: bool = False


class ConversationMemoryService:
    """Serialize summary work per conversation and preserve complete-turn coverage."""

    def __init__(
        self,
        ai_service: AIService,
        conversation_manager: ConversationManager,
        summary_trigger_tokens: int | None = DEFAULT_APP_SETTINGS.summary_trigger_tokens,
        keep_recent_tokens: int = DEFAULT_APP_SETTINGS.keep_recent_tokens,
        context_budget: ContextBudget | None = None,
        *, memory_generation_enabled: bool = True,
        memory_use_saved_summaries: bool | None = None,
        memory_background_enabled: bool = True,
    ) -> None:
        """Bind services, validate token policy, and initialize summary coordination."""
        self.ai_service = ai_service
        self.conversation_manager = conversation_manager
        if (
            (
                summary_trigger_tokens is not None
                and (type(summary_trigger_tokens) is not int or summary_trigger_tokens < 0)
            )
            or type(keep_recent_tokens) is not int or keep_recent_tokens < 1
            or (summary_trigger_tokens and keep_recent_tokens >= summary_trigger_tokens)
        ):
            raise ValueError("Summary token settings require a nonnegative trigger and a smaller positive retention target when enabled.")
        self.summary_trigger_tokens = summary_trigger_tokens
        self.keep_recent_tokens = keep_recent_tokens
        self.memory_generation_enabled = memory_generation_enabled
        self.memory_use_saved_summaries = (
            summary_trigger_tokens != 0 if memory_use_saved_summaries is None
            else memory_use_saved_summaries
        )
        self.memory_background_enabled = memory_background_enabled
        for name in ("memory_generation_enabled", "memory_use_saved_summaries", "memory_background_enabled"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean.")
        self._running: set[str] = set()
        self._pending: dict[
            str, tuple[
                Conversation, str | None, "ConversationMemoryService",
                MemoryPolicySnapshot,
            ]
        ] = {}
        self.context_budget = context_budget or ContextBudget()
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._revision = _MemoryPolicyRevision()
        self._settings_revision = 0
        self._generation_settings_revision = 0
        self._use_settings_revision = 0

    def invalidate_policy(
        self, profile_id: str | None = None, *,
        preserve_generation: bool = False, preserve_use: bool = False,
    ) -> None:
        """Publish revocation while preserving permissions unaffected by a settings change."""
        if profile_id is not None:
            self._revision.profiles[profile_id] = (
                self._revision.profiles.get(profile_id, 0) + 1
            )
        else:
            self._revision.value += 1
            self._settings_revision = self._revision.value
            if not preserve_generation:
                self._revision.generation += 1
            if not preserve_use:
                self._revision.use_saved += 1
            self._generation_settings_revision = self._revision.generation
            self._use_settings_revision = self._revision.use_saved

    def _settings_signature(self, *, use_only: bool = False) -> tuple:
        """Identify settings used for planning without including mutable runtime state."""
        return (
            self.context_budget, self.summary_trigger_tokens,
            self.keep_recent_tokens,
            self.memory_use_saved_summaries,
            *(() if use_only else (self.memory_generation_enabled,)),
        )

    def _profile(self, profile_id: str | None) -> ConnectionProfile | None:
        """Resolve explicit memory permission without guessing from an AI implementation."""
        profiles = getattr(self.ai_service, "profile_service", None)
        if profiles is None:
            return None
        return (
            profiles.get_profile(profile_id) if profile_id
            else profiles.get_active_profile()
        )

    def _profile_is_current(self, snapshot: MemoryPolicySnapshot) -> bool:
        """Check the pinned profile and revocations shared by use and generation."""
        profile = (
            self._profile(snapshot.profile.id)
            if snapshot.profile is not None else None
        )
        return (
            snapshot.profile is not None
            and profile is not None
            and snapshot.profile_revision == self._revision.profiles.get(
                profile.id, 0,
            )
            and snapshot.profile.request_settings() == profile.request_settings()
        )

    def policy_is_current(self, snapshot: MemoryPolicySnapshot) -> bool:
        """Check generation policy, including background revocation for background work."""
        return (
            snapshot.generation_revision
            == self._revision.generation == self._generation_settings_revision
            and snapshot.settings == self._settings_signature()
            and self._profile_is_current(snapshot)
            and (
                not (snapshot.background or snapshot.policy.get("one_shot"))
                or snapshot.revision == self._revision.value == self._settings_revision
            )
        )

    def memory_use_is_current(self, snapshot: MemoryPolicySnapshot) -> bool:
        """Allow already prepared memory independently of generation and background switches."""
        if snapshot.policy.get("one_shot"):
            return self.policy_is_current(snapshot)
        return (
            snapshot.use_revision
            == self._revision.use_saved == self._use_settings_revision
            and snapshot.use_settings == self._settings_signature(use_only=True)
            and self._profile_is_current(snapshot)
        )

    def _check_policy(self, snapshot: MemoryPolicySnapshot) -> None:
        """Stop a summary before submission or adoption when its permission is stale."""
        if not snapshot.policy["generation_enabled"] or not self.policy_is_current(snapshot):
            raise MemoryPolicyChangedError("Memory generation policy changed; no further summary work is allowed.")

    def with_settings(
        self, ai_service: AIService, context_budget: ContextBudget,
        summary_trigger_tokens: int | None, keep_recent_tokens: int,
        *, memory_generation_enabled: bool | None = None,
        memory_use_saved_summaries: bool | None = None,
        memory_background_enabled: bool | None = None,
    ) -> "ConversationMemoryService":
        """Snapshot work policy while sharing per-conversation summary coordination."""
        snapshot = copy(self)
        snapshot.ai_service = ai_service
        snapshot.context_budget = context_budget
        snapshot.summary_trigger_tokens = summary_trigger_tokens
        snapshot.keep_recent_tokens = keep_recent_tokens
        for name, value in (
            ("memory_generation_enabled", memory_generation_enabled),
            ("memory_use_saved_summaries", memory_use_saved_summaries),
            ("memory_background_enabled", memory_background_enabled),
        ):
            if value is not None:
                setattr(snapshot, name, value)
        return snapshot

    def capture_policy(
        self, profile_id: str | None = None, *, one_shot: bool = False,
    ) -> MemoryPolicySnapshot:
        """Capture automatic permission or a request-only grant with a feasible summary plan."""
        current_profile = self._profile(profile_id)
        profile = replace(current_profile) if current_profile is not None else None
        output_limit = (
            self.ai_service.output_limit(profile)
            if profile is not None and isinstance(self.ai_service, AIService)
            else None
        )
        capacity = (
            MemoryCapacity.resolve(profile, output_limit)
            if profile else MemoryCapacity()
        )
        allowance = capacity.input_limit
        summary_input_limit = (
            min(allowance, self.context_budget.input_limit)
            if allowance is not None else None
        )
        trigger = None if one_shot else self.summary_trigger_tokens
        plan = MemoryPlan.resolve(
            capacity, trigger, self.keep_recent_tokens, summary_input_limit,
            output_limit=output_limit,
            declared_max_output=profile.declared_model_max_output_tokens if profile else None,
        )
        enabled = profile is not None and (one_shot or profile.memory_enabled is True)
        state = "ready"
        reason = capacity.reason
        mode = "manual" if trigger is not None else "auto"
        if not enabled or trigger == 0:
            state, mode = "off", "disabled"
        elif self._generation_settings_revision != self._revision.generation:
            state, mode = "stopped", "disabled"
        elif allowance is None:
            state, mode = "needs_info", "unavailable"
        elif plan.trigger_tokens is None:
            state, mode = "needs_info", "unavailable"
            reason = "capacity_too_small"
        else:
            trigger = plan.trigger_tokens
        if state != "ready":
            trigger = 0 if self.summary_trigger_tokens == 0 else None
        if not enabled:
            reason = "disabled"
        elif self.summary_trigger_tokens == 0 and not one_shot:
            reason = "threshold_disabled"
        elif state == "stopped":
            reason = "policy_changed"
        use_saved = enabled and (one_shot or self.memory_use_saved_summaries)
        generate = state == "ready" and use_saved and (one_shot or self.memory_generation_enabled)
        policy = {
            **estimation_metadata(enabled=state == "ready"),
            "enabled": enabled, "state": state,
            "review_required": profile is not None and profile.memory_enabled is None,
            "use_saved_summaries": use_saved,
            "generation_enabled": generate,
            "background_enabled": generate and self.memory_background_enabled and not one_shot,
            "reason": reason,
            "mode": mode, "trigger_tokens": trigger,
            "keep_recent_tokens": self.keep_recent_tokens,
            "configured_trigger_tokens": None if one_shot else self.summary_trigger_tokens,
            "trigger_adjusted": plan.trigger_adjusted,
            "effective_keep_recent_tokens": plan.keep_recent_tokens,
            "summary_target_tokens": plan.summary_target_tokens,
            "compaction_target_tokens": plan.compaction_target_tokens,
            "summary_input_limit": summary_input_limit,
            "capacity_input_limit": allowance,
            "capacity_source": capacity.source,
            "output_headroom": capacity.headroom,
            "headroom_source": capacity.headroom_source,
            "input_only": capacity.reason == "input_only",
            "revision": self._revision.value, "one_shot": one_shot,
        }
        snapshot = MemoryPolicySnapshot(
            policy=policy, revision=self._revision.value,
            profile_revision=(
                self._revision.profiles.get(profile.id, 0)
                if profile is not None else 0
            ),
            settings=self._settings_signature(), profile=profile,
            ai_service=self.ai_service,
            generation_revision=self._revision.generation,
            use_revision=self._revision.use_saved,
            use_settings=self._settings_signature(use_only=True),
        )
        return self._pin_policy(snapshot)

    def _pin_policy(self, snapshot: MemoryPolicySnapshot) -> MemoryPolicySnapshot:
        """Bind summary authorization to the exact foreground or background snapshot."""
        if snapshot.profile is not None and isinstance(snapshot.ai_service, AIService):
            snapshot = replace(snapshot, ai_service=snapshot.ai_service.with_profile(
                snapshot.profile, before_summary=lambda: self._check_policy(snapshot),
                summary_target_tokens=snapshot.policy["summary_target_tokens"],
            ))
        return snapshot

    def summary_policy(self, profile_id: str | None = None) -> dict:
        """Expose activation and planning information without private connection data."""
        return self.capture_policy(profile_id).policy

    def _prompt_builder(self, conversation_id: str | None) -> PromptBuilder:
        """Resolve durable work context through the request's shared run store."""
        return PromptBuilder(
            getattr(self.ai_service, "work_run_store", None), conversation_id,
        )

    @staticmethod
    def _fragment_end(text: str, offset: int, stop: int) -> int:
        """Prefer paragraph and fenced-block boundaries, then lines, before a character fallback."""
        position = 0
        fence = None
        preferred, outside, lines = [], [], []
        for line in text.splitlines(keepends=True):
            begin = position
            position += len(line)
            marker = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
            if marker:
                run = marker.group(1)
                if fence is None:
                    if offset < begin <= stop:
                        preferred.append(begin)
                    fence = (run[0], len(run))
                elif (
                    run[0] == fence[0] and len(run) >= fence[1]
                    and not line[marker.end():].strip()
                ):
                    fence = None
                    if offset < position <= stop:
                        preferred.append(position)
            if offset < position <= stop:
                lines.append(position)
                if fence is None:
                    outside.append(position)
                    if not line.strip():
                        preferred.append(position)
            if position > stop:
                break
        return max(preferred or outside or lines, default=stop)

    def _recent_cutoff(
        self, history: list[Message], start: int,
        *, conversation_id: str | None = None,
        target_tokens: int | None = None,
    ) -> int:
        """Retain complete recent turns using their actual model replay representation."""
        builder = self._prompt_builder(conversation_id)
        target = self.keep_recent_tokens if target_tokens is None else target_tokens
        empty_size = None
        suffix = [0] * (len(history) + 1)
        for index in range(len(history) - 1, start - 1, -1):
            message = history[index]
            if message.run_id is None:
                size = self.context_budget.message_tokens(message.role, message.content)
            else:
                if empty_size is None:
                    empty_size = self.context_budget.estimate("", [])
                size = self.context_budget.estimate(
                    "", builder.history_items([message]),
                ) - empty_size
            suffix[index] = suffix[index + 1] + size
        if suffix[start] <= target:
            return start
        boundaries = self.turn_boundaries(history)
        # Keep the latest completed turn and any following unfinished messages intact.
        last_cutoff = boundaries[-2] if len(boundaries) >= 2 else start
        candidates = [boundary for boundary in boundaries if start < boundary <= last_cutoff]
        for boundary in candidates:
            if suffix[boundary] <= target:
                return boundary
        return candidates[-1] if candidates else start

    def _automatic_cutoff(
        self, history: list[Message], start: int, policy: dict,
        conversation_id: str, fixed_tokens: int, estimated: int,
        *, blocking_limit: int | None = None,
    ) -> int:
        """Plan one prefix and amortize saved-memory refreshes while input still fits."""
        retention = min(
            policy["effective_keep_recent_tokens"],
            max(
                0, policy["compaction_target_tokens"]
                - fixed_tokens - policy["summary_target_tokens"],
            ),
        )
        cutoff = self._recent_cutoff(
            history, start, conversation_id=conversation_id,
            target_tokens=retention,
        )
        if cutoff <= start or start == 0:
            return cutoff
        admission = policy["capacity_input_limit"]
        if blocking_limit is not None:
            admission = min(admission, blocking_limit)
        if estimated > admission:
            return cutoff
        builder = self._prompt_builder(conversation_id)
        new_tokens = self.context_budget.estimate(
            "", builder.history_items(history[start:cutoff]),
        ) - self.context_budget.estimate("", [])
        if new_tokens < policy["summary_target_tokens"]:
            return start
        return cutoff

    @asynccontextmanager
    async def _exclusive(self, conversation_id: str):
        """Hold a per-conversation lock and release its registry entry after the last user."""
        lock, users = self._locks.get(conversation_id, (asyncio.Lock(), 0))
        self._locks[conversation_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            remaining = self._locks[conversation_id][1] - 1
            if remaining:
                self._locks[conversation_id] = (lock, remaining)
            else:
                del self._locks[conversation_id]

    @staticmethod
    def turn_boundaries(history: list[Message]) -> list[int]:
        """End after an assistant group, keeping the next user turn intact."""
        has_user = False
        boundaries = []
        for end, message in enumerate(history, 1):
            if message.role == "user":
                has_user = True
            if (
                has_user
                and message.role == "assistant"
                and (end == len(history) or history[end].role == "user")
            ):
                boundaries.append(end)
                has_user = False
        return boundaries

    def _check_version(self, conversation: Conversation, version: int) -> None:
        """Reject context work when its conversation is inactive or has changed."""
        if (
            not self.conversation_manager.is_active(conversation)
            or conversation.version != version
        ):
            raise ContextChangedError(
                "Conversation changed while preparing context. Please retry."
            )

    def _summary_size(
        self, previous: str, messages: list[Message],
        *, target_tokens: int | None = None,
    ) -> int:
        """Estimate the input cost of a summary request including instructions and memory."""
        instructions, request = PromptBuilder.build_memory(previous, messages, target_tokens=target_tokens)
        return self.context_budget.estimate(
            instructions, [{"role": "user", "content": request}]
        )

    def _summary_chunk(
        self,
        history: list[Message],
        start: int,
        end: int,
        offset: int,
        previous: str,
        limit: int,
        *, target_tokens: int | None = None,
    ) -> tuple[list[Message], int, int]:
        """Fit messages or a partial message into the summary input allowance.

        Returns:
            The message chunk, next history index, and character offset within
            that next message; the offset is zero at a message boundary.
        """
        prefix = "[continued message fragment]\n" if offset else ""
        message = history[start]

        def candidate(stop: int) -> list[Message]:
            """Copy a candidate range while continuing only its first message."""
            return [
                replace(message, content=prefix + message.content[offset:]),
                *[replace(item) for item in history[start + 1 : stop]],
            ]

        # A token trigger can accumulate thousands of short messages; avoid recounting every growing prefix.
        low, high = start, end
        while low < high:
            middle = (low + high + 1) // 2
            if self._summary_size(previous, candidate(middle), target_tokens=target_tokens) <= limit:
                low = middle
            else:
                high = middle - 1
        if low > start:
            # Keep user/assistant groups together whenever a complete turn fits.
            boundaries = [
                boundary for boundary in self.turn_boundaries(history[:end])
                if start < boundary <= low
            ]
            if boundaries:
                low = boundaries[-1]
            return candidate(low), low, 0

        if message.run_id is not None:
            raise ContextBudgetExceeded(
                "A complete work context exceeds the summary input budget. Its call and result bundle was preserved. Increase the budget or retain the original context."
            )
        # Split oversized API input without advancing coverage until the complete turn is summarized.
        low, high = 0, len(message.content) - offset - 1
        while low < high:
            middle = (low + high + 1) // 2
            fragment = replace(
                message,
                content=prefix + message.content[offset : offset + middle] + "\n[message continues]",
            )
            if self._summary_size(previous, [fragment], target_tokens=target_tokens) <= limit:
                low = middle
            else:
                high = middle - 1
        if low < 1:
            raise ContextBudgetExceeded(
                "Existing memory cannot fit the summary input budget. Increase the context budget or shorten the conversation."
            )
        stop = self._fragment_end(message.content, offset, offset + low)
        # Token merges can make a shorter textual prefix cost slightly more tokens.
        semantic = replace(
            message,
            content=prefix + message.content[offset:stop] + "\n[message continues]",
        )
        if self._summary_size(previous, [semantic], target_tokens=target_tokens) <= limit:
            low = stop - offset
        fragment = replace(
            message, content=prefix + message.content[offset : offset + low] + "\n[message continues]",
        )
        return [fragment], start, offset + low

    async def _summarize_prefix(
        self,
        conversation: Conversation,
        version: int,
        history: list[Message],
        start: int,
        end: int,
        previous: str,
        profile_id: str | None,
        work: list[int],
        limit: int,
        snapshot: MemoryPolicySnapshot,
    ) -> str:
        """Summarize a history range within call limits, shrinking chunks on context rejection."""
        history = [
            *history[:start],
            *self._prompt_builder(conversation.id).summary_messages(history[start:end]),
            *history[end:],
        ]
        offset = 0
        target_tokens = snapshot.policy["summary_target_tokens"]
        while start < end:
            self._check_version(conversation, version)
            self._check_policy(snapshot)
            if work[0] >= self.context_budget.max_summary_calls:
                raise SummaryUnavailableError(
                    "Conversation summary work limit reached. Shorten the input or increase the summary call budget."
                )
            chunk, next_start, next_offset = self._summary_chunk(
                history, start, end, offset, previous, limit,
                target_tokens=target_tokens,
            )
            work[0] += 1
            try:
                summary = await snapshot.ai_service.summarize_memory(
                    previous,
                    chunk,
                    profile_id=profile_id,
                    conversation_id=conversation.id,
                )
            except ContextWindowExceeded:
                self._check_policy(snapshot)
                limit //= 2
                continue
            self._check_version(conversation, version)
            self._check_policy(snapshot)
            if not summary or not summary.strip():
                raise SummaryUnavailableError(
                    "Conversation summary did not complete. Please retry."
                )
            previous = summary.strip()
            if self._summary_size(previous, [], target_tokens=target_tokens) > limit:
                raise ContextBudgetExceeded(
                    "The generated summary exceeds the context budget. Please retry with a larger budget."
                )
            start, offset = next_start, next_offset
        return previous

    def recovery_plan(
        self, history: list[Message], user_prompt: str, workspace_context: str,
        snapshot: MemoryPolicySnapshot, *, attachment_content=None,
        visual_tokens: int = 0, input_limit: int | None = None,
        conversation_id: str | None = None,
    ) -> tuple[int, int]:
        """Reject irreducible fixed input before paying to summarize older complete turns."""
        if not snapshot.policy.get("one_shot") or snapshot.policy["state"] != "ready":
            raise SummaryUnavailableError("Deployment information is required before a one-time summary can be planned.")
        boundaries = self.turn_boundaries(history)
        if len(boundaries) < 2:
            raise SummaryUnavailableError("There are no older complete turns to summarize while preserving the latest complete turn. Edit the input or choose another deployment.")
        cutoff = boundaries[-2]
        target = snapshot.policy["capacity_input_limit"]
        if input_limit is not None:
            target = min(target, input_limit)
        retained = self._prompt_builder(conversation_id).build(
            history[cutoff:], user_prompt, workspace_context,
            attachment_content=attachment_content,
        )
        if self.context_budget.estimate(
            *retained, visual_tokens=visual_tokens,
        ) >= target:
            raise ContextBudgetExceeded("The question, workspace, attachments, and latest complete turn already reach the recovery input allowance. Reduce these inputs or choose another deployment; no summary call was started.")
        return cutoff, target

    async def prepare_recovery(
        self, conversation: Conversation, user_prompt: str, workspace_context: str,
        profile_id: str, *, history_end: int, summary_policy: MemoryPolicySnapshot,
        attachment_content=None, visual_tokens: int = 0,
        input_limit: int | None = None,
    ) -> tuple[str, list[dict]]:
        """Summarize original older turns once for this reply without adopting stored memory."""
        version = conversation.version
        history = [replace(message) for message in conversation.messages[:history_end]]
        cutoff, target = self.recovery_plan(
            history, user_prompt, workspace_context, summary_policy,
            attachment_content=attachment_content, visual_tokens=visual_tokens,
            input_limit=input_limit,
            conversation_id=conversation.id,
        )
        async with self._exclusive(conversation.id):
            self._check_version(conversation, version)
            self._check_policy(summary_policy)
            summary = await self._summarize_prefix(
                conversation, version, history, 0, cutoff, "", profile_id, [0],
                summary_policy.policy["summary_input_limit"], summary_policy,
            )
            self._check_version(conversation, version)
            self._check_policy(summary_policy)
            builder = self._prompt_builder(conversation.id)
            inputs = builder.build(
                history, user_prompt, workspace_context, summary, cutoff,
                attachment_content=attachment_content,
            )
            original = builder.build(
                history, user_prompt, workspace_context,
                attachment_content=attachment_content,
            )
            estimated = self.context_budget.estimate(
                *inputs, visual_tokens=visual_tokens,
            )
            if estimated > target or estimated >= self.context_budget.estimate(
                *original, visual_tokens=visual_tokens,
            ):
                raise SummaryUnavailableError("The one-time summary did not produce a smaller input within the recovery allowance. No reply call was started. Edit the input or choose another deployment.")
            return inputs

    async def prepare_context(
        self,
        conversation: Conversation,
        user_prompt: str,
        workspace_context: str,
        profile_id: str,
        *,
        history_end: int | None = None,
        persist_memory: bool = True,
        input_limit: int | None = None,
        attachment_content: list[dict] | None = None,
        visual_tokens: int = 0,
        preflight_mode: str = "block",
        summary_policy: MemoryPolicySnapshot | None = None,
        reply_tools: list[dict] | None = None,
        reply_instructions_suffix: str = "",
    ) -> tuple[str, list[dict]]:
        """Apply memory thresholds and enforce blocking without forcing history summaries."""
        builder = self._prompt_builder(conversation.id)
        limit = (
            input_limit
            if input_limit is not None else self.context_budget.input_limit
        )
        fixed = builder.build(
            [], user_prompt, workspace_context, attachment_content=attachment_content,
        )
        if preflight_mode not in {"warn", "block", "off"}:
            raise ValueError("Context preflight mode must be warn, block, or off.")

        def reply_estimate(inputs: tuple[str, list[dict]]) -> int:
            """Include enabled function definitions in reply planning, not summary prose."""
            return self.context_budget.estimate(
                inputs[0] + reply_instructions_suffix, inputs[1],
                visual_tokens=visual_tokens, tools=reply_tools,
            )

        def admit(inputs: tuple[str, list[dict]]) -> tuple[str, list[dict]]:
            """Reject estimated input only when the selected policy explicitly blocks it."""
            self._check_version(conversation, version)
            if not self.memory_use_is_current(snapshot):
                inputs = builder.build(
                    original_messages[:end], user_prompt, workspace_context,
                    attachment_content=attachment_content,
                )
            if (
                preflight_mode == "block"
                and reply_estimate(inputs) > limit
            ):
                raise ContextBudgetExceeded(
                    "The estimated input exceeds the selected preflight limit. "
                    "Increase the limit, reduce the input, or change the preflight mode."
                )
            return inputs

        if (
            preflight_mode == "block"
            and reply_estimate(fixed) > limit
        ):
            raise ContextBudgetExceeded(
                "The current question and workspace context exceed the input "
                "budget. Shorten the question or reduce selected attachments."
            )
        original_messages = conversation.messages
        version = conversation.version
        self._check_version(conversation, version)
        end = len(original_messages) if history_end is None else history_end
        if not 0 <= end <= len(original_messages):
            raise ContextChangedError(
                "The requested conversation history is no longer available."
            )
        snapshot = summary_policy or self.capture_policy(profile_id)
        memory_policy = snapshot.policy
        memory_disabled = (
            not memory_policy["use_saved_summaries"]
            or not self.memory_use_is_current(snapshot)
        )
        prepared = builder.build(
            original_messages[:end],
            user_prompt,
            workspace_context,
            "" if memory_disabled else conversation.memory_summary,
            0 if memory_disabled else conversation.summarized_message_count,
            attachment_content=attachment_content,
        )
        trigger = memory_policy["trigger_tokens"]
        automatic = (
            not memory_disabled
            and memory_policy["generation_enabled"]
            and self.policy_is_current(snapshot)
            and bool(trigger)
            and len(self.turn_boundaries(original_messages[:end])) >= 2
        )
        if not automatic:
            return admit(prepared)
        estimated = reply_estimate(prepared)
        auto_needed = automatic and estimated > trigger
        # Requests that already fit can proceed while background memory is being generated.
        if not auto_needed:
            return admit(prepared)
        if (
            conversation.id in self._running
            and estimated <= memory_policy["summary_input_limit"]
            and (preflight_mode != "block" or estimated <= limit)
        ):
            return admit(prepared)
        async with self._exclusive(conversation.id):
            if original_messages is not conversation.messages:
                raise ContextChangedError(
                    "Conversation changed while waiting for context preparation. Please retry."
                )
            version = conversation.version
            self._check_version(conversation, version)
            if not self.policy_is_current(snapshot):
                return admit(prepared)
            history = [replace(message) for message in original_messages[:end]]
            summary, start = (
                conversation.memory_summary,
                conversation.summarized_message_count,
            )
            if not summary.strip() or not 0 < start <= end:
                summary, start = "", 0
            inputs = builder.build(
                history, user_prompt, workspace_context, summary, start, attachment_content=attachment_content,
            )
            if reply_estimate(inputs) <= trigger:
                return admit(inputs)
            original_count = start
            estimate = reply_estimate(inputs)
            cutoff = self._automatic_cutoff(
                history, start, memory_policy, conversation.id,
                reply_estimate(fixed), estimate,
                blocking_limit=limit if preflight_mode == "block" else None,
            )
            if cutoff <= start:
                return admit(inputs)
            try:
                candidate_summary = await self._summarize_prefix(
                    conversation, version, history, start, cutoff, summary,
                    profile_id, [0], memory_policy["summary_input_limit"], snapshot,
                )
            except MemoryPolicyChangedError:
                return admit(inputs)
            except ContextChangedError:
                raise
            except (ContextBudgetExceeded, SummaryUnavailableError):
                logger.warning("Automatic conversation summary deferred; retaining original request.")
                self._check_version(conversation, version)
                return admit(inputs)
            except Exception as exc:
                logger.error("Pre-flight conversation summary failed (error_type=%s)", type(exc).__name__)
                self._check_version(conversation, version)
                return admit(inputs)
            candidate_inputs = builder.build(
                history, user_prompt, workspace_context, candidate_summary, cutoff,
                attachment_content=attachment_content,
            )
            if reply_estimate(candidate_inputs) < estimate:
                summary, start, inputs = candidate_summary, cutoff, candidate_inputs
            else:
                logger.warning("Automatic conversation summary did not reduce input; retaining existing context.")
            self._check_version(conversation, version)
            if not self.policy_is_current(snapshot):
                return admit(prepared)
            admit(inputs)
            if persist_memory and start > original_count:
                if not self.conversation_manager.update_memory_if_version_matches(
                    conversation, summary, start, expected_version=version
                ):
                    raise ContextChangedError(
                        "Conversation changed before memory could be saved. Please retry."
                    )
            return inputs

    async def compact_if_needed(
        self, conversation: Conversation, profile_id: str | None = None,
        *, summary_policy: MemoryPolicySnapshot | None = None,
    ) -> None:
        """Run one summary per conversation, coalescing overlapping requests.

        The first caller owns the work. Later callers only replace the pending
        request and return; no detached task or unbounded queue is created.
        """
        if not self.conversation_manager.is_active(conversation):
            return
        snapshot = summary_policy or self.capture_policy(profile_id)
        if not snapshot.policy["background_enabled"]:
            return
        snapshot = self._pin_policy(replace(snapshot, background=True))
        if not self.policy_is_current(snapshot):
            return
        conversation_id = conversation.id
        self._pending[conversation_id] = (conversation, profile_id, self, snapshot)
        if conversation_id in self._running:
            return
        self._running.add(conversation_id)
        last_attempt = None
        try:
            async with self._exclusive(conversation_id):
                while conversation_id in self._pending:
                    pending = self._pending.pop(conversation_id)
                    (
                        current, requested_profile_id, requested_memory,
                        requested_policy,
                    ) = pending
                    attempt = (
                        current.version, requested_profile_id,
                        requested_memory.context_budget,
                        requested_memory.summary_trigger_tokens,
                        requested_memory.keep_recent_tokens,
                        requested_policy.revision,
                    )
                    if attempt == last_attempt:
                        continue
                    last_attempt = attempt
                    await requested_memory._compact_once(
                        current, requested_profile_id, requested_policy,
                    )
        finally:
            # Clear busy state on cancellation so later replies can retry unfinished memory.
            self._pending.pop(conversation_id, None)
            self._running.discard(conversation_id)

    async def _compact_once(
        self, conversation: Conversation, profile_id: str | None,
        snapshot: MemoryPolicySnapshot,
    ) -> None:
        """Summarize eligible completed turns and save memory only if its version still matches."""
        if (
            not self.conversation_manager.is_active(conversation)
            or not self.policy_is_current(snapshot)
        ):
            return
        start_version = conversation.version
        previous_summary = conversation.memory_summary
        policy = snapshot.policy
        trigger = policy["trigger_tokens"]
        if not trigger:
            return
        history = [replace(message) for message in conversation.messages]
        start = conversation.summarized_message_count
        if not previous_summary.strip() or not 0 < start <= len(history):
            previous_summary, start = "", 0
        builder = self._prompt_builder(conversation.id)
        instructions, inputs = builder.build(
            history, "", memory_summary=previous_summary,
            summarized_message_count=start,
        )
        original_estimate = self.context_budget.estimate(instructions, inputs[:-1])
        if original_estimate <= trigger:
            return
        fixed_instructions, fixed_inputs = builder.build([], "")
        cutoff = self._automatic_cutoff(
            history, start, policy, conversation.id,
            self.context_budget.estimate(fixed_instructions, fixed_inputs[:-1]),
            original_estimate,
        )
        if cutoff <= start:
            return
        try:
            summary = await self._summarize_prefix(
                conversation,
                start_version,
                history,
                start,
                cutoff,
                previous_summary,
                profile_id,
                [0],
                policy["summary_input_limit"],
                snapshot,
            )
            if summary:
                candidate_instructions, candidate_inputs = builder.build(
                    history, "", memory_summary=summary,
                    summarized_message_count=cutoff,
                )
                if self.context_budget.estimate(
                    candidate_instructions, candidate_inputs[:-1],
                ) >= original_estimate:
                    logger.warning("Background conversation summary did not reduce input; retaining existing memory.")
                    return
                self._check_policy(snapshot)
                applied = (
                    self.conversation_manager.update_memory_if_version_matches(
                        conversation,
                        summary,
                        cutoff,
                        expected_version=start_version,
                    )
                )
                if not applied:
                    logger.debug(
                        "Discarded stale conversation memory: %s",
                        conversation.id,
                    )
        except (ContextChangedError, MemoryPolicyChangedError):
            logger.debug(
                "Discarded stale conversation memory: %s", conversation.id
            )
        except (SummaryUnavailableError, ContextBudgetExceeded) as exc:
            logger.warning("Conversation memory deferred: %s", exc)
        except Exception as exc:
            # Memory is an optimization; a failed summary must not interrupt the user response.
            logger.error("Conversation memory compaction failed (error_type=%s)", type(exc).__name__)
