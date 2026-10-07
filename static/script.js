/* Connect frontend feature modules and start the initial page and session restoration. */

import { createChat } from './chat.js';
import { createProfiles } from './profiles.js';
import { createWorkspace } from './workspace.js';
import { initializeUI } from './ui.js';
import { initializeSettings } from './settings.js';
import { createUsage } from './usage.js';

const workspace = createWorkspace({ getActiveProfileId: () => profiles.getActiveProfileId() });
const profiles = createProfiles({ isGenerating: () => chat.isGenerating() });
const chat = createChat({
  getActiveProfileId: profiles.getActiveProfileId,
  getAttachmentFiles: workspace.getAttachmentFiles,
  onAttachmentMetadata: workspace.updateAttachmentMetadata,
  clearFocusFiles: workspace.clearFocusFiles,
  onConversationChange(id) { usage.selectConversation(id); },
  onGeneratingChange(value) {
    workspace.setGenerating(value);
    profiles.setGenerating(value);
    usage.setGenerating(value);
  }
});
const usage = createUsage({ footer: chat.usageFooter, renderFooter: chat.renderUsageContent });
usage.init();

const settings = initializeSettings({
  onConversationUsageVisibilityChange(visible) {
    chat.setUsageVisibility(visible);
    usage.setFooterVisible(visible);
  },
  hasUnsavedProfileChanges: profiles.hasUnsavedChanges,
  isProfileSaving: profiles.isSaving
});
initializeUI({
  beforeDialogClose(dialog) {
    if (dialog.id === 'workspace-dialog') return workspace.beforeDialogClose();
    if (dialog.id === 'settings-dialog') return settings.beforeDialogClose();
    return true;
  }
});
workspace.init();
profiles.init();
chat.init();

profiles.checkHealth();
const workspaceReady = workspace.load();
chat.newChat();
profiles.load();
workspace.restorePicker(workspaceReady);
