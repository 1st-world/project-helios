"""Resolve separate draft and persisted conversation workspaces without a shared selection."""

from collections import OrderedDict
from copy import copy
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from models.workspace import WorkspaceBinding
from services.conversation_service import ConversationManager
from services.workspace_service import WorkspaceService


@dataclass
class DraftWorkspace:
    """Retain a browser draft and its first saved conversation for safe request retries."""

    workspace: WorkspaceService
    conversation_id: str | None = None


class ConversationWorkspaces:
    """Keep live authority objects separate while the conversation JSON owns durable state."""

    MAX_DRAFTS = 128

    def __init__(self, manager: ConversationManager) -> None:
        """Initialize a bounded draft inventory and lazy conversation authority cache."""
        self.manager = manager
        self.drafts: OrderedDict[str, DraftWorkspace] = OrderedDict()
        self.saved: dict[str, WorkspaceService] = {}
        self.owners: dict[int, str] = {}

    @staticmethod
    def binding(workspace: WorkspaceService) -> WorkspaceBinding:
        """Snapshot the selected path and reference identity for atomic persistence."""
        return WorkspaceBinding(
            root=str(workspace.root) if workspace.root is not None else None,
            revision=workspace.revision, session_id=workspace.session_id,
            read_options=workspace.read_options,
        )

    def create_draft(self) -> WorkspaceService:
        """Start a fresh folder-free draft without adding an empty saved conversation."""
        workspace = WorkspaceService()
        self.drafts[workspace.session_id] = DraftWorkspace(workspace)
        while len(self.drafts) > self.MAX_DRAFTS:
            self.drafts.popitem(last=False)
        return workspace

    def snapshot_drafts(self) -> list[dict]:
        """Retain bounded draft bindings for one managed restart without saving transcripts."""
        return [{"binding": self.binding(draft.workspace).model_dump(), "conversation_id": draft.conversation_id}
                for draft in self.drafts.values()]

    def restore_drafts(self, snapshot: object) -> None:
        """Rebuild the preceding worker's browser draft identities from its private handoff."""
        if not isinstance(snapshot, list):
            return
        for item in snapshot[-self.MAX_DRAFTS:]:
            try:
                binding = WorkspaceBinding.model_validate(item["binding"])
                conversation_id = item.get("conversation_id")
            except (KeyError, TypeError, ValidationError):
                continue
            if conversation_id is not None and not isinstance(conversation_id, str):
                continue
            self.drafts[binding.session_id] = DraftWorkspace(self._workspace_from(binding), conversation_id)

    @staticmethod
    def _workspace_from(binding: WorkspaceBinding) -> WorkspaceService:
        """Restore a binding without resolving paths or reading Workspace content."""
        workspace = WorkspaceService()
        workspace.root = Path(binding.root) if binding.root is not None else None
        workspace.revision = binding.revision
        workspace.session_id = binding.session_id
        workspace.read_options = binding.read_options
        return workspace

    def for_conversation(self, conversation) -> WorkspaceService:
        """Restore metadata without resolving paths, creating folders, or listing files."""
        workspace = self.saved.get(conversation.id)
        if workspace is None:
            workspace = self._workspace_from(conversation.workspace)
            self.saved[conversation.id] = workspace
            self.owners[id(workspace)] = conversation.id
        return workspace

    def draft(self, session_id: str) -> DraftWorkspace | None:
        """Refresh an existing draft's retention without accepting arbitrary identities."""
        draft = self.drafts.get(session_id)
        if draft is not None:
            self.drafts.move_to_end(session_id)
        return draft

    def adopt(
        self, workspace: WorkspaceService, *, profile_id: str | None = None,
    ):
        """Save a draft once and reuse it if preparation failed before the start event."""
        draft = self.draft(workspace.session_id)
        if draft is not None and draft.conversation_id is not None:
            conversation = self.manager.get(draft.conversation_id)
            if conversation is None:
                raise ValueError("The draft conversation is no longer available.")
            return conversation
        conversation = self.manager.create(
            self.binding(workspace), profile_id=profile_id,
        )
        self.saved[conversation.id] = copy(workspace)
        self.owners[id(self.saved[conversation.id])] = conversation.id
        if draft is not None:
            draft.conversation_id = conversation.id
        return conversation

    def owner(self, workspace: WorkspaceService) -> str | None:
        """Identify the conversation whose version and request slot guard this selection."""
        return self.owners.get(id(workspace))

    def forget(self, conversation_id: str) -> None:
        """Drop cached metadata after deletion while in-flight work keeps its own guard."""
        workspace = self.saved.pop(conversation_id, None)
        if workspace is not None:
            self.owners.pop(id(workspace), None)
