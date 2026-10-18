/* Coordinate workspace browsing and mixed attachment selections, inspection, and restoration. */

import { $, closeMobileSidebar, refreshIcons, toast } from './ui.js';
import { createWorkspacePicker } from './workspace-picker.js';
import { attachmentIcon, attachmentKey, attachmentSourceLabel, attachmentSummary, attachmentWarnings } from './attachments.js';
import { createUploads } from './uploads.js';
import { describeRequestError } from './chat-feedback.js';

export function createWorkspace({ getActiveProfileId }) {
  // This bounds rendered rows per branch, without limiting file selection or model input.
  const treePageSize = 200;
  const state = {
    attachments: [],
    workspaceRoot: null,
    workspaceRevision: null,
    workspaceSessionId: null,
    workspaceEntries: [],
    workspaceFileCount: null,
    workspaceController: null,
    workspaceLoadingEntries: false,
    changingWorkspace: false,
    structureController: null,
    pendingFiles: [],
    attachingFile: false,
    generating: false,
    fileDialogVersion: 0,
    workspaceRequest: 0,
    selectionEdited: false,
    restoreController: null,
    inspection: null,
    inspectionFeedback: null,
    inspectionController: null
  };

  const picker = createWorkspacePicker({ openWorkspaceDialog, getWorkspaceRoot: () => state.workspaceRoot });

  const uploads = createUploads({
    isBusy: () => state.attachingFile || state.generating,
    getPending: () => state.pendingFiles,
    onToggle: toggleAttachment,
    onUploaded(file) {
      invalidateInspection();
      state.pendingFiles.push(file);
      updateSelection();
    },
    onDeleted(file) {
      if (state.attachments.some(item => attachmentKey(item) === attachmentKey(file))) state.selectionEdited = true;
      state.attachments = state.attachments.filter(item => attachmentKey(item) !== attachmentKey(file));
      state.pendingFiles = state.pendingFiles.filter(item => attachmentKey(item) !== attachmentKey(file));
      invalidateInspection();
      renderAttachments();
      updateSelection();
    },
    onInventory(files) {
      const inventory = new Map(files.map(file => [attachmentKey(file), file]));
      let changed = false;
      for (const file of [...state.attachments, ...state.pendingFiles]) {
        if (file.reference.source !== 'upload') continue;
        const original = inventory.get(attachmentKey(file));
        const available = Boolean(original && original.available !== false);
        if (file.available !== available) changed = true;
        file.available = available;
        if (original) file.path = original.path;
      }
      if (changed) invalidateInspection();
      renderAttachments();
      updateSelection();
    },
    onChange() { invalidateInspection(); updateSelection(); }
  });

  function workspaceFile(path) {
    return { path, reference: { source: 'workspace', path, workspace_revision: state.workspaceRevision, workspace_session_id: state.workspaceSessionId } };
  }

  function pendingWorkspacePath(path) {
    return state.pendingFiles.some(file => file.reference.source === 'workspace' && file.path === path);
  }

  function sameSelection(first, second) {
    return first.length === second.length && first.every((file, index) => attachmentKey(file) === attachmentKey(second[index]));
  }

  function* matchingFiles(entries, query) {
    for (const entry of entries) {
      if (entry.type === 'directory') yield* matchingFiles(entry.children || [], query);
      else if (entry.path.toLowerCase().includes(query)) yield entry;
    }
  }

  function renderTree(entries, parent, query = '') {
    const iterator = query ? matchingFiles(entries, query) : entries[Symbol.iterator]();
    let next = iterator.next();
    let more = null;

    function appendPage() {
      if (!$('#file-dialog').open || state.attachingFile || state.generating) return;
      const continueFocus = more === document.activeElement;
      more?.remove();
      const fragment = document.createDocumentFragment();
      for (let count = 0; count < treePageSize && !next.done; count++) {
        const entry = next.value;
        next = iterator.next();
        if (entry.type === 'directory') {
          const group = document.createElement('details');
          const label = document.createElement('summary');
          label.className = 'tree-entry tree-folder';
          label.innerHTML = '<i data-lucide="folder"></i>';
          const name = document.createElement('span');
          name.textContent = entry.name;
          label.append(name);
          const children = document.createElement('div');
          children.className = 'tree-children';
          group.addEventListener('toggle', () => {
            if (!group.isConnected || !$('#file-dialog').open) return;
            children.replaceChildren();
            if (group.open) renderTree(entry.children || [], children);
          });
          group.append(label, children);
          fragment.append(group);
        } else {
          const row = document.createElement('button');
          row.type = 'button';
          row.className = 'tree-entry file';
          row.dataset.path = entry.path;
          row.title = entry.path;
          row.setAttribute('aria-pressed', String(pendingWorkspacePath(entry.path)));
          row.classList.toggle('selected', pendingWorkspacePath(entry.path));
          row.disabled = state.attachingFile || state.generating;
          row.innerHTML = `<i data-lucide="${attachmentIcon(entry.path)}"></i>`;
          const name = document.createElement('span');
          name.textContent = query ? entry.path : entry.name;
          row.append(name);
          row.onclick = () => toggleFile(entry.path);
          fragment.append(row);
        }
      }
      if (!next.done) {
        more = document.createElement('button');
        more.type = 'button';
        more.className = 'tree-entry tree-more';
        more.textContent = 'Show more entries';
        more.onclick = appendPage;
        fragment.append(more);
      }
      const firstNewRow = fragment.querySelector('.tree-entry');
      parent.append(fragment);
      refreshIcons(parent);
      if (continueFocus) firstNewRow?.focus();
    }

    appendPage();
  }

  function renderFileList() {
    if (!$('#file-dialog').open) return;
    const tree = $('#workspace-tree');
    tree.replaceChildren();
    const query = $('#file-search').value.trim().toLowerCase();
    renderTree(state.workspaceEntries, tree, query);
    if (!tree.childElementCount) {
      const empty = document.createElement('p');
      empty.className = 'workspace-empty-state';
      empty.textContent = !state.workspaceRoot ? 'No workspace selected. Use Choose files above, or connect a workspace to browse project files.'
        : query ? 'No matching files. Try another name or path.' : 'No files in this folder. Choose another folder in Workspace settings.';
      tree.append(empty);
    }
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
    if (changed) {
      state.workspaceEntries = [];
      state.workspaceFileCount = null;
      const hadWorkspaceFiles = state.attachments.some(file => file.reference.source === 'workspace');
      state.attachments = state.attachments.filter(file => file.reference.source === 'upload');
      state.pendingFiles = state.pendingFiles.filter(file => file.reference.source === 'upload');
      if (hadWorkspaceFiles) state.selectionEdited = true;
      invalidateInspection();
      $('#workspace-structure-enabled').checked = false;
      $('#workspace-structure-path').value = '';
      $('#workspace-structure-depth').value = '1';
      $('#workspace-structure-entries').value = '';
      $('#workspace-structure-absolute').checked = false;
      resetStructurePreview();
    }
    if (data.entries_loaded !== false) {
      state.workspaceEntries = Array.isArray(data.entries) ? data.entries : [];
      state.workspaceFileCount = countFiles(state.workspaceEntries);
    }
    const folderName = data.root?.split(/[/\\]/).filter(Boolean).pop() || 'No workspace';
    $('#workspace-folder-name').textContent = folderName;
    $('#workspace-file-count').textContent = data.root ? 'Folder connected · Structure off' : 'Optional · Chat without a folder';
    $('#workspace-card').title = data.root ? `Manage workspace: ${data.root}` : 'Choose an optional workspace';
    $('#composer-workspace-name').textContent = folderName;
    $('#file-source-name').textContent = folderName;
    $('#file-source-name').title = data.root || 'No workspace selected';
    $('.attachment-workspace').dataset.connected = String(Boolean(data.root));
    if (state.inspection && !inspectionIsCurrent()) invalidateInspection();
    renderAttachments();
    updateSelection();
    renderFileList();
    updateStructureControls();
  }

  async function loadWorkspace({ includeEntries = false } = {}) {
    const request = ++state.workspaceRequest;
    state.workspaceController?.abort();
    const controller = new AbortController();
    state.workspaceController = controller;
    state.workspaceLoadingEntries = includeEntries;
    try {
      const response = await fetch(includeEntries ? '/api/workspace' : '/api/workspace?include_entries=false', { signal: controller.signal });
      if (!response.ok) throw new Error('Could not load workspace.');
      const data = await response.json();
      if (request !== state.workspaceRequest) return;
      applyWorkspace(data);
    } catch (error) {
      if (error.name === 'AbortError' || request !== state.workspaceRequest) return;
      $('#workspace-file-count').textContent = 'Folder unavailable';
      $('#composer-workspace-sub').textContent = 'Unavailable';
      $('#workspace-tree').textContent = 'Could not load files. Use Refresh to try again.';
      state.workspaceEntries = [];
      state.workspaceFileCount = null;
      invalidateInspection();
      updateSelection();
      toast('Could not load workspace.', 'error');
    } finally {
      if (state.workspaceController === controller) {
        state.workspaceController = null;
        state.workspaceLoadingEntries = false;
      }
    }
  }

  function updateSelection() {
    $('#workspace-tree').querySelectorAll('[data-path]').forEach((row) => {
      const selected = pendingWorkspacePath(row.dataset.path);
      row.classList.toggle('selected', selected);
      row.setAttribute('aria-pressed', String(selected));
      row.disabled = state.attachingFile || state.generating;
    });
    $('#workspace-tree').querySelectorAll('.tree-more').forEach((button) => { button.disabled = state.attachingFile || state.generating; });
    const count = state.pendingFiles.length;
    const busy = state.attachingFile || state.generating || uploads.isUploading();
    $('#selected-file-label').textContent = count ? `${count} file${count === 1 ? '' : 's'} selected` : 'No files selected';
    $('#selected-file-label').dataset.hasFile = String(count > 0);
    $('#selected-file-label').title = state.pendingFiles.map(file => `${file.path} (${attachmentSourceLabel(file)})`).join('\n');
    $('#attach-selected-file').disabled = busy || (!count && !state.attachments.length);
    $('#attach-selected-file').textContent = state.attachingFile ? 'Checking...' : !count ? 'Clear attachments'
      : inspectionIsCurrent() && !state.inspection.data.context_policy.blocked ? 'Apply selection' : 'Check selection';
    $('#refresh-workspace').disabled = state.attachingFile || state.generating || state.changingWorkspace;
    $('#file-search').disabled = !state.workspaceRoot || state.generating;
    $('#attachment-workspace-settings').disabled = busy;
    uploads.update();
  }

  function toggleFile(path) {
    toggleAttachment(workspaceFile(path));
  }

  function toggleAttachment(file) {
    if (state.attachingFile || state.generating || uploads.isUploading()) return;
    invalidateInspection();
    const key = attachmentKey(file);
    if (state.pendingFiles.some(item => attachmentKey(item) === key)) {
      state.pendingFiles = state.pendingFiles.filter(item => attachmentKey(item) !== key);
    } else {
      state.pendingFiles.push(file);
    }
    updateSelection();
  }

  function renderAttachments() {
    const bar = $('.context-bar');
    const focusedKey = document.activeElement?.closest('[data-attachment-key]')?.dataset.attachmentKey;
    bar.querySelectorAll('.context-chip.file').forEach(chip => chip.remove());
    for (const file of state.attachments) {
      const chip = $('#file-chip-template').content.firstElementChild.cloneNode(true);
      const key = attachmentKey(file);
      chip.dataset.attachmentKey = key;
      chip.dataset.source = file.reference.source;
      if (file.reference.source === 'workspace') chip.dataset.workspaceFile = file.path;
      chip.querySelector('i[data-lucide]').setAttribute('data-lucide', attachmentIcon(file.path, file.kind));
      chip.title = [file.path, attachmentSourceLabel(file), file.reference.attachment_id || '', attachmentSummary(file), ...attachmentWarnings(file),
        file.available === false ? 'Original unavailable. Select the file again or remove this attachment.' : 'Included with each new message until removed.'].filter(Boolean).join('\n');
      chip.dataset.unavailable = String(file.available === false);
      chip.querySelector('.file-chip-name').textContent = file.path;
      chip.querySelector('.file-chip-source').textContent = file.available === false ? 'Unavailable' : attachmentSourceLabel(file);
      const remove = chip.querySelector('button');
      remove.disabled = state.generating;
      const identity = file.reference.source === 'upload' ? ` (${file.reference.attachment_id.slice(0, 8)})` : '';
      remove.title = `Remove ${attachmentSourceLabel(file).toLowerCase()} attachment: ${file.path}${identity}`;
      remove.setAttribute('aria-label', remove.title);
      remove.onclick = () => {
        if (state.generating) return;
        state.selectionEdited = true;
        state.attachments = state.attachments.filter(item => attachmentKey(item) !== key);
        state.pendingFiles = state.pendingFiles.filter(item => attachmentKey(item) !== key);
        invalidateInspection();
        renderAttachments();
        updateSelection();
        const next = [...bar.querySelectorAll('.context-chip.file button')][0] || $('#load-file');
        next.focus();
      };
      bar.append(chip);
      if (focusedKey === key) remove.focus({ preventScroll: true });
    }
    refreshIcons(bar);
  }

  function clearFocusFiles() {
    state.restoreController?.abort();
    state.restoreController = null;
    uploads.cancel();
    state.attachments = [];
    state.pendingFiles = [];
    state.selectionEdited = false;
    invalidateInspection();
    renderAttachments();
    updateSelection();
  }

  async function restoreAttachments(sources = []) {
    clearFocusFiles();
    const controller = new AbortController();
    state.restoreController = controller;
    state.attachments = sources.map(reference => reference.source === 'workspace'
      ? { path: reference.path, reference }
      : { path: `Uploaded file (${reference.attachment_id.slice(0, 8)})`, reference });
    state.pendingFiles = [...state.attachments];
    renderAttachments();
    updateSelection();
    await Promise.all(state.attachments.filter(file => file.reference.source === 'upload').map(async file => {
      try {
        const response = await fetch(`/api/attachments/uploads/${file.reference.attachment_id}`, { signal: controller.signal });
        const data = await response.json();
        if (state.restoreController !== controller || controller.signal.aborted) return;
        if (response.ok) Object.assign(file, { path: data.filename, size_bytes: data.size_bytes, available: true });
        else if (response.status === 410) file.available = false;
      } catch (error) {
        if (error.name === 'AbortError') return;
      }
      if (state.restoreController === controller) { renderAttachments(); updateSelection(); }
    }));
  }

  function getAttachmentSelection({ regenerating = false } = {}) {
    if (regenerating && !state.selectionEdited) return {};
    return { attachment_sources: state.attachments.map(file => ({ ...file.reference })) };
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
    state.pendingFiles = [...state.attachments];
    $('#file-search').value = '';
    updateSelection();
    $('#workspace-tree').textContent = state.workspaceRoot ? 'Loading files...' : 'No workspace selected.';
    $('#file-dialog').showModal();
    $('#choose-upload-files').focus();
    await Promise.all([loadWorkspace({ includeEntries: true }), uploads.load()]);
  }

  function setGenerating(value) {
    state.generating = value;
    if (value) { invalidateInspection(); uploads.cancel(); }
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
      const count = state.workspaceFileCount;
      const files = count === null ? 'Folder connected' : `${count} file${count === 1 ? '' : 's'}`;
      $('#workspace-file-count').textContent = `${files} · Structure ${enabled ? 'on' : 'off'}`;
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
        body: JSON.stringify({ path, workspace_revision: state.workspaceRevision ?? undefined, workspace_session_id: state.workspaceSessionId ?? undefined, include_entries: false })
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
      && inspection.version === state.fileDialogVersion && sameSelection(inspection.files, state.pendingFiles));
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
      name.textContent = `${file.path} · ${attachmentSourceLabel(file)}`;
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
    for (const metadata of files) {
      const file = state.attachments.find(item => metadata.reference ? attachmentKey(item) === attachmentKey(metadata)
        : item.reference.source === 'workspace' && item.path === metadata.path);
      if (file) Object.assign(file, metadata);
    }
    renderAttachments();
  }

  async function checkOrApplySelection() {
    const files = [...state.pendingFiles];
    if (state.attachingFile || state.generating || (!files.length && !state.attachments.length) || uploads.isUploading()) return;
    if (!files.length || (inspectionIsCurrent() && !state.inspection.data.context_policy.blocked)) {
      state.attachments = files.length ? state.inspection.data.files : [];
      state.selectionEdited = true;
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
      && sameSelection(files, state.pendingFiles);
    state.inspectionController = controller;
    state.attachingFile = true;
    state.inspectionFeedback = { type: 'checking', message: 'Checking selected files locally...' };
    renderInspection();
    updateSelection();
    try {
      const response = await fetch('/api/attachments/inspect', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ attachment_sources: files.map(file => file.reference), profile_id: profileId, workspace_revision: revision ?? undefined, workspace_session_id: sessionId ?? undefined }), signal: controller.signal
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
      data.files = data.files.map(file => ({ ...file, reference: file.reference || workspaceFile(file.path).reference }));
      if (!sameSelection(data.files, files)) throw new Error('The checked sources changed. Check the selection again.');
      state.inspection = { files, root, revision, sessionId, version, profileId, data };
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
    uploads.init();
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
      if (state.workspaceLoadingEntries) {
        ++state.workspaceRequest;
        state.workspaceController?.abort();
        state.workspaceController = null;
        state.workspaceLoadingEntries = false;
      }
      $('#workspace-tree').replaceChildren();
      uploads.cancel();
      $('#uploaded-files').replaceChildren();
      invalidateInspection();
      updateSelection();
    });

    $('#refresh-workspace').onclick = () => {
      invalidateInspection();
      updateSelection();
      loadWorkspace({ includeEntries: true });
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
    restorePicker: picker.restore, getAttachmentSelection, restoreAttachments, getWorkspaceContext, updateAttachmentMetadata,
    markAttachmentsSaved() { state.selectionEdited = false; }
  };
}
