/* Own the workspace tree, selected attachments, and workspace dialogs, coordinating the folder picker. */

import { $, closeMobileSidebar, refreshIcons, toast } from './ui.js';
import { createWorkspacePicker } from './workspace-picker.js';
import { attachmentIcon, attachmentSummary, attachmentWarnings } from './attachments.js';
import { describeRequestError } from './chat-feedback.js';

export function createWorkspace({ getActiveProfileId }) {
  const state = {
    workspaceFiles: [],
    workspaceRoot: '',
    workspaceEntries: [],
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
      empty.textContent = query ? 'No matching files. Try another name or path.' : 'No files in this folder. Choose another folder in Workspace settings.';
      tree.append(empty);
    }
    refreshIcons();
  }

  function countFiles(entries) {
    return entries.reduce((count, entry) => count + (entry.type === 'file' ? 1 : countFiles(entry.children || [])), 0);
  }

  function applyWorkspace(data) {
    if (state.workspaceRoot && state.workspaceRoot !== data.root) clearFocusFiles();
    state.workspaceRoot = data.root;
    state.workspaceEntries = data.entries;
    const folderName = data.root.split(/[/\\]/).filter(Boolean).pop() || data.root;
    const fileCount = countFiles(data.entries);
    $('#workspace-folder-name').textContent = folderName;
    $('#workspace-file-count').textContent = `${fileCount} file${fileCount === 1 ? '' : 's'} · Folder context on`;
    $('#workspace-card').title = `Manage workspace: ${data.root}`;
    $('#composer-workspace-name').textContent = folderName;
    $('#composer-workspace-sub').textContent = 'Folder context';
    $('#workspace-context-chip').title = `Folder paths included: ${data.root}`;
    $('#file-source-name').textContent = folderName;
    $('#file-source-name').title = data.root;
    const paths = new Set();
    const collect = (entries) => entries.forEach((entry) => {
      if (entry.type === 'file') paths.add(entry.path);
      if (entry.children) collect(entry.children);
    });
    collect(data.entries);
    state.workspaceFiles = state.workspaceFiles.filter((path) => paths.has(path));
    state.pendingFiles = state.pendingFiles.filter((path) => paths.has(path));
    for (const path of state.attachmentMetadata.keys()) {
      if (!state.workspaceFiles.includes(path)) state.attachmentMetadata.delete(path);
    }
    if (state.inspection && !inspectionIsCurrent()) invalidateInspection();
    renderAttachments();
    updateSelection();
    renderFileList();
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
    $('#refresh-workspace').disabled = state.attachingFile || state.generating;
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
    $('#workspace-path-input').value = state.workspaceRoot;
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
    updateSelection();
  }

  function beforeDialogClose() {
    if (!picker.isPicking()) return true;
    picker.cancel();
    return false;
  }

  function inspectionIsCurrent() {
    const inspection = state.inspection;
    return Boolean(inspection && inspection.root === state.workspaceRoot && inspection.profileId === getActiveProfileId()
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
    const text = [policy.blocked ? 'The approximate attachment input exceeds the selected budget or declared model limit in blocking mode. Reduce the selection or change the context policy before applying.'
      : policy.warning ? 'The approximate attachment input exceeds the selected budget or declared model limit. Warning mode allows you to apply this selection.'
      : 'File checks passed locally. Review the delivery method, then apply this selection.'];
    if (Number.isSafeInteger(policy.estimated_input_tokens)) text.push(`Approximate attachment input: ${policy.estimated_input_tokens.toLocaleString()} tokens. Selected input budget: ${policy.input_budget.toLocaleString()} tokens.`);
    if (Number.isSafeInteger(policy.declared_model_input_limit)) text.push(`Declared model input limit: ${policy.declared_model_input_limit.toLocaleString()} tokens.`);
    if (policy.mode === 'off') text.push('Context token estimation is off.');
    text.push('This check covers attachments only. The question, workspace paths, and conversation history are checked when you send a message.');
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
    const version = state.fileDialogVersion;
    const profileId = getActiveProfileId();
    const controller = new AbortController();
    const isCurrent = () => state.inspectionController === controller && $('#file-dialog').open && !state.generating
      && version === state.fileDialogVersion && root === state.workspaceRoot && profileId === getActiveProfileId()
      && paths.length === state.pendingFiles.length && paths.every((path, index) => path === state.pendingFiles[index]);
    state.inspectionController = controller;
    state.attachingFile = true;
    state.inspectionFeedback = { type: 'checking', message: 'Checking selected files locally...' };
    renderInspection();
    updateSelection();
    try {
      const response = await fetch('/api/attachments/inspect', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths, profile_id: profileId }), signal: controller.signal
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
      state.inspection = { paths, root, version, profileId, data };
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
      if (picker.isPicking()) return;
      const button = $('#apply-workspace');
      button.disabled = true;
      try {
        const response = await fetch('/api/workspace/root', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: $('#workspace-path-input').value.trim() })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Could not open workspace folder.');
        ++state.workspaceRequest;
        clearFocusFiles();
        applyWorkspace(data);
        $('#workspace-dialog').close();
        toast('Workspace folder updated.', 'success');
      } catch (error) {
        toast(error.message || 'Could not open workspace folder.', 'error');
      } finally {
        button.disabled = false;
      }
    };

    $('#load-file').onclick = openFileDialog;

    picker.init();
  }

  return {
    init, load: loadWorkspace, clearFocusFiles, setGenerating, beforeDialogClose,
    restorePicker: picker.restore, getAttachmentFiles: () => [...state.workspaceFiles], updateAttachmentMetadata
  };
}
