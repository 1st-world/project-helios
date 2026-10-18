/* Own the workspace tree, selected attachments, and workspace dialogs, coordinating the folder picker. */

import { $, closeMobileSidebar, refreshIcons, toast } from './ui.js';
import { createWorkspacePicker } from './workspace-picker.js';
import { attachmentIcon, attachmentSummary, attachmentWarnings } from './attachments.js';
import { describeRequestError } from './chat-feedback.js';

export function createWorkspace({ getActiveProfileId }) {
  const state = {
    workspaceFiles: [],
    workspaceRoot: null,
    workspaceRevision: null,
    workspaceSessionId: null,
    workspaceEntries: [],
    changingWorkspace: false,
    structureController: null,
    pendingFiles: [],
    attachingFile: false,
    generating: false,
    fileDialogVersion: 0,
    workspaceRequest: 0,
    attachmentMetadata: new Map(),
    inspection: null,
    inspectionFeedback: null,
    inspectionController: null
  };

  const picker = createWorkspacePicker({ openWorkspaceDialog, getWorkspaceRoot: () => state.workspaceRoot });

  function renderTree(entries, parent, query = '') {
    for (const entry of entries) {
      if (entry.type === 'directory') {
        const group = document.createElement('details');
        group.open = true;
        const label = document.createElement('summary');
        label.className = 'tree-entry tree-folder';
        label.innerHTML = '<i data-lucide="folder"></i>';
        const name = document.createElement('span');
        name.textContent = entry.name;
        label.append(name);
        const children = document.createElement('div');
        children.className = 'tree-children';
        renderTree(entry.children || [], children, query);
        if (!children.childElementCount) continue;
        group.append(label, children);
        parent.append(group);
      } else if (!query || entry.path.toLowerCase().includes(query)) {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'tree-entry file';
        row.dataset.path = entry.path;
        row.title = entry.path;
        row.setAttribute('aria-pressed', String(state.pendingFiles.includes(entry.path)));
        row.classList.toggle('selected', state.pendingFiles.includes(entry.path));
        row.disabled = state.attachingFile || state.generating;
        row.innerHTML = `<i data-lucide="${attachmentIcon(entry.path)}"></i>`;
        const name = document.createElement('span');
        name.textContent = query ? entry.path : entry.name;
        row.append(name);
        row.onclick = () => toggleFile(entry.path);
        parent.append(row);
      }
    }
  }

  function renderFileList() {
    const tree = $('#workspace-tree');
    tree.replaceChildren();
    const query = $('#file-search').value.trim().toLowerCase();
    renderTree(state.workspaceEntries, tree, query);
    if (!tree.childElementCount) {
      const empty = document.createElement('p');
      empty.className = 'workspace-empty-state';
      empty.textContent = !state.workspaceRoot ? 'Choose a workspace to attach selected files. Chat is available without a workspace. Independent file uploads are planned.'
        : query ? 'No matching files. Try another name or path.' : 'No files in this folder. Choose another folder in Workspace settings.';
      tree.append(empty);
    }
    refreshIcons();
  }

  function countFiles(entries) {
    return entries.reduce((count, entry) => count + (entry.type === 'file' ? 1 : countFiles(entry.children || [])), 0);
  }

  function applyWorkspace(data) {
    const revision = Number.isSafeInteger(data.revision) ? data.revision : null;
    const sessionId = typeof data.session_id === 'string' ? data.session_id : null;
    const changed = state.workspaceRoot !== data.root || state.workspaceRevision !== revision || state.workspaceSessionId !== sessionId;
    state.workspaceRoot = data.root;
    state.workspaceRevision = revision;
    state.workspaceSessionId = sessionId;
    state.workspaceEntries = Array.isArray(data.entries) ? data.entries : [];
    if (changed) {
      clearFocusFiles();
      $('#workspace-structure-enabled').checked = false;
      $('#workspace-structure-path').value = '';
      $('#workspace-structure-depth').value = '1';
      $('#workspace-structure-entries').value = '';
      $('#workspace-structure-absolute').checked = false;
      resetStructurePreview();
    }
    const folderName = data.root?.split(/[/\\]/).filter(Boolean).pop() || 'No workspace';
    const fileCount = countFiles(state.workspaceEntries);
    $('#workspace-folder-name').textContent = folderName;
    $('#workspace-file-count').textContent = data.root ? `${fileCount} file${fileCount === 1 ? '' : 's'} · Structure off` : 'Optional · Chat without a folder';
    $('#workspace-card').title = data.root ? `Manage workspace: ${data.root}` : 'Choose an optional workspace';
    $('#composer-workspace-name').textContent = folderName;
    $('#file-source-name').textContent = folderName;
    $('#file-source-name').title = data.root || 'No workspace selected';
    const paths = new Set();
    const collect = (entries) => entries.forEach((entry) => {
      if (entry.type === 'file') paths.add(entry.path);
      if (entry.children) collect(entry.children);
    });
    collect(state.workspaceEntries);
    state.workspaceFiles = state.workspaceFiles.filter((path) => paths.has(path));
    state.pendingFiles = state.pendingFiles.filter((path) => paths.has(path));
    for (const path of state.attachmentMetadata.keys()) {
      if (!state.workspaceFiles.includes(path)) state.attachmentMetadata.delete(path);
    }
    if (state.inspection && !inspectionIsCurrent()) invalidateInspection();
    renderAttachments();
    updateSelection();
    renderFileList();
    updateStructureControls();
  }

  async function loadWorkspace() {
    const request = ++state.workspaceRequest;
    try {
      const response = await fetch('/api/workspace');
      if (!response.ok) throw new Error('Could not load workspace.');
      const data = await response.json();
      if (request !== state.workspaceRequest) return;
      applyWorkspace(data);
    } catch {
      if (request !== state.workspaceRequest) return;
      $('#workspace-file-count').textContent = 'Folder unavailable';
      $('#composer-workspace-sub').textContent = 'Unavailable';
      $('#workspace-tree').textContent = 'Could not load files. Use Refresh to try again.';
      state.workspaceEntries = [];
      state.pendingFiles = [];
      invalidateInspection();
      updateSelection();
      toast('Could not load workspace.', 'error');
    }
  }

  function updateSelection() {
    $('#workspace-tree').querySelectorAll('[data-path]').forEach((row) => {
      const selected = state.pendingFiles.includes(row.dataset.path);
      row.classList.toggle('selected', selected);
      row.setAttribute('aria-pressed', String(selected));
      row.disabled = state.attachingFile || state.generating;
    });
    const count = state.pendingFiles.length;
    $('#selected-file-label').textContent = count ? `${count} file${count === 1 ? '' : 's'} selected` : 'No files selected';
    $('#selected-file-label').dataset.hasFile = String(count > 0);
    $('#selected-file-label').title = state.pendingFiles.join('\n');
    $('#attach-selected-file').disabled = state.attachingFile || state.generating || (!count && !state.workspaceFiles.length);
    $('#attach-selected-file').textContent = state.attachingFile ? 'Checking...' : !count ? 'Clear attachments'
      : inspectionIsCurrent() && !state.inspection.data.context_policy.blocked ? 'Apply selection' : 'Check selection';
    $('#refresh-workspace').disabled = state.attachingFile || state.generating || state.changingWorkspace;
    $('#file-search').disabled = !state.workspaceRoot || state.generating;
    $('#attachment-workspace-settings').disabled = state.attachingFile || state.generating;
  }

  function toggleFile(path) {
    if (state.attachingFile || state.generating) return;
    invalidateInspection();
    if (state.pendingFiles.includes(path)) {
      state.pendingFiles = state.pendingFiles.filter((selected) => selected !== path);
    } else {
      state.pendingFiles.push(path);
    }
    updateSelection();
  }

  function renderAttachments() {
    const bar = $('.context-bar');
    bar.querySelectorAll('.context-chip.file').forEach((chip) => chip.remove());
    for (const path of state.workspaceFiles) {
      const chip = $('#file-chip-template').content.firstElementChild.cloneNode(true);
      chip.dataset.workspaceFile = path;
      const metadata = state.attachmentMetadata.get(path);
      chip.querySelector('i[data-lucide]').setAttribute('data-lucide', attachmentIcon(path, metadata?.kind));
      chip.title = [path, metadata ? attachmentSummary(metadata) : '', ...attachmentWarnings(metadata || {}), 'Included with each message until removed.'].filter(Boolean).join('\n');
      const name = chip.querySelector('.file-chip-name');
      name.textContent = path;
      name.title = path;
      const remove = chip.querySelector('button');
      remove.disabled = state.generating;
      remove.title = `Remove ${path}`;
      remove.setAttribute('aria-label', `Remove ${path}`);
      remove.onclick = () => {
        if (state.generating) return;
        state.workspaceFiles = state.workspaceFiles.filter((selected) => selected !== path);
        state.pendingFiles = state.pendingFiles.filter((selected) => selected !== path);
        state.attachmentMetadata.delete(path);
        invalidateInspection();
        renderAttachments();
        updateSelection();
      };
      bar.append(chip);
    }
    refreshIcons();
  }

  function clearFocusFiles() {
    state.workspaceFiles = [];
    state.pendingFiles = [];
    state.attachmentMetadata.clear();
    invalidateInspection();
    renderAttachments();
    updateSelection();
  }

  function openWorkspaceDialog() {
    closeMobileSidebar();
    $('#workspace-path-input').value = state.workspaceRoot || '';
    updateStructureControls();
    $('#workspace-dialog').showModal();
  }

  async function openFileDialog() {
    ++state.fileDialogVersion;
    invalidateInspection();
    closeMobileSidebar();
    state.pendingFiles = [...state.workspaceFiles];
    $('#file-search').value = '';
    updateSelection();
    renderFileList();
    $('#file-dialog').showModal();
    $('#file-search').focus();
    await loadWorkspace();
  }

  function setGenerating(value) {
    state.generating = value;
    if (value) invalidateInspection();
    ['load-file', 'workspace-card', 'workspace-context-chip'].forEach((id) => {
      const el = $(`#${id}`);
      if (el) el.disabled = value;
    });
    $('.context-bar').querySelectorAll('.context-chip.file button').forEach((button) => { button.disabled = value; });
    if (value) resetStructurePreview();
    updateStructureControls();
    updateSelection();
  }

  function resetStructurePreview() {
    state.structureController?.abort();
    state.structureController = null;
    $('#workspace-structure-preview').hidden = true;
    $('#workspace-structure-preview').textContent = '';
    $('#workspace-structure-status').textContent = '';
  }

  function structureOptions() {
    const limit = (id) => {
      const input = $(`#${id}`);
      if (!input.value.trim()) return null;
      const value = Number(input.value);
      if (!Number.isSafeInteger(value) || value < 1) throw new Error('Structure depth and entry limits must be positive whole numbers or blank.');
      return value;
    };
    return {
      path: $('#workspace-structure-path').value.trim(),
      max_depth: limit('workspace-structure-depth'),
      max_entries: limit('workspace-structure-entries'),
      include_absolute_path: $('#workspace-structure-absolute').checked
    };
  }

  function getWorkspaceContext() {
    return {
      workspace_revision: state.workspaceRevision ?? undefined,
      workspace_session_id: state.workspaceSessionId ?? undefined,
      workspace_structure: state.workspaceRoot && $('#workspace-structure-enabled').checked ? structureOptions() : null
    };
  }

  function updateStructureControls() {
    const busy = state.generating || state.changingWorkspace || picker.isPicking();
    const connected = Boolean(state.workspaceRoot);
    const enabled = connected && $('#workspace-structure-enabled').checked;
    $('#workspace-path-input').disabled = busy;
    $('#browse-workspace-btn').disabled = busy;
    $('#apply-workspace').disabled = busy;
    $('#disconnect-workspace').disabled = busy || !connected;
    $('#workspace-structure-enabled').disabled = busy || !connected;
    $('#workspace-structure-options').disabled = busy || !enabled;
    $('#workspace-structure-options').hidden = !enabled;
    $('#preview-workspace-structure').disabled = busy || !enabled || Boolean(state.structureController);
    $('#composer-workspace-sub').textContent = enabled ? 'Structure on' : 'Structure off';
    $('#workspace-context-chip').title = enabled ? 'Selected project structure is included with messages. Open Workspace to review the scope.'
      : 'Project structure is off. Open Workspace to choose a folder or structure scope.';
    if (connected) {
      const count = countFiles(state.workspaceEntries);
      $('#workspace-file-count').textContent = `${count} file${count === 1 ? '' : 's'} · Structure ${enabled ? 'on' : 'off'}`;
    }
  }

  async function previewStructure() {
    if (state.generating || state.changingWorkspace || !state.workspaceRoot || !$('#workspace-structure-enabled').checked) return;
    resetStructurePreview();
    let options;
    try { options = structureOptions(); }
    catch (error) { toast(error.message, 'error'); return; }
    const revision = state.workspaceRevision;
    const sessionId = state.workspaceSessionId;
    const controller = new AbortController();
    state.structureController = controller;
    $('#workspace-structure-status').textContent = 'Reading selected structure locally...';
    updateStructureControls();
    try {
      const response = await fetch('/api/workspace/structure', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...options, workspace_revision: revision ?? undefined, workspace_session_id: sessionId ?? undefined }), signal: controller.signal
      });
      const data = await response.json();
      if (!response.ok) {
        const feedback = describeRequestError(data, 'Could not preview the selected structure.');
        throw new Error(feedback.message);
      }
      if (state.structureController !== controller || revision !== state.workspaceRevision || sessionId !== state.workspaceSessionId) return;
      $('#workspace-structure-preview').textContent = data.context;
      $('#workspace-structure-preview').hidden = false;
      $('#workspace-structure-status').textContent = 'Local preview. Files may change before sending.';
    } catch (error) {
      if (error.name !== 'AbortError' && state.structureController === controller) $('#workspace-structure-status').textContent = error.message;
    } finally {
      if (state.structureController === controller) {
        state.structureController = null;
        updateStructureControls();
      }
    }
  }

  async function changeWorkspace(path) {
    if (state.generating || state.changingWorkspace || picker.isPicking()) return;
    state.changingWorkspace = true;
    resetStructurePreview();
    invalidateInspection();
    updateStructureControls();
    try {
      const response = await fetch('/api/workspace/root', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, workspace_revision: state.workspaceRevision ?? undefined, workspace_session_id: state.workspaceSessionId ?? undefined })
      });
      const data = await response.json();
      if (!response.ok) {
        const feedback = describeRequestError(data, 'Could not change the workspace.');
        throw new Error(feedback.message);
      }
      ++state.workspaceRequest;
      applyWorkspace(data);
      $('#workspace-dialog').close();
      toast(path === null ? 'Workspace disconnected.' : 'Workspace folder updated.', 'success');
    } catch (error) {
      toast(error.message || 'Could not change the workspace.', 'error');
    } finally {
      state.changingWorkspace = false;
      updateStructureControls();
      updateSelection();
    }
  }

  function beforeDialogClose() {
    if (!picker.isPicking()) return true;
    picker.cancel();
    return false;
  }

  function inspectionIsCurrent() {
    const inspection = state.inspection;
    return Boolean(inspection && inspection.root === state.workspaceRoot && inspection.revision === state.workspaceRevision
      && inspection.sessionId === state.workspaceSessionId && inspection.profileId === getActiveProfileId()
      && inspection.version === state.fileDialogVersion && inspection.paths.length === state.pendingFiles.length
      && inspection.paths.every((path, index) => path === state.pendingFiles[index]));
  }

  function invalidateInspection() {
    state.inspectionController?.abort();
    state.inspectionController = null;
    state.attachingFile = false;
    state.inspection = null;
    state.inspectionFeedback = null;
    renderInspection();
  }

  function renderInspection() {
    const region = $('#attachment-inspection');
    const status = $('#attachment-inspection-status');
    const list = $('#attachment-inspection-files');
    list.replaceChildren();
    region.hidden = !state.inspection && !state.inspectionFeedback;
    region.dataset.state = state.inspectionFeedback?.type || 'ready';
    if (state.inspectionFeedback) {
      const { message, details = [] } = state.inspectionFeedback;
      status.textContent = [message, ...details].join('\n');
      return;
    }
    if (!state.inspection) {
      status.textContent = '';
      return;
    }
    const { files, context_policy: policy } = state.inspection.data;
    region.dataset.state = policy.blocked ? 'error' : policy.warning ? 'warning' : 'ready';
    const text = [policy.blocked ? 'The approximate attachment input exceeds the selected input budget in blocking mode. Reduce the selection or change the context policy before applying.'
      : policy.warning ? 'The approximate attachment input exceeds the selected input budget. Warning mode allows you to apply this selection.'
      : 'File checks passed locally. Review the delivery method, then apply this selection.'];
    if (Number.isSafeInteger(policy.estimated_input_tokens)) text.push(`Approximate attachment input: ${policy.estimated_input_tokens.toLocaleString()} tokens.`);
    text.push(policy.input_budget === null ? 'No input limit is selected. The provider validates input capacity when you send a message.' : `Selected input budget: ${policy.input_budget.toLocaleString()} tokens.`);
    if (policy.legacy_input_policy) text.push('Previous input policy: review the budget in Settings.');
    if (Number.isSafeInteger(policy.declared_model_input_limit)) text.push(`Declared model input limit: ${policy.declared_model_input_limit.toLocaleString()} tokens.`);
    if (policy.mode === 'off') text.push('Context token estimation is off.');
    text.push('This check covers attachments only. The question, selected structure, and conversation history are checked when you send a message.');
    if (files.some(file => file.delivery === 'native')) text.push('Image and PDF support is checked when you send a message.');
    status.textContent = text.join('\n');
    for (const file of files) {
      const item = document.createElement('li');
      const name = document.createElement('strong');
      name.textContent = file.path;
      const description = document.createElement('p');
      description.textContent = attachmentSummary(file);
      item.append(name, description);
      for (const warning of attachmentWarnings(file)) {
        const note = document.createElement('p');
        note.className = 'attachment-processing-note';
        note.textContent = warning;
        item.append(note);
      }
      list.append(item);
    }
  }

  function updateAttachmentMetadata(files) {
    if (!Array.isArray(files)) return;
    for (const file of files) {
      if (state.workspaceFiles.includes(file.path)) state.attachmentMetadata.set(file.path, file);
    }
    renderAttachments();
  }

  async function checkOrApplySelection() {
    const paths = [...state.pendingFiles];
    if (state.attachingFile || state.generating || (!paths.length && !state.workspaceFiles.length)) return;
    if (!paths.length || (inspectionIsCurrent() && !state.inspection.data.context_policy.blocked)) {
      state.workspaceFiles = paths.length ? state.inspection.data.files.map(file => file.path) : [];
      state.attachmentMetadata = new Map(paths.length ? state.inspection.data.files.map(file => [file.path, file]) : []);
      renderAttachments();
      $('#file-dialog').close();
      $('#prompt').focus();
      return;
    }
    const root = state.workspaceRoot;
    const revision = state.workspaceRevision;
    const sessionId = state.workspaceSessionId;
    const version = state.fileDialogVersion;
    const profileId = getActiveProfileId();
    const controller = new AbortController();
    const isCurrent = () => state.inspectionController === controller && $('#file-dialog').open && !state.generating
      && version === state.fileDialogVersion && root === state.workspaceRoot && revision === state.workspaceRevision
      && sessionId === state.workspaceSessionId && profileId === getActiveProfileId()
      && paths.length === state.pendingFiles.length && paths.every((path, index) => path === state.pendingFiles[index]);
    state.inspectionController = controller;
    state.attachingFile = true;
    state.inspectionFeedback = { type: 'checking', message: 'Checking selected files locally...' };
    renderInspection();
    updateSelection();
    try {
      const response = await fetch('/api/attachments/inspect', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths, profile_id: profileId, workspace_revision: revision ?? undefined, workspace_session_id: sessionId ?? undefined }), signal: controller.signal
      });
      let data;
      try { data = await response.json(); }
      catch { throw new Error(`Could not read the file check result. (HTTP ${response.status})`); }
      if (!response.ok) {
        const feedback = describeRequestError(data, 'Could not check the selected files.', `HTTP ${response.status}`);
        throw Object.assign(new Error(feedback.message), { feedback });
      }
      if (!isCurrent()) return;
      if (!Array.isArray(data.files) || !data.files.length || data.files.some(file => !file || typeof file.path !== 'string')
          || !data.context_policy || !['warn', 'block', 'off'].includes(data.context_policy.mode)) {
        throw new Error('Could not read the file check result. Check the selection again.');
      }
      state.inspection = { paths, root, revision, sessionId, version, profileId, data };
      state.inspectionFeedback = null;
      renderInspection();
    } catch (error) {
      if (error.name !== 'AbortError' && isCurrent()) {
        const feedback = error.feedback || describeRequestError({ message: error.message }, 'Could not check the selected files.');
        state.inspection = null;
        state.inspectionFeedback = { type: 'error', ...feedback };
        renderInspection();
      }
    } finally {
      if (state.inspectionController === controller) {
        state.inspectionController = null;
        state.attachingFile = false;
        if (state.inspectionFeedback?.type === 'checking') {
          state.inspectionFeedback = null;
          renderInspection();
        }
        updateSelection();
      }
    }
  }

  function init() {
    $('#workspace-card').onclick = openWorkspaceDialog;
    $('#workspace-context-chip').onclick = openWorkspaceDialog;
    $('#attachment-workspace-settings').onclick = () => {
      $('#file-dialog').close();
      openWorkspaceDialog();
    };
    $('#disconnect-workspace').onclick = () => changeWorkspace(null);
    $('#preview-workspace-structure').onclick = previewStructure;
    ['workspace-structure-enabled', 'workspace-structure-path', 'workspace-structure-depth', 'workspace-structure-entries', 'workspace-structure-absolute'].forEach((id) => {
      $(`#${id}`).addEventListener('input', () => {
        resetStructurePreview();
        updateStructureControls();
      });
    });
    $('#workspace-dialog').addEventListener('close', () => { if (!$('#workspace-dialog').open) resetStructurePreview(); });
    $('#file-search').oninput = renderFileList;
    $('#attach-selected-file').onclick = checkOrApplySelection;
    $('#file-dialog').addEventListener('close', () => {
      // Ignore a queued close event after the dialog has already reopened.
      if ($('#file-dialog').open) return;
      ++state.fileDialogVersion;
      invalidateInspection();
      updateSelection();
    });

    $('#refresh-workspace').onclick = () => {
      invalidateInspection();
      updateSelection();
      loadWorkspace();
    };

    $('#workspace-form').onsubmit = async (event) => {
      event.preventDefault();
      await changeWorkspace($('#workspace-path-input').value.trim());
    };

    $('#load-file').onclick = openFileDialog;

    picker.init();
  }

  return {
    init, load: loadWorkspace, clearFocusFiles, setGenerating, beforeDialogClose,
    restorePicker: picker.restore, getAttachmentFiles: () => [...state.workspaceFiles], getWorkspaceContext, updateAttachmentMetadata
  };
}
