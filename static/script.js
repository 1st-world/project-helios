/* Connect frontend feature modules and start the initial page and session restoration. */

import { createChat } from './chat.js';
import { createProfiles } from './profiles.js';
import { createWorkspace } from './workspace.js';
import { initializeUI } from './ui.js';
import { initializeSettings } from './settings.js';
import { createUsage } from './usage.js';
import { createExploration } from './exploration.js';
import { createWorkToolSettings } from './work-tool-settings.js';

const workspace = createWorkspace({
  getActiveProfileId: () => profiles.getSelectedProfileId(),
  getModelContextSignature: () => profiles.getModelContextSignature(),
  onWorkspaceSaved: (id, version) => chat.setWorkspaceVersion(id, version)
});
const profiles = createProfiles({ isGenerating: () => chat.isGenerating(), onModelContextChange: () => workspace.modelContextChanged(),
  onSelectionChange: () => chat.updateComposerState() });
const exploration = createExploration({
  getWorkspace: workspace.getWorkspaceInfo,
  chooseWorkspace: workspace.chooseWorkspace,
  getLimits: () => workToolSettings.limits(),
  onAction(action, context) {
    if (action === 'work-settings') return workToolSettings.open({ focusLimits: true });
    if (action === 'prepare-request') return chat.prepareExploration(context);
    if (action === 'resume-run') return chat.resumeWorkRun(context);
    if (action === 'stop-run') return chat.stopWorkRun(context);
    if (action === 'workspace') return workspace.chooseWorkspace();
    if (action === 'attachments') return document.querySelector('#load-file').click();
    return profiles.openSettings({ connections: 'connections', usage: 'usage', limits: 'context' }[action]);
  },
  renderContent: render => chat.renderUsageContent(render),
  onPausedChange: (id, run) => chat.setPausedRun(id, run)
});
const chat = createChat({
  exploration,
  getActiveProfileId: profiles.getSelectedProfileId,
  getModelSelection: profiles.getModelSelection,
  onPausedModelChange: profiles.setPausedRun,
  prepareRequestProfile: profiles.prepareRequestProfile,
  selectModel: profiles.selectConversation,
  startModelDraft: profiles.startDraft,
  adoptModel: profiles.adoptConversation,
  refreshModelSelection: profiles.refreshModelSelection,
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
    workToolSettings.updateState();
  }
});
const workToolSettings = createWorkToolSettings({ getWorkspace: workspace.getWorkspaceInfo, isGenerating: chat.isGenerating });
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
    if (dialog.id === 'work-tools-settings-dialog') return workToolSettings.beforeDialogClose();
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
