/* Connect frontend feature modules and start the initial page and session restoration. */

import { createChat } from './chat.js';
import { createProfiles } from './profiles.js';
import { createWorkspace } from './workspace.js';
import { initializeUI } from './ui.js';

const workspace = createWorkspace();
const profiles = createProfiles({ isGenerating: () => chat.isGenerating() });
const chat = createChat({
  getActiveProfileId: profiles.getActiveProfileId,
  getWorkspaceFile: workspace.getWorkspaceFile,
  clearFocusFile: workspace.clearFocusFile,
  onGeneratingChange(value) {
    workspace.setGenerating(value);
    profiles.setGenerating(value);
  }
});

initializeUI({
  beforeDialogClose: (dialog) => dialog.id !== 'workspace-dialog' || workspace.beforeDialogClose()
});
workspace.init();
profiles.init();
chat.init();

profiles.checkHealth();
const workspaceReady = workspace.load();
chat.newChat();
profiles.load();
workspace.restorePicker(workspaceReady);
