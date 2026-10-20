/* Connect frontend feature modules and start the initial page and session restoration. */

import { createChat } from './chat.js';
import { createProfiles } from './profiles.js';
import { createWorkspace } from './workspace.js';
import { initializeUI } from './ui.js';
import { initializeSettings } from './settings.js';
import { createUsage } from './usage.js';
import { createExploration } from './exploration.js';

const workspace = createWorkspace({
  getActiveProfileId: () => profiles.getActiveProfileId(),
  onWorkspaceSaved: (id, version) => chat.setWorkspaceVersion(id, version)
});
const profiles = createProfiles({ isGenerating: () => chat.isGenerating() });
const exploration = createExploration({
  getWorkspace: workspace.getWorkspaceInfo,
  chooseWorkspace: workspace.chooseWorkspace,
  renderContent: render => chat.renderUsageContent(render)
});
const chat = createChat({
  exploration,
  getActiveProfileId: profiles.getActiveProfileId,
  getAttachmentSelection: workspace.getAttachmentSelection,
  restoreAttachments: workspace.restoreAttachments,
  onAttachmentsSaved: workspace.markAttachmentsSaved,
  getWorkspaceContext: workspace.getWorkspaceContext,
  selectWorkspace: workspace.selectConversation,
  startWorkspaceDraft: workspace.startDraft,
  adoptWorkspace: workspace.adoptConversation,
  onAttachmentMetadata: workspace.updateAttachmentMetadata,
  clearFocusFiles: workspace.clearFocusFiles,
  openSettings: profiles.openSettings,
  onConversationChange(id) { usage.selectConversation(id); },
  onGeneratingChange(value) {
    workspace.setGenerating(value);
    profiles.setGenerating(value);
    usage.setGenerating(value);
    exploration.setGenerating(value);
  }
});
const usage = createUsage({ footer: chat.usageFooter, renderFooter: chat.renderUsageContent });
usage.init();

const settings = initializeSettings({
  onConversationUsageVisibilityChange(visible) {
    chat.setUsageVisibility(visible);
    usage.setFooterVisible(visible);
  },
  onResponseUsageVisibilityChange: chat.setResponseUsageVisibility,
  hasUnsavedProfileChanges: profiles.hasUnsavedChanges,
  isProfileSaving: profiles.isSaving
});
initializeUI({
  beforeDialogClose(dialog) {
    if (dialog.id === 'workspace-dialog') return workspace.beforeDialogClose();
    if (dialog.id === 'file-dialog') return workspace.beforeAttachmentDialogClose();
    if (dialog.id === 'settings-dialog') return settings.beforeDialogClose();
    return true;
  }
});
workspace.init();
profiles.init();
chat.init();

profiles.checkHealth();
const workspaceReady = chat.newChat();
profiles.load();
workspaceReady.then(() => workspace.restorePicker(workspaceReady));
