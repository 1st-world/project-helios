/* Manage browser uploads and reusable originals inside the shared attachment dialog. */

import { $, refreshIcons } from './ui.js';
import { attachmentIcon, attachmentKey } from './attachments.js';
import { describeRequestError } from './chat-feedback.js';
import { showActionDialog } from './dialogs.js';

export function createUploads({ isBusy, getPending, onToggle, onUploaded, onDeleted, onInventory, onChange }) {
  const state = { files: [], controller: null, listController: null, busy: false, visible: 200 };

  async function readResult(response, fallback) {
    let data;
    try { data = await response.json(); }
    catch { throw new Error(`${fallback} Could not read the response. (HTTP ${response.status})`); }
    if (!response.ok) {
      const feedback = describeRequestError(data, fallback, `HTTP ${response.status}`);
      throw new Error([feedback.message, ...(feedback.details || [])].join('\n'));
    }
    return data;
  }

  function record(data) {
    return { ...data.inspection, path: data.filename, reference: data.reference, available: data.available !== false, size_bytes: data.size_bytes };
  }

  function update() {
    const busy = isBusy() || state.busy;
    $('#choose-upload-files').disabled = busy;
    $('#refresh-uploads').disabled = busy || Boolean(state.listController);
    $('#cancel-upload').hidden = !state.controller;
    $('#upload-file-input').disabled = busy;
    const list = $('#uploaded-files');
    if (!$('#file-dialog').open) { list.replaceChildren(); return; }
    const focused = document.activeElement;
    const focusKey = focused?.closest('[data-attachment-key]')?.dataset.attachmentKey;
    const deleteFocused = focused?.dataset.deleteUpload !== undefined;
    list.replaceChildren();
    const files = new Map(state.files.map(file => [attachmentKey(file), file]));
    getPending().filter(file => file.reference.source === 'upload').forEach(file => {
      if (!files.has(attachmentKey(file))) files.set(attachmentKey(file), file);
    });
    const selected = new Set(getPending().map(attachmentKey));
    for (const file of [...files.values()].slice(0, state.visible)) {
      const row = document.createElement('li');
      row.dataset.attachmentKey = attachmentKey(file);
      const choose = document.createElement('button');
      choose.type = 'button';
      choose.className = 'upload-selection';
      choose.disabled = busy || (file.available === false && !selected.has(attachmentKey(file)));
      choose.setAttribute('aria-pressed', String(selected.has(attachmentKey(file))));
      choose.innerHTML = `<i data-lucide="${attachmentIcon(file.path, file.kind)}"></i>`;
      const name = document.createElement('span');
      name.textContent = file.path;
      choose.append(name);
      choose.title = `${file.path}\nUpload ID: ${file.reference.attachment_id}`;
      choose.onclick = () => onToggle(file);
      const size = document.createElement('small');
      size.textContent = file.available === false ? 'Unavailable · select the original again'
        : `${(file.size_bytes || 0).toLocaleString()} bytes · ${file.reference.attachment_id.slice(0, 8)}`;
      choose.append(size);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'quiet upload-delete';
      remove.dataset.deleteUpload = '';
      remove.disabled = busy;
      remove.title = `Delete stored upload: ${file.path} (${file.reference.attachment_id.slice(0, 8)})`;
      remove.setAttribute('aria-label', remove.title);
      remove.innerHTML = '<i data-lucide="trash-2"></i>';
      remove.onclick = () => deleteUpload(file);
      row.append(choose, remove);
      list.append(row);
      if (focusKey === row.dataset.attachmentKey) (deleteFocused ? remove : choose).focus({ preventScroll: true });
    }
    if (files.size > state.visible) {
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'quiet sm-btn';
      more.textContent = 'Show more uploads';
      more.disabled = busy;
      more.onclick = () => {
        const firstNew = state.visible;
        state.visible += 200;
        update();
        list.children[firstNew]?.querySelector('button:not(:disabled)')?.focus();
      };
      const row = document.createElement('li');
      row.append(more);
      list.append(row);
    }
    refreshIcons(list);
  }

  async function load() {
    state.listController?.abort();
    const controller = new AbortController();
    state.listController = controller;
    $('#upload-status').textContent = 'Loading reusable uploads...';
    update();
    try {
      const response = await fetch('/api/attachments/uploads', { signal: controller.signal });
      const data = await readResult(response, 'Could not load uploaded files.');
      if (state.listController !== controller || !$('#file-dialog').open) return;
      if (!Array.isArray(data.uploads)) throw new Error('Could not read the upload list. Use Refresh to try again.');
      state.files = data.uploads.map(record);
      onInventory(state.files);
      $('#upload-status').textContent = state.files.length ? 'Select a stored original to reuse it.' : 'Choose files from your device. No workspace is needed.';
    } catch (error) {
      if (error.name !== 'AbortError' && state.listController === controller) $('#upload-status').textContent = error.message;
    } finally {
      if (state.listController === controller) {
        state.listController = null;
        update();
      }
    }
  }

  async function uploadFiles(files) {
    if (!files.length || isBusy() || state.busy) return;
    state.listController?.abort();
    state.listController = null;
    const controller = new AbortController();
    state.controller = controller;
    state.busy = true;
    onChange();
    update();
    let completed = 0;
    try {
      for (const file of files) {
        $('#upload-status').textContent = `Uploading and checking ${file.name} (${completed + 1}/${files.length})...`;
        const response = await fetch(`/api/attachments/uploads?filename=${encodeURIComponent(file.name)}`, {
          method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file, signal: controller.signal
        });
        const data = await readResult(response, `Could not upload ${file.name}.`);
        if (state.controller !== controller || controller.signal.aborted || !$('#file-dialog').open) return;
        if (data.reference?.source !== 'upload' || !data.reference.attachment_id || typeof data.filename !== 'string') throw new Error('Could not read the upload result. Refresh the uploaded files before trying again.');
        const uploaded = record(data);
        state.files.unshift(uploaded);
        onUploaded(uploaded);
        completed++;
        update();
      }
      $('#upload-status').textContent = `${completed} file${completed === 1 ? '' : 's'} uploaded. Check the combined selection before applying.`;
    } catch (error) {
      if (state.controller === controller) $('#upload-status').textContent = error.name === 'AbortError'
        ? 'Upload stopped. Completed files remain available.' : `${error.message}${completed ? ' Completed files remain available.' : ''}`;
    } finally {
      if (state.controller === controller) {
        state.controller = null;
        state.busy = false;
        onChange();
        update();
        $('#choose-upload-files').focus({ preventScroll: true });
      }
    }
  }

  async function deleteUpload(file) {
    if (isBusy() || state.busy) return;
    const deleted = await showActionDialog({ title: 'Delete stored upload?',
      description: `Permanently delete “${file.path}”? Conversations using this original will require the file to be selected again. Removing a selection alone keeps the original.`,
      confirmLabel: 'Delete original', danger: true,
      async onConfirm() {
        state.listController?.abort();
        state.listController = null;
        const response = await fetch(`/api/attachments/uploads/${file.reference.attachment_id}`, { method: 'DELETE' });
        await readResult(response, 'Could not delete this original.');
        state.files = state.files.filter(item => attachmentKey(item) !== attachmentKey(file));
        onDeleted(file);
        $('#upload-status').textContent = `Deleted ${file.path}.`;
        update();
      }
    });
    if (deleted && $('#file-dialog').open) ($('#uploaded-files .upload-selection') || $('#choose-upload-files')).focus();
  }

  function cancel() {
    state.controller?.abort();
    state.listController?.abort();
    state.controller = state.listController = null;
    state.busy = false;
    $('#upload-file-input').value = '';
  }

  function init() {
    $('#choose-upload-files').onclick = () => $('#upload-file-input').click();
    $('#upload-file-input').onchange = event => {
      const files = [...event.target.files];
      event.target.value = '';
      uploadFiles(files);
    };
    $('#cancel-upload').onclick = () => state.controller?.abort();
    $('#refresh-uploads').onclick = load;
  }

  return { init, load, update, cancel, isUploading: () => state.busy };
}
