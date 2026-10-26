/* Coordinate workspace context and a single host-file attachment selection and restoration flow. */

import { $, closeMobileSidebar, refreshIcons, toast } from './ui.js';
import { createWorkspacePicker } from './workspace-picker.js';
import { attachmentIcon, attachmentKey, attachmentSize, attachmentSourceLabel, attachmentSummary, attachmentWarnings } from './attachments.js';
import { createFilePicker } from './file-picker.js';
import { createAttachmentDrafts } from './attachment-drafts.js';
import { describeRequestError } from './chat-feedback.js';
import { createDisclosure } from './disclosure.js';
import { showActionDialog } from './dialogs.js';

export function createWorkspace({ getActiveProfileId, onWorkspaceSaved }) {
  // Bound selected-file rendering without changing attachment resource limits.
  const selectionPageSize = 200;
  const state = {
    attachments: [],
    workspaceRoot: null,
    workspaceRevision: null,
    workspaceSessionId: null,
    conversationId: null,
    workspaceAvailable: false,
    workspaceReady: null,
    workspaceController: null,
    changingWorkspace: false,
    contextController: null,
    pendingFiles: [],
    visiblePending: 200,
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

  let contextDisclosure = null;
  const attachmentDrafts = createAttachmentDrafts({
    getFiles: () => [...state.attachments, ...($('#file-dialog').open ? state.pendingFiles : [])]
  });
  const picker = createWorkspacePicker({ openWorkspaceDialog, getWorkspaceRoot: () => state.workspaceRoot, getWorkspaceScope: workspaceScope,
    isBusy: () => state.generating || state.changingWorkspace || !state.workspaceSessionId,
    onStateChange: updateWorkspaceControls, onCloseRequested: requestWorkspaceDialogClose });

  const filePicker = createFilePicker({
    async getWorkspaceScope() {
      await state.workspaceReady;
      if (!state.workspaceSessionId) throw new Error('Workspace session unavailable.');
      return workspaceScope();
    },
    isBusy: () => state.attachingFile || state.generating,
    onSelected(files) {
      if (!$('#file-dialog').open || state.generating) return;
      invalidateInspection();
      state.pendingFiles.push(...files);
      updateSelection();
    },
    onChange: updateSelection,
    onCloseRequested: requestAttachmentDialogClose
  });

  function workspaceFile(path) {
    return { path, reference: { source: 'workspace', path, workspace_revision: state.workspaceRevision, workspace_session_id: state.workspaceSessionId } };
  }

  function workspaceScope() {
    return { conversation_id: state.conversationId ?? undefined, workspace_session_id: state.workspaceSessionId ?? undefined };
  }

  function selectConversation(id, binding, { preserveSelection = false } = {}) {
    state.conversationId = id;
    ++state.workspaceRequest;
    state.workspaceController?.abort();
    state.workspaceController = null;
    if (!preserveSelection) clearFocusFiles();
    applyWorkspace({ root: binding.root, revision: binding.revision, session_id: binding.session_id, available: binding.available, entries_loaded: false });
    state.workspaceReady = Promise.resolve();
  }

  async function startDraft() {
    state.conversationId = null;
    const request = ++state.workspaceRequest;
    state.workspaceController?.abort();
    state.workspaceController = null;
    clearFocusFiles();
    applyWorkspace({ root: null, revision: null, session_id: null, entries_loaded: false });
    state.workspaceReady = (async () => {
      const response = await fetch('/api/workspace', { method: 'POST' });
      if (request !== state.workspaceRequest) return;
      if (!response.ok) throw new Error('Could not start the workspace draft. Open a new chat to try again.');
      const data = await response.json();
      if (request !== state.workspaceRequest) return;
      applyWorkspace(data);
    })();
    try { await state.workspaceReady; }
    catch (error) { if (request === state.workspaceRequest) toast(error.message, 'error'); }
  }

  function adoptConversation(id) {
    state.conversationId = id;
  }

  function sameSelection(first, second) {
    return first.length === second.length && first.every((file, index) => attachmentKey(file) === attachmentKey(second[index]));
  }

  function applyWorkspace(data) {
    const revision = Number.isSafeInteger(data.revision) ? data.revision : null;
    const sessionId = typeof data.session_id === 'string' ? data.session_id : null;
    const changed = state.workspaceRoot !== data.root || state.workspaceRevision !== revision || state.workspaceSessionId !== sessionId;
    state.workspaceRoot = data.root;
    state.workspaceRevision = revision;
    state.workspaceSessionId = sessionId;
    state.workspaceAvailable = Boolean(data.root) && data.available !== false;
    for (const file of [...state.attachments, ...state.pendingFiles]) {
      if (file.reference.source === 'workspace') file.available = state.workspaceAvailable
        && file.reference.workspace_revision === revision && file.reference.workspace_session_id === sessionId;
    }
    document.dispatchEvent(new CustomEvent('workspace-change', { detail: { changed } }));
    if (changed) {
      const hadWorkspaceFiles = state.attachments.some(file => file.reference.source === 'workspace');
      const removed = state.pendingFiles.filter(file => file.reference.source === 'workspace').length;
      state.attachments = state.attachments.filter(file => file.reference.source === 'upload');
      state.pendingFiles = state.pendingFiles.filter(file => file.reference.source === 'upload');
      if (hadWorkspaceFiles) state.selectionEdited = true;
      if ($('#file-dialog').open && removed) {
        $('#attachment-selection-notice').hidden = false;
        $('#attachment-selection-notice').textContent = `Workspace changed. Removed ${removed} workspace file selection${removed === 1 ? '' : 's'}. Attachment copies are kept.`;
      }
      invalidateInspection();
      resetContextPreview();
    }
    const folderName = data.root?.split(/[/\\]/).filter(Boolean).pop() || 'No workspace';
    $('#workspace-folder-name').textContent = folderName;
    $('#workspace-card').title = data.root ? `Manage workspace: ${data.root}` : 'Choose an optional workspace';
    $('#composer-workspace-name').textContent = folderName;
    if (state.inspection && !inspectionIsCurrent()) invalidateInspection();
    renderAttachments();
    updateSelection();
    updateWorkspaceControls();
  }

  async function loadWorkspace() {
    const request = ++state.workspaceRequest;
    state.workspaceController?.abort();
    const controller = new AbortController();
    state.workspaceController = controller;
    updateSelection();
    try {
      await state.workspaceReady;
      if (request !== state.workspaceRequest) return;
      const query = new URLSearchParams({ include_entries: 'false' });
      for (const [key, value] of Object.entries(workspaceScope())) if (value !== undefined) query.set(key, value);
      const response = await fetch(`/api/workspace?${query}`, { signal: controller.signal });
      if (!response.ok) throw new Error('Could not load workspace.');
      const data = await response.json();
      if (request !== state.workspaceRequest) return;
      applyWorkspace(data);
    } catch (error) {
      if (error.name === 'AbortError' || request !== state.workspaceRequest) return;
      $('#workspace-file-count').textContent = 'Folder unavailable';
      $('#composer-workspace-sub').textContent = 'Unavailable';
      invalidateInspection();
      updateSelection();
      toast('Could not load workspace.', 'error');
    } finally {
      if (state.workspaceController === controller) {
        state.workspaceController = null;
        updateSelection();
      }
    }
  }

  function updateSelection() {
    attachmentDrafts.update();
    const busy = state.attachingFile || state.generating || filePicker.isPicking();
    const count = state.pendingFiles.length;
    $('#selected-file-label').textContent = count ? `${count} file${count === 1 ? '' : 's'} selected` : 'No files selected';
    $('#selected-file-label').dataset.hasFile = String(count > 0);
    $('#selected-file-label').title = state.pendingFiles.map(file => `${file.path} (${attachmentSourceLabel(file)})`).join('\n');
    $('#attach-selected-file').disabled = busy || (!count && !state.attachments.length);
    $('#attach-selected-file-label').textContent = state.attachingFile ? 'Checking...' : !count && state.attachments.length ? 'Remove attachments'
      : inspectionIsCurrent() && !state.inspection.data.context_policy.blocked ? 'Attach files' : 'Check files';
    renderPendingAttachments();
    renderInspection();
    filePicker.update();
  }

  function renderPendingAttachments() {
    const list = $('#selected-attachments');
    if (!$('#file-dialog').open) { list.replaceChildren(); return; }
    const focused = document.activeElement?.closest('#selected-attachments [data-attachment-key]')?.dataset.attachmentKey;
    list.replaceChildren();
    $('#attachment-selection-empty').hidden = state.pendingFiles.length > 0;
    list.setAttribute('aria-busy', String(state.attachingFile || filePicker.isPicking()));
    const checked = inspectionIsCurrent() ? new Map(state.inspection.data.files.map(file => [attachmentKey(file), file])) : new Map();
    state.pendingFiles.slice(0, state.visiblePending).forEach(file => {
      const key = attachmentKey(file);
      const metadata = checked.get(key);
      const row = document.createElement('li');
      row.dataset.attachmentKey = key;
      row.dataset.unavailable = String(file.available === false);
      const icon = document.createElement('i');
      icon.dataset.lucide = attachmentIcon(file.path, metadata?.kind || file.kind);
      icon.setAttribute('aria-hidden', 'true');
      const copy = document.createElement('div');
      copy.className = 'attachment-file-copy';
      const name = document.createElement('strong');
      name.textContent = file.path;
      name.title = file.path;
      const detail = document.createElement('small');
      detail.textContent = [attachmentSourceLabel(file), metadata ? attachmentSummary(metadata) : attachmentSize(file.size_bytes)].filter(Boolean).join(' · ');
      copy.append(name, detail);
      const notes = file.available === false ? ['Unavailable. Select the original again or remove this selection.'] : metadata ? attachmentWarnings(metadata) : [];
      notes.forEach(text => {
        const note = document.createElement('p');
        note.className = 'attachment-processing-note';
        note.textContent = text;
        copy.append(note);
      });
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'quiet-btn attachment-remove';
      remove.disabled = state.attachingFile || state.generating || filePicker.isPicking();
      remove.setAttribute('aria-label', `Remove selection: ${file.path}`);
      remove.title = `Remove from this selection: ${file.path}`;
      remove.innerHTML = '<i data-lucide="x" aria-hidden="true"></i>';
      remove.onclick = () => {
        const index = state.pendingFiles.findIndex(item => attachmentKey(item) === key);
        toggleAttachment(file);
        (list.children[Math.min(index, list.children.length - 1)]?.querySelector('button') || $('#choose-attachment-files')).focus({ preventScroll: true });
      };
      row.append(icon, copy, remove);
      list.append(row);
      if (focused === key) remove.focus({ preventScroll: true });
    });
    if (state.pendingFiles.length > state.visiblePending) {
      const row = document.createElement('li');
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'quiet sm-btn';
      more.disabled = state.attachingFile || state.generating || filePicker.isPicking();
      more.textContent = 'Show more selected files';
      more.onclick = () => {
        const firstNew = state.visiblePending;
        state.visiblePending += selectionPageSize;
        renderPendingAttachments();
        list.children[firstNew]?.querySelector('button')?.focus({ preventScroll: true });
      };
      row.append(more);
      list.append(row);
    }
    refreshIcons(list);
  }

  function toggleAttachment(file) {
    if (state.attachingFile || state.generating || filePicker.isPicking()) return;
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
        file.available === false ? file.reference.source === 'workspace' ? 'Workspace changed or unavailable. Select the file again or remove this attachment.'
          : 'Original unavailable. Select the file again or remove this attachment.' : 'Included with each new message until removed.'].filter(Boolean).join('\n');
      chip.dataset.unavailable = String(file.available === false);
      chip.querySelector('.file-chip-name').textContent = file.path;
      chip.querySelector('.file-chip-source').textContent = file.available === false ? 'Unavailable' : attachmentSourceLabel(file);
      const remove = chip.querySelector('button');
      remove.disabled = state.generating;
      const identity = file.reference.source === 'upload' ? ` (${file.reference.attachment_id.slice(0, 8)})` : '';
      remove.title = `Remove ${file.reference.source === 'workspace' ? 'workspace attachment' : 'attachment'}: ${file.path}${identity}`;
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
    filePicker.cancel();
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
      ? { path: reference.path, reference, available: Boolean(state.workspaceRoot) && state.workspaceAvailable
          && reference.workspace_revision === state.workspaceRevision && reference.workspace_session_id === state.workspaceSessionId }
      : { path: `Uploaded file (${reference.attachment_id.slice(0, 8)})`, reference });
    state.pendingFiles = [...state.attachments];
    renderAttachments();
    updateSelection();
    await Promise.all(state.attachments.filter(file => file.reference.source === 'upload').map(async file => {
      try {
        const response = await fetch(`/api/attachments/uploads/${file.reference.attachment_id}`, { signal: controller.signal });
        const data = await response.json();
        if (state.restoreController !== controller || controller.signal.aborted) return;
        if (response.ok) Object.assign(file, { path: data.filename, size_bytes: data.size_bytes, created_at: data.created_at, available: true });
        else if (response.status === 410) file.available = false;
      } catch (error) {
        if (error.name === 'AbortError') return;
      }
      if (state.restoreController === controller) { renderAttachments(); updateSelection(); }
    }));
  }

  async function getAttachmentSelection({ regenerating = false } = {}) {
    await attachmentDrafts.flush();
    if (regenerating && !state.selectionEdited) return {};
    if (state.attachments.some(file => file.available === false)) throw new Error('Some attachments are unavailable. Select them again or remove them before sending.');
    return { attachment_sources: state.attachments.map(file => ({ ...file.reference })) };
  }

  async function openWorkspaceDialog() {
    try { if (!state.workspaceSessionId) await state.workspaceReady; }
    catch { toast('Could not load workspace. Open a new chat to try again.', 'error'); return false; }
    closeMobileSidebar();
    $('#workspace-path-input').value = state.workspaceRoot || '';
    if (!picker.isPicking()) $('#workspace-path-help').textContent = 'Enter a folder path or use Browse to select one on the computer running Helios, then apply.';
    updateWorkspaceControls();
    $('#workspace-dialog').showModal();
    return true;
  }

  async function chooseWorkspace() {
    if (!await openWorkspaceDialog()) return false;
    return new Promise(resolve => {
      $('#workspace-dialog').addEventListener('close', () => resolve(Boolean(state.workspaceRoot)), { once: true });
    });
  }

  async function openFileDialog() {
    ++state.fileDialogVersion;
    filePicker.cancel();
    invalidateInspection();
    closeMobileSidebar();
    state.pendingFiles = [...state.attachments];
    state.visiblePending = selectionPageSize;
    if (!filePicker.isPicking()) {
      $('#attachment-file-status').textContent = '';
      $('#attachment-file-status').hidden = true;
      $('#attachment-file-status').dataset.state = 'ready';
    }
    $('#attachment-selection-notice').hidden = true;
    $('#file-dialog').showModal();
    $('.attachment-body').scrollTop = 0;
    updateSelection();
    $('#choose-attachment-files').focus();
    await loadWorkspace();
  }

  function setGenerating(value) {
    state.generating = value;
    if (value) { invalidateInspection(); filePicker.cancel(); }
    ['load-file', 'workspace-card', 'workspace-context-chip'].forEach((id) => {
      const el = $(`#${id}`);
      if (el) el.disabled = value;
    });
    $('.context-bar').querySelectorAll('.context-chip.file button').forEach((button) => { button.disabled = value; });
    if (value) resetContextPreview();
    updateWorkspaceControls();
    updateSelection();
  }

  function clearContextPreview() {
    state.contextController?.abort();
    state.contextController = null;
    $('#workspace-context-preview').hidden = true;
    $('#workspace-context-preview').textContent = '';
    $('#workspace-context-status').textContent = '';
    $('.workspace-context-preview-body').setAttribute('aria-busy', 'false');
  }

  function resetContextPreview() {
    contextDisclosure?.finish(false);
    clearContextPreview();
  }

  async function getWorkspaceContext() {
    await state.workspaceReady;
    if (state.changingWorkspace) throw new Error('Wait for the Workspace change to finish before sending.');
    if (!state.workspaceSessionId) throw new Error('Could not load workspace. Reopen this conversation or start a new chat.');
    return {
      workspace_revision: state.workspaceRevision ?? undefined,
      workspace_session_id: state.workspaceSessionId ?? undefined
    };
  }

  function updateWorkspaceControls() {
    const busy = state.generating || state.changingWorkspace || picker.isPicking() || !state.workspaceSessionId;
    const connected = Boolean(state.workspaceRoot);
    const iconName = connected ? 'folder' : 'folder-plus';
    ['#workspace-card', '#workspace-context-chip'].forEach(selector => {
      const control = $(selector);
      control.dataset.connected = String(connected);
      const icon = control.querySelector('[data-workspace-icon]');
      if (icon && icon.getAttribute('data-lucide') !== iconName) {
        const replacement = document.createElement('i');
        replacement.setAttribute('data-lucide', iconName);
        replacement.setAttribute('data-workspace-icon', '');
        replacement.setAttribute('aria-hidden', 'true');
        icon.replaceWith(replacement);
        refreshIcons(control);
      }
    });
    $('#workspace-path-input').disabled = busy;
    picker.update();
    $('#apply-workspace').disabled = busy;
    $('#disconnect-workspace').disabled = busy || !connected;
    $('#preview-workspace-context').setAttribute('aria-disabled', String(busy || !connected));
    $('#preview-workspace-context').title = connected ? 'Preview the connected workspace context' : 'Connect a workspace to preview its context';
    $('.workspace-context-preview-body').setAttribute('aria-busy', String(Boolean(state.contextController)));
    $('#composer-workspace-sub').hidden = !connected;
    $('#composer-workspace-sub').textContent = state.workspaceAvailable ? 'Project context' : 'Unavailable';
    $('#workspace-context-chip').title = connected ? 'Project context is included with messages. Open Workspace to preview it or change folders.'
      : 'Chat without a folder, or choose a Workspace to add project context.';
    $('#workspace-file-count').textContent = connected
      ? state.workspaceAvailable ? 'Chat with a connected folder' : 'Folder unavailable · Choose another folder'
      : 'Chat without a folder · Optional';
  }

  async function previewProjectContext() {
    if (state.generating || state.changingWorkspace || picker.isPicking() || !state.workspaceRoot) {
      resetContextPreview();
      return;
    }
    clearContextPreview();
    const revision = state.workspaceRevision;
    const sessionId = state.workspaceSessionId;
    const controller = new AbortController();
    state.contextController = controller;
    $('#workspace-context-status').textContent = 'Preparing project context locally...';
    updateWorkspaceControls();
    try {
      const response = await fetch('/api/workspace/context', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...workspaceScope(), workspace_revision: revision ?? undefined, workspace_session_id: sessionId ?? undefined }), signal: controller.signal
      });
      const data = await response.json();
      if (!response.ok) {
        const feedback = describeRequestError(data, 'Could not preview project context.');
        throw new Error(feedback.message);
      }
      if (state.contextController !== controller || revision !== state.workspaceRevision || sessionId !== state.workspaceSessionId) return;
      $('#workspace-context-preview').textContent = data.context;
      $('#workspace-context-preview').hidden = false;
      $('#workspace-context-status').textContent = 'Local preview of the connected workspace. Refreshed when you send a message.';
    } catch (error) {
      if (error.name !== 'AbortError' && state.contextController === controller) $('#workspace-context-status').textContent = error.message;
    } finally {
      if (state.contextController === controller) {
        state.contextController = null;
        updateWorkspaceControls();
      }
    }
  }

  async function changeWorkspace(path) {
    if (state.generating || state.changingWorkspace || picker.isPicking()) return;
    state.changingWorkspace = true;
    const request = ++state.workspaceRequest;
    resetContextPreview();
    invalidateInspection();
    updateWorkspaceControls();
    try {
      const response = await fetch('/api/workspace/root', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, ...workspaceScope(), workspace_revision: state.workspaceRevision ?? undefined, include_entries: false })
      });
      const data = await response.json();
      if (request !== state.workspaceRequest) return;
      if (!response.ok) {
        const feedback = describeRequestError(data, 'Could not change the workspace.');
        throw new Error(feedback.message);
      }
      applyWorkspace(data);
      onWorkspaceSaved?.(state.conversationId, data.conversation_version);
      $('#workspace-dialog').close();
      toast(path === null ? 'Workspace disconnected.' : 'Workspace folder updated.', 'success');
    } catch (error) {
      toast(error.message || 'Could not change the workspace.', 'error');
    } finally {
      state.changingWorkspace = false;
      updateWorkspaceControls();
      updateSelection();
    }
  }

  function beforeAttachmentDialogClose() {
    if (filePicker.isPicking()) {
      filePicker.cancel({ close: true });
      return false;
    }
    if (sameSelection(state.pendingFiles, state.attachments)) return true;
    return showActionDialog({ title: 'Discard attachment changes?', description: 'Your unapplied file selection will be lost. Previously attached files will stay unchanged.',
      confirmLabel: 'Discard changes', cancelLabel: 'Keep editing', danger: true });
  }

  async function requestAttachmentDialogClose() {
    if (await beforeAttachmentDialogClose()) $('#file-dialog').close();
  }

  function beforeDialogClose() {
    if (state.changingWorkspace) {
      toast('Wait for the Workspace change to finish before closing.', 'info');
      return false;
    }
    if (picker.isPicking()) {
      picker.cancel({ close: true });
      return false;
    }
    if ($('#workspace-path-input').value.trim() === (state.workspaceRoot || '')) return true;
    return showActionDialog({ title: 'Discard Workspace changes?', description: 'Your unapplied folder path will be lost. The workspace selection for this conversation will stay unchanged.',
      confirmLabel: 'Discard changes', cancelLabel: 'Keep editing', danger: true });
  }

  async function requestWorkspaceDialogClose() {
    if (await beforeDialogClose()) $('#workspace-dialog').close();
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
    region.dataset.state = state.inspectionFeedback?.type || (state.inspection ? 'ready' : 'idle');
    if (state.inspectionFeedback) {
      const { message, details = [] } = state.inspectionFeedback;
      status.textContent = [message, ...details].join('\n');
      return;
    }
    if (!state.inspection) {
      status.textContent = state.pendingFiles.length ? 'Use Check files to review delivery details before attaching.'
        : 'Add files to review their delivery and local preparation.';
      return;
    }
    const { context_policy: policy } = state.inspection.data;
    region.dataset.state = policy.blocked ? 'error' : policy.warning ? 'warning' : 'ready';
    const text = [policy.blocked ? 'The estimated attachment input exceeds the input budget. Remove files or change the context policy before attaching.'
      : policy.warning ? 'The estimated attachment input exceeds the input budget. Warning mode allows you to attach these files.'
      : 'Files are ready to attach.'];
    if (Number.isSafeInteger(policy.estimated_input_tokens)) text.push(`Approximate attachment input: ${policy.estimated_input_tokens.toLocaleString()} tokens.`);
    if (policy.input_budget !== null) text.push(`Input budget: ${policy.input_budget.toLocaleString()} tokens.`);
    if (policy.legacy_input_policy) text.push('Previous input policy: review the budget in Settings.');
    if (Number.isSafeInteger(policy.declared_model_input_limit)) text.push(`Declared model input limit: ${policy.declared_model_input_limit.toLocaleString()} tokens.`);
    if (policy.mode === 'off') text.push('Context token estimation is off.');
    status.textContent = text.join('\n');
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
    if (state.attachingFile || state.generating || (!files.length && !state.attachments.length) || filePicker.isPicking()) return;
    if (!files.length || (inspectionIsCurrent() && !state.inspection.data.context_policy.blocked)) {
      state.attachments = files.length ? state.inspection.data.files.map((file, index) => ({ ...files[index], ...file })) : [];
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
        body: JSON.stringify({ attachment_sources: files.map(file => file.reference), profile_id: profileId, ...workspaceScope(), workspace_revision: revision ?? undefined, workspace_session_id: sessionId ?? undefined }), signal: controller.signal
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
        if (document.activeElement === document.body && $('#file-dialog').open) $('#attach-selected-file').focus({ preventScroll: true });
      }
    }
  }

  function init() {
    attachmentDrafts.init();
    filePicker.init();
    $('#workspace-card').onclick = openWorkspaceDialog;
    $('#workspace-context-chip').onclick = openWorkspaceDialog;
    $('#disconnect-workspace').onclick = () => changeWorkspace(null);

    const preview = $('#workspace-context-disclosure');
    $('#preview-workspace-context').addEventListener('click', event => {
      if ($('#preview-workspace-context').getAttribute('aria-disabled') !== 'true' || preview.open) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    }, { capture: true });
    contextDisclosure = createDisclosure(preview, $('.workspace-context-preview-body'));
    preview.addEventListener('toggle', () => {
      if (preview.open) previewProjectContext();
      else clearContextPreview();
    });
    $('#workspace-dialog').addEventListener('close', () => { if (!$('#workspace-dialog').open) resetContextPreview(); });

    $('#attach-selected-file').onclick = checkOrApplySelection;
    $('#file-dialog').addEventListener('close', () => {
      // Ignore a queued close event after the dialog has already reopened.
      if ($('#file-dialog').open) return;
      ++state.fileDialogVersion;
      filePicker.cancel();
      invalidateInspection();
      state.pendingFiles = [...state.attachments];
      updateSelection();
    });

    $('#workspace-form').onsubmit = async event => {
      event.preventDefault();
      await changeWorkspace($('#workspace-path-input').value.trim());
    };
    $('#load-file').onclick = openFileDialog;
    picker.init();
  }

  return {
    init, load: loadWorkspace, startDraft, selectConversation, adoptConversation, clearFocusFiles, setGenerating, beforeDialogClose, beforeAttachmentDialogClose, chooseWorkspace,
    restorePicker: picker.restore, getAttachmentSelection, restoreAttachments, getWorkspaceContext, updateAttachmentMetadata,
    getWorkspaceInfo() { return { root: state.workspaceRoot, revision: state.workspaceRevision, sessionId: state.workspaceSessionId }; },
    markAttachmentsSaved() { state.selectionEdited = false; }
  };
}
