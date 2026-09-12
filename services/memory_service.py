"""Compacts old conversation history into durable AI-generated memory."""

import logging
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

from models.conversation import Conversation
from models.message import Message
from services.ai_service import AIService
from services.conversation_service import ConversationManager
from services.context_budget import (ContextBudget, ContextBudgetExceeded, ContextChangedError,
                                     ContextWindowExceeded, SummaryUnavailableError)
from services.prompt_builder import PromptBuilder


logger = logging.getLogger(__name__)


class ConversationMemoryService:
    def __init__(self, ai_service: AIService, conversation_manager: ConversationManager,
                 max_context_messages: int, keep_recent_messages: int, context_budget: ContextBudget | None = None) -> None:
        self.ai_service = ai_service
        self.conversation_manager = conversation_manager
        self.max_context_messages = max(2, max_context_messages)
        self.keep_recent_messages = max(2, min(keep_recent_messages, self.max_context_messages - 1))
        self._running: set[str] = set()
        self._pending: dict[str, tuple[Conversation, str | None]] = {}
        self.context_budget = context_budget or ContextBudget()
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def _exclusive(self, conversation_id: str):
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
            if (has_user and message.role == "assistant"
                    and (end == len(history) or history[end].role == "user")):
                boundaries.append(end)
                has_user = False
        return boundaries

    def _check_version(self, conversation: Conversation, version: int) -> None:
        if not self.conversation_manager.is_active(conversation) or conversation.version != version:
            raise ContextChangedError("Conversation changed while preparing context. Please retry.")

    def _summary_size(self, previous: str, messages: list[Message]) -> int:
        instructions, request = PromptBuilder.build_memory(previous, messages)
        return self.context_budget.estimate(instructions, [{"role": "user", "content": request}])

    def _summary_chunk(self, history: list[Message], start: int, end: int, offset: int,
                       previous: str, limit: int) -> tuple[list[Message], int, int]:
        chunk = []
        while start < end:
            message = history[start]
            prefix = "[continued message fragment]\n" if offset else ""
            remainder = replace(message, content=prefix + message.content[offset:])
            if self._summary_size(previous, [*chunk, remainder]) <= limit:
                chunk.append(remainder)
                start, offset = start + 1, 0
                continue
            if chunk:
                break
            # A single long message can exceed the summarizer's input allowance.
            # Split only the API input; the durable cutoff advances after every fragment up to a complete conversation turn has succeeded.
            low, high = 0, len(message.content) - offset - 1
            while low < high:
                middle = (low + high + 1) // 2
                fragment = replace(message, content=prefix + message.content[offset:offset + middle] + "\n[message continues]")
                if self._summary_size(previous, [fragment]) <= limit:
                    low = middle
                else:
                    high = middle - 1
            if low < 1:
                raise ContextBudgetExceeded("Existing memory cannot fit the summary input budget. Increase the context budget or shorten the conversation.")
            chunk.append(replace(message, content=prefix + message.content[offset:offset + low] + "\n[message continues]"))
            offset += low
            break
        return chunk, start, offset

    async def _summarize_prefix(self, conversation: Conversation, version: int, history: list[Message],
                                start: int, end: int, previous: str, profile_id: str | None,
                                work: list[int], limit: int) -> str:
        offset = 0
        while start < end:
            self._check_version(conversation, version)
            if work[0] >= self.context_budget.max_summary_calls:
                raise SummaryUnavailableError("Conversation summary work limit reached. Shorten the input or increase the summary call budget.")
            chunk, next_start, next_offset = self._summary_chunk(history, start, end, offset, previous, limit)
            work[0] += 1
            try:
                summary = await self.ai_service.summarize_memory(previous, chunk, profile_id=profile_id)
            except ContextWindowExceeded:
                limit //= 2
                continue
            self._check_version(conversation, version)
            if not summary or not summary.strip():
                raise SummaryUnavailableError("Conversation summary did not complete. Please retry.")
            previous = summary.strip()
            if self._summary_size(previous, []) > limit:
                raise ContextBudgetExceeded("The generated summary exceeds the context budget. Please retry with a larger budget.")
            start, offset = next_start, next_offset
        return previous

    async def prepare_context(self, conversation: Conversation, user_prompt: str, workspace_context: str,
                              profile_id: str, *, history_end: int | None = None,
                              persist_memory: bool = True, input_limit: int | None = None) -> tuple[str, list[dict[str, str]]]:
        """Fit a request before adding its user message or emitting any answer text."""
        builder = PromptBuilder()
        limit = min(self.context_budget.input_limit, input_limit if input_limit is not None else self.context_budget.input_limit)
        fixed = builder.build([], user_prompt, workspace_context)
        if self.context_budget.estimate(*fixed) > limit:
            raise ContextBudgetExceeded("The current question and workspace context exceed the input budget. Shorten the question or selected file.")
        original_messages = conversation.messages
        self._check_version(conversation, conversation.version)
        end = len(original_messages) if history_end is None else history_end
        if not 0 <= end <= len(original_messages):
            raise ContextChangedError("The requested conversation history is no longer available.")
        prepared = builder.build(original_messages[:end], user_prompt, workspace_context,
                                 conversation.memory_summary, conversation.summarized_message_count)
        # A fitting request needs no summary API call or lock. Let it proceed while optional background memory is still being generated.
        if self.context_budget.estimate(*prepared) <= limit:
            return prepared
        async with self._exclusive(conversation.id):
            if original_messages is not conversation.messages:
                raise ContextChangedError("Conversation changed while waiting for context preparation. Please retry.")
            version = conversation.version
            self._check_version(conversation, version)
            history = [replace(message) for message in original_messages[:end]]
            summary, start = conversation.memory_summary, conversation.summarized_message_count
            if not summary.strip() or not 0 < start <= end:
                summary, start = "", 0
            inputs = builder.build(history, user_prompt, workspace_context, summary, start)
            original_count = start
            boundaries = self.turn_boundaries(history)
            work = [0]
            while self.context_budget.estimate(*inputs) > limit:
                candidates = [boundary for boundary in boundaries if boundary > start]
                if not candidates:
                    raise ContextBudgetExceeded("The remaining context cannot fit without dropping an unfinished turn. Shorten the input or increase the context budget.")
                preferred = [boundary for boundary in candidates if boundary <= len(history) - self.keep_recent_messages]
                cutoff = preferred[-1] if preferred else candidates[0]
                try:
                    summary = await self._summarize_prefix(conversation, version, history, start, cutoff, summary,
                                                         profile_id, work, min(limit, self.context_budget.input_limit))
                except (ContextBudgetExceeded, ContextChangedError, SummaryUnavailableError):
                    raise
                except Exception as exc:
                    logger.exception("Pre-flight conversation summary failed")
                    raise SummaryUnavailableError("Could not summarize conversation history. Please retry.") from exc
                start = cutoff
                inputs = builder.build(history, user_prompt, workspace_context, summary, start)
            self._check_version(conversation, version)
            if persist_memory and start > original_count:
                if not self.conversation_manager.update_memory_if_version_matches(
                        conversation, summary, start, expected_version=version):
                    raise ContextChangedError("Conversation changed before memory could be saved. Please retry.")
            return inputs

    async def compact_if_needed(self, conversation: Conversation, profile_id: str | None = None) -> None:
        """Run one summary per conversation, coalescing overlapping requests.

        The first caller owns the work. Later callers only replace the pending
        request and return; no detached task or unbounded queue is created.
        """
        if not self.conversation_manager.is_active(conversation):
            return
        conversation_id = conversation.id
        self._pending[conversation_id] = (conversation, profile_id)
        if conversation_id in self._running:
            return
        self._running.add(conversation_id)
        last_attempt = None
        try:
            async with self._exclusive(conversation_id):
                while conversation_id in self._pending:
                    current, requested_profile_id = self._pending.pop(conversation_id)
                    attempt = (current.version, requested_profile_id)
                    if attempt == last_attempt:
                        continue
                    last_attempt = attempt
                    await self._compact_once(current, requested_profile_id)
        finally:
            # Cancellation must not leave a permanent busy marker. Unfinished memory remains eligible on the next successfully saved response.
            self._pending.pop(conversation_id, None)
            self._running.discard(conversation_id)

    async def _compact_once(self, conversation: Conversation, profile_id: str | None) -> None:
        if not self.conversation_manager.is_active(conversation):
            return
        start_version = conversation.version
        previous_summary = conversation.memory_summary
        unsummarized_count = len(conversation.messages) - conversation.summarized_message_count
        if unsummarized_count <= self.max_context_messages:
            return
        history = [replace(message) for message in conversation.messages]
        boundaries = [end for end in self.turn_boundaries(history)
                      if conversation.summarized_message_count < end <= len(history) - self.keep_recent_messages]
        if not boundaries:
            return
        cutoff = boundaries[-1]
        try:
            summary = await self._summarize_prefix(conversation, start_version, history,
                                                 conversation.summarized_message_count, cutoff, previous_summary,
                                                 profile_id, [0], self.context_budget.input_limit)
            if summary:
                applied = self.conversation_manager.update_memory_if_version_matches(
                    conversation, summary, cutoff, expected_version=start_version)
                if not applied:
                    logger.debug("Discarded stale conversation memory: %s", conversation.id)
        except ContextChangedError:
            logger.debug("Discarded stale conversation memory: %s", conversation.id)
        except (SummaryUnavailableError, ContextBudgetExceeded) as exc:
            logger.warning("Conversation memory deferred: %s", exc)
        except Exception:
            # Memory is an optimization; a failed summary must not interrupt the user response.
            logger.exception("Conversation memory compaction failed")
