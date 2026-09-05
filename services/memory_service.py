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

    async def compact_if_needed(self, conversation: Conversation) -> None:
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
            summary = await self.ai_service.summarize_memory(previous_summary, chunk)
            if summary:
                applied = self.conversation_manager.update_memory_if_version_matches(
                    conversation, summary, cutoff, expected_version=start_version)
                if not applied:
                    logger.debug("Discarded stale conversation memory: %s", conversation.id)
        except Exception:
            # Memory is an optimization; a failed summary must not interrupt the user response.
            logger.exception("Conversation memory compaction failed")
