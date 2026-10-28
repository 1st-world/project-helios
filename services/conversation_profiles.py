"""Restore conversation model choices independently of transcript versions and request snapshots."""

import sqlite3

from models.conversation import Conversation
from services.conversation_service import ConversationManager
from services.profile_service import ProfileNotFoundError, ProfileService
from services.work_run_store import WorkContextUnavailableError, WorkRunStore


class ConversationProfiles:
    """Persist profile references while leaving shared connection details in the profile registry."""

    def __init__(
        self, manager: ConversationManager, profiles: ProfileService,
        runs: WorkRunStore,
    ) -> None:
        """Bind durable conversations, shared profiles, and historical tool response metadata."""
        self.manager = manager
        self.profiles = profiles
        self.runs = runs

    def ensure(self, conversation: Conversation) -> None:
        """Initialize older selections once from recorded responses or the current default."""
        if conversation.profile_initialized:
            return
        profile_id = None
        for message in reversed(conversation.messages):
            if message.role != "assistant":
                continue
            profile_id = message.profile_id
            if not profile_id and message.run_id:
                try:
                    self.runs.get(message.run_id, conversation.id)
                    profile_id = self.runs.current_segment(message.run_id).get("profile", {}).get("id")
                except (WorkContextUnavailableError, sqlite3.Error):
                    continue
            if isinstance(profile_id, str) and profile_id.strip():
                break
            profile_id = None
        if profile_id is None:
            default = self.profiles.get_active_profile()
            profile_id = default.id if default is not None else None
        self.manager.set_profile(conversation, profile_id)

    def selection(self, conversation: Conversation) -> dict:
        """Expose the saved reference and availability without copying credentials or settings."""
        self.ensure(conversation)
        profile = (
            self.profiles.get_profile(conversation.profile_id)
            if conversation.profile_id is not None else None
        )
        return {
            "profile_id": conversation.profile_id,
            "profile_revision": conversation.profile_revision,
            "profile_available": profile is not None,
        }

    def select(
        self, conversation: Conversation, profile_id: str, *,
        expected_revision: int,
    ) -> dict:
        """Update an existing choice only when its independent revision still matches."""
        self.ensure(conversation)
        if self.profiles.get_profile(profile_id) is None:
            raise ProfileNotFoundError("Profile not found.")
        self.manager.set_profile(
            conversation, profile_id, expected_revision=expected_revision,
        )
        return self.selection(conversation)
