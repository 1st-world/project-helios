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
from services.model_context import MemoryCapacity
from services.token_estimator import estimation_metadata

logger = logging.getLogger(__name__)


class MemoryPolicyChangedError(RuntimeError):
    """Stop unsubmitted summary work or adoption after permission or policy changes."""


@dataclass
class _MemoryPolicyRevision:
    """Share revocation state across immutable application settings snapshots."""

    value: int = 0


@dataclass(frozen=True)
class MemoryPolicySnapshot:
    """Keep public planning details and private request authorization together."""

    policy: dict
    revision: int
    settings: tuple
    profile: ConnectionProfile | None = field(repr=False)
    ai_service: AIService = field(repr=False)


class ConversationMemoryService:
    """Serialize summary work per conversation and preserve complete-turn coverage."""

    def __init__(
        self,
        ai_service: AIService,
        conversation_manager: ConversationManager,
        summary_trigger_tokens: int | None = DEFAULT_APP_SETTINGS.summary_trigger_tokens,
        keep_recent_tokens: int = DEFAULT_APP_SETTINGS.keep_recent_tokens,
        context_budget: ContextBudget | None = None,
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

    def invalidate_policy(self) -> None:
        """Revoke outstanding work after successfully publishing a policy change."""
        self._revision.value += 1
        self._settings_revision = self._revision.value

    def _settings_signature(self) -> tuple:
        """Identify settings used for planning without including mutable runtime state."""
        return (
            self.context_budget, self.summary_trigger_tokens,
            self.keep_recent_tokens,
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

    def policy_is_current(self, snapshot: MemoryPolicySnapshot) -> bool:
        """Check revocation and the captured connection before using summarized input."""
        return (
            snapshot.revision == self._revision.value == self._settings_revision
            and snapshot.settings == self._settings_signature()
            and snapshot.profile is not None
            and snapshot.profile == self._profile(snapshot.profile.id)
        )

    def _check_policy(self, snapshot: MemoryPolicySnapshot) -> None:
        """Stop a summary before submission or adoption when its permission is stale."""
        if not self.policy_is_current(snapshot):
            raise MemoryPolicyChangedError("Memory policy changed; original history will be used.")

    def with_settings(
        self, ai_service: AIService, context_budget: ContextBudget,
        summary_trigger_tokens: int | None, keep_recent_tokens: int,
    ) -> "ConversationMemoryService":
        """Snapshot work policy while sharing per-conversation summary coordination."""
        snapshot = copy(self)
        snapshot.ai_service = ai_service
        snapshot.context_budget = context_budget
        snapshot.summary_trigger_tokens = summary_trigger_tokens
        snapshot.keep_recent_tokens = keep_recent_tokens
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
        trigger = None if one_shot else self.summary_trigger_tokens
        enabled = profile is not None and (one_shot or profile.memory_enabled is True)
        state = "ready"
        reason = capacity.reason
        mode = "manual" if trigger is not None else "auto"
        if not enabled or trigger == 0:
            state, mode = "off", "disabled"
        elif self._settings_revision != self._revision.value:
            state, mode = "stopped", "disabled"
        elif allowance is None:
            state, mode = "needs_info", "unavailable"
        elif mode == "auto":
            trigger = allowance * 9 // 10 if allowance is not None else None
            if trigger is None or trigger < 1:
                state, mode = "needs_info", "unavailable"
                reason = "capacity_too_small"
        if state != "ready":
            trigger = 0 if self.summary_trigger_tokens == 0 else None
        if not enabled:
            reason = "disabled"
        elif self.summary_trigger_tokens == 0 and not one_shot:
            reason = "threshold_disabled"
        elif state == "stopped":
            reason = "policy_changed"
        policy = {
            **estimation_metadata(enabled=state == "ready"),
            "enabled": enabled, "state": state,
            "review_required": profile is not None and profile.memory_enabled is None,
            "reason": reason,
            "mode": mode, "trigger_tokens": trigger,
            "keep_recent_tokens": self.keep_recent_tokens,
            "summary_input_limit": (
                min(allowance, self.context_budget.input_limit)
                if allowance is not None else None
            ),
            "capacity_input_limit": allowance,
            "capacity_source": capacity.source,
            "output_headroom": capacity.headroom,
            "headroom_source": capacity.headroom_source,
            "input_only": capacity.reason == "input_only",
            "revision": self._revision.value, "one_shot": one_shot,
        }
        snapshot = MemoryPolicySnapshot(
            policy, self._revision.value, self._settings_signature(), profile,
            self.ai_service,
        )
        if profile is not None and isinstance(self.ai_service, AIService):
            snapshot = replace(snapshot, ai_service=self.ai_service.with_profile(
                profile, before_summary=lambda: self._check_policy(snapshot),
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

    def _recent_cutoff(self, history: list[Message], start: int) -> int:
        """Choose a complete-turn cutoff while retaining the newest turn and a token-target suffix."""
        suffix = [0] * (len(history) + 1)
        for index in range(len(history) - 1, start - 1, -1):
            suffix[index] = suffix[index + 1] + self.context_budget.message_tokens(
                history[index].role, history[index].content,
            )
        if suffix[start] <= self.keep_recent_tokens:
            return start
        boundaries = self.turn_boundaries(history)
        # Keep the latest completed turn and any following unfinished messages intact.
        last_cutoff = boundaries[-2] if len(boundaries) >= 2 else start
        candidates = [boundary for boundary in boundaries if start < boundary <= last_cutoff]
        for boundary in candidates:
            if suffix[boundary] <= self.keep_recent_tokens:
                return boundary
        return candidates[-1] if candidates else start

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

    def _summary_size(self, previous: str, messages: list[Message]) -> int:
        """Estimate the input cost of a summary request including instructions and memory."""
        instructions, request = PromptBuilder.build_memory(previous, messages)
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
            if self._summary_size(previous, candidate(middle)) <= limit:
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
            if self._summary_size(previous, [fragment]) <= limit:
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
        if self._summary_size(previous, [semantic]) <= limit:
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
        while start < end:
            self._check_version(conversation, version)
            self._check_policy(snapshot)
            if work[0] >= self.context_budget.max_summary_calls:
                raise SummaryUnavailableError(
                    "Conversation summary work limit reached. Shorten the input or increase the summary call budget."
                )
            chunk, next_start, next_offset = self._summary_chunk(
                history, start, end, offset, previous, limit
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
            if self._summary_size(previous, []) > limit:
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
            if not self.policy_is_current(snapshot):
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
            memory_policy["state"] != "ready"
            or not self.policy_is_current(snapshot)
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
            original_count = start
            boundaries = self.turn_boundaries(history)
            work = [0]
            while True:
                estimate = reply_estimate(inputs)
                auto_needed = automatic and estimate > trigger
                if not auto_needed:
                    break
                candidates = [
                    boundary for boundary in boundaries if boundary > start
                    and boundary < boundaries[-1]
                ]
                if not candidates:
                    break
                preferred = self._recent_cutoff(history, start)
                cutoff = preferred if preferred in candidates else candidates[0]
                summary_limit = memory_policy["summary_input_limit"]
                try:
                    summary = await self._summarize_prefix(
                        conversation,
                        version,
                        history,
                        start,
                        cutoff,
                        summary,
                        profile_id,
                        work,
                        summary_limit,
                        snapshot,
                    )
                except MemoryPolicyChangedError:
                    return admit(prepared)
                except ContextChangedError:
                    raise
                except (ContextBudgetExceeded, SummaryUnavailableError):
                    logger.warning("Automatic conversation summary deferred; retaining original request.")
                    self._check_version(conversation, version)
                    return admit(prepared)
                except Exception as exc:
                    logger.error("Pre-flight conversation summary failed (error_type=%s)", type(exc).__name__)
                    self._check_version(conversation, version)
                    return admit(prepared)
                start = cutoff
                inputs = builder.build(
                    history, user_prompt, workspace_context, summary, start, attachment_content=attachment_content,
                )
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
        if snapshot.policy["state"] != "ready" or not self.policy_is_current(snapshot):
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
        instructions, inputs = self._prompt_builder(conversation.id).build(
            history, "", memory_summary=previous_summary,
            summarized_message_count=conversation.summarized_message_count,
        )
        if self.context_budget.estimate(instructions, inputs[:-1]) <= trigger:
            return
        cutoff = self._recent_cutoff(history, conversation.summarized_message_count)
        if cutoff <= conversation.summarized_message_count:
            return
        try:
            summary = await self._summarize_prefix(
                conversation,
                start_version,
                history,
                conversation.summarized_message_count,
                cutoff,
                previous_summary,
                profile_id,
                [0],
                policy["summary_input_limit"],
                snapshot,
            )
            if summary:
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
