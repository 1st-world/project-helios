"""Compacts old conversation history into durable AI-generated memory."""

import logging
from dataclasses import replace

from models.conversation import Conversation
from services.ai_service import AIService
from services.conversation_service import ConversationManager


logger = logging.getLogger(__name__)


class ConversationMemoryService:
    def __init__(self, ai_service: AIService, conversation_manager: ConversationManager,
                 max_context_messages: int, keep_recent_messages: int) -> None:
        self.ai_service = ai_service
        self.conversation_manager = conversation_manager
        self.max_context_messages = max(2, max_context_messages)
        self.keep_recent_messages = max(2, min(keep_recent_messages, self.max_context_messages - 1))
        self._running: set[str] = set()
        self._pending: dict[str, tuple[Conversation, str | None]] = {}

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
            while conversation_id in self._pending:
                current, requested_profile_id = self._pending.pop(conversation_id)
                attempt = (current.version, requested_profile_id)
                if attempt == last_attempt:
                    continue
                last_attempt = attempt
                await self._compact_once(current, requested_profile_id)
        finally:
            # Cancellation must not leave a permanent busy marker. Unfinished
            # memory remains eligible on the next successfully saved response.
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
        cutoff = len(conversation.messages) - self.keep_recent_messages
        chunk = [replace(message) for message in conversation.messages[conversation.summarized_message_count:cutoff]]
        if not chunk:
            return
        try:
            summary = await self.ai_service.summarize_memory(previous_summary, chunk, profile_id=profile_id)
            if summary:
                applied = self.conversation_manager.update_memory_if_version_matches(
                    conversation, summary, cutoff, expected_version=start_version)
                if not applied:
                    logger.debug("Discarded stale conversation memory: %s", conversation.id)
        except Exception:
            # Memory is an optimization; a failed summary must not interrupt the user response.
            logger.exception("Conversation memory compaction failed")
