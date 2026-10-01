/* Own the workspace tree, selected attachments, and workspace dialogs, coordinating the folder picker. */

import { $, closeMobileSidebar, refreshIcons, toast } from './ui.js';
import { createWorkspacePicker } from './workspace-picker.js';

export function createWorkspace() {
  const state = {
    workspaceFiles: [],
    workspaceRoot: '',
    workspaceEntries: [],
    pendingFiles: [],
    attachingFile: false,
    generating: false,
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
        row.setAttribute('aria-pressed', String(state.pendingFiles.includes(entry.path)));
        row.classList.toggle('selected', state.pendingFiles.includes(entry.path));
        row.disabled = state.attachingFile || state.generating;
        row.innerHTML = '<i data-lucide="file-text"></i>';
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
    $('#refresh-workspace').disabled = state.attachingFile || state.generating;
  }

  function toggleFile(path) {
    if (state.attachingFile || state.generating) return;
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

  function init() {
    $('#workspace-card').onclick = openWorkspaceDialog;
    $('#workspace-context-chip').onclick = openWorkspaceDialog;
    $('#file-search').oninput = renderFileList;
    $('#attach-selected-file').onclick = async () => {
      const paths = [...state.pendingFiles];
      const root = state.workspaceRoot;
      const dialogVersion = state.fileDialogVersion;
      if (state.attachingFile || state.generating || (!paths.length && !state.workspaceFiles.length)) return;
      state.attachingFile = true;
      updateSelection();
      const button = $('#attach-selected-file');
      button.disabled = true;
      button.textContent = 'Checking...';
      try {
        for (const path of paths) {
          const response = await fetch('/api/read-file', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path })
          });
          if (!response.ok) {
            const error = await response.json();
            throw new Error(`${path}: ${error.detail || 'Could not attach this file.'}`);
          }
          if (!$('#file-dialog').open || dialogVersion !== state.fileDialogVersion || state.workspaceRoot !== root) return;
        }
        if (!$('#file-dialog').open || dialogVersion !== state.fileDialogVersion || state.workspaceRoot !== root
            || paths.length !== state.pendingFiles.length || paths.some((path, index) => path !== state.pendingFiles[index])) return;
        state.workspaceFiles = paths;
        renderAttachments();
        $('#file-dialog').close();
        $('#prompt').focus();
      } catch (error) {
        if ($('#file-dialog').open && dialogVersion === state.fileDialogVersion) {
          toast(error.message || 'Could not attach this file.', 'error');
        }
      } finally {
        state.attachingFile = false;
        button.textContent = 'Apply selection';
        updateSelection();
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
    restorePicker: picker.restore, getWorkspaceFiles: () => [...state.workspaceFiles]
  };
}
