/* Own the workspace tree, selected attachment, and workspace dialogs, coordinating the folder picker. */

import { $, closeMobileSidebar, refreshIcons, toast } from './ui.js';
import { createWorkspacePicker } from './workspace-picker.js';

export function createWorkspace() {
  const state = {
    workspaceFile: null,
    workspaceRoot: '',
    workspaceEntries: [],
    pendingFile: null,
    attachingFile: false,
    fileDialogVersion: 0,
    workspaceRequest: 0
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
        row.setAttribute('aria-pressed', String(entry.path === state.pendingFile));
        row.classList.toggle('selected', entry.path === state.pendingFile);
        row.innerHTML = '<i data-lucide="file-text"></i>';
        const name = document.createElement('span');
        name.textContent = query ? entry.path : entry.name;
        row.append(name);
        row.onclick = () => selectFile(entry.path);
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
    if (state.workspaceRoot && state.workspaceRoot !== data.root) clearFocusFile();
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
    if (state.workspaceFile && !paths.has(state.workspaceFile)) clearFocusFile();
    if (state.pendingFile && !paths.has(state.pendingFile)) state.pendingFile = null;
    selectFile(state.pendingFile);
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
      selectFile(null);
      toast('Could not load workspace.', 'error');
    }
  }

  function selectFile(path) {
    state.pendingFile = path;
    $('#workspace-tree').querySelectorAll('[data-path]').forEach((row) => {
      const selected = row.dataset.path === path;
      row.classList.toggle('selected', selected);
      row.setAttribute('aria-pressed', String(selected));
    });
    $('#selected-file-label').textContent = path || 'No file selected';
    $('#selected-file-label').dataset.hasFile = String(Boolean(path));
    $('#selected-file-label').title = path || '';
    $('#attach-selected-file').disabled = !path || state.attachingFile;
  }

  function clearFocusFile() {
    state.workspaceFile = null;
    $('#file-chip').classList.add('hidden');
  }

  function openWorkspaceDialog() {
    closeMobileSidebar();
    $('#workspace-path-input').value = state.workspaceRoot;
    $('#workspace-dialog').showModal();
  }

  async function openFileDialog() {
    ++state.fileDialogVersion;
    closeMobileSidebar();
    state.pendingFile = state.workspaceFile;
    $('#file-search').value = '';
    selectFile(state.pendingFile);
    renderFileList();
    $('#file-dialog').showModal();
    $('#file-search').focus();
    await loadWorkspace();
  }

  function setGenerating(value) {
    ['load-file', 'workspace-card', 'workspace-context-chip', 'remove-focus-file'].forEach((id) => {
      const el = $(`#${id}`);
      if (el) el.disabled = value;
    });
  }

  function beforeDialogClose() {
    if (!picker.isPicking()) return true;
    picker.cancel();
    return false;
  }

  function init() {
    $('#workspace-card').onclick = openWorkspaceDialog;
    $('#workspace-context-chip').onclick = openWorkspaceDialog;
    $('#file-search').oninput = renderFileList;
    $('#attach-selected-file').onclick = async () => {
      const path = state.pendingFile;
      const root = state.workspaceRoot;
      const dialogVersion = state.fileDialogVersion;
      if (!path || state.attachingFile) return;
      state.attachingFile = true;
      const button = $('#attach-selected-file');
      button.disabled = true;
      button.textContent = 'Checking...';
      try {
        const response = await fetch('/api/read-file', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path })
        });
        if (!response.ok) {
          const error = await response.json();
          throw new Error(error.detail || 'Could not attach this file.');
        }
        if (!$('#file-dialog').open || dialogVersion !== state.fileDialogVersion || state.pendingFile !== path || state.workspaceRoot !== root) return;
        state.workspaceFile = path;
        $('#file-chip .file-chip-name').textContent = path;
        $('#file-chip .file-chip-name').title = path;
        $('#file-chip').classList.remove('hidden');
        $('#file-dialog').close();
        $('#prompt').focus();
      } catch (error) {
        if ($('#file-dialog').open && dialogVersion === state.fileDialogVersion) {
          toast(error.message || 'Could not attach this file.', 'error');
        }
      } finally {
        state.attachingFile = false;
        button.textContent = 'Attach file';
        button.disabled = !state.pendingFile;
      }
    };

    $('#refresh-workspace').onclick = loadWorkspace;

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
        clearFocusFile();
        state.pendingFile = null;
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

    const removeFocusBtn = $('#remove-focus-file');
    if (removeFocusBtn) {
      removeFocusBtn.onclick = (event) => {
        event.stopPropagation();
        clearFocusFile();
      };
    }

    picker.init();
  }

  return {
    init, load: loadWorkspace, clearFocusFile, setGenerating, beforeDialogClose,
    restorePicker: picker.restore, getWorkspaceFile: () => state.workspaceFile
  };
}
