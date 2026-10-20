/* Coordinate host file selection, preparation, cancellation, and reconnects for the attachment dialog. */

import { $ } from './ui.js';

export function createFilePicker({ getWorkspaceScope, isBusy, onSelected, onChange, onCloseRequested }) {
  const storageKey = 'helios.attachmentPicker';
  let current = null;
  let leaving = false;

  function savedSession() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey));
      return saved && typeof saved.token === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(saved.token)
        && saved.scope && typeof saved.scope === 'object' && !Array.isArray(saved.scope) ? saved : null;
    } catch { return null; }
  }

  function remember(picker) {
    try { sessionStorage.setItem(storageKey, JSON.stringify({ token: picker.token, scope: picker.scope, cancel: picker.cancelRequested })); }
    catch { /* Selection still works when browser session storage is unavailable. */ }
  }

  function update() {
    const picking = Boolean(current);
    $('#choose-attachment-files').disabled = picking ? Boolean(current.cancelRequested && !current.cancelFailed) : isBusy();
    $('#attachment-picker-add-icon').toggleAttribute('hidden', picking);
    $('#attachment-picker-stop-icon').toggleAttribute('hidden', !picking);
    $('#attachment-picker-label').textContent = picking ? 'Stop' : 'Add files';
    $('#attachment-file-status').setAttribute('aria-busy', String(Boolean(current)));
  }

  function finish(picker, result) {
    if (current !== picker) return;
    current = null;
    clearTimeout(picker.retry);
    clearTimeout(picker.timer);
    if (result.status !== 'disconnected') {
      try { sessionStorage.removeItem(storageKey); } catch { /* No persisted session needs clearing. */ }
    }
    picker.socket?.close();
    const status = $('#attachment-file-status');
    status.hidden = false;
    status.dataset.state = ['error', 'disconnected', 'missing'].includes(result.status) ? 'error' : 'ready';
    if (result.status === 'selected' && !picker.cancelRequested && $('#file-dialog').open) {
      if (!Array.isArray(result.files) || !result.files.length || result.files.some(file => file.reference?.source !== 'upload'
          || typeof file.reference.attachment_id !== 'string' || typeof file.filename !== 'string')) {
        status.textContent = 'Could not read the file selection. Use Add files to try again.';
        status.dataset.state = 'error';
      } else {
        onSelected(result.files.map(file => ({ ...file.inspection, path: file.filename, reference: file.reference, size_bytes: file.size_bytes, available: true })));
        status.textContent = `${result.files.length} file${result.files.length === 1 ? '' : 's'} added to this selection.`;
      }
    } else if (result.status === 'cancelled' || (result.status === 'missing' && picker.cancelRequested)) {
      status.textContent = 'File selection cancelled. Your previous selection is kept.';
    } else {
      status.textContent = result.detail || 'The previous file picker has closed. Use Add files to select files again.';
    }
    onChange();
    update();
    if (picker.closeOnCancel && result.status !== 'disconnected') onCloseRequested();
    else if (picker.restart && ['cancelled', 'missing'].includes(result.status)) begin();
    else if ($('#file-dialog').open) $('#choose-attachment-files').focus({ preventScroll: true });
  }

  function waitForConnection(picker) {
    if (picker.timer) return;
    picker.timer = setTimeout(() => {
      if (current !== picker) return;
      picker.cancelRequested = true;
      remember(picker);
      finish(picker, { status: 'disconnected', detail: 'Connection lost. Use Add files to check and close the previous picker.' });
    }, 10000);
  }

  function connect(picker, start = false) {
    if (current !== picker || leaving) return;
    waitForConnection(picker);
    const url = new URL(`/api/attachments/picker/${picker.token}`, location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(url);
    picker.socket = socket;
    socket.onopen = () => {
      if (current !== picker || leaving || picker.socket !== socket) { socket.close(); return; }
      socket.send(JSON.stringify({ action: start ? 'start' : 'resume', ...picker.scope }));
      if (picker.cancelRequested) socket.send(JSON.stringify({ action: 'cancel' }));
    };
    socket.onmessage = event => {
      if (current !== picker || leaving || picker.socket !== socket) return;
      let result;
      try { result = JSON.parse(event.data); }
      catch { return; }
      const status = $('#attachment-file-status');
      if (result.status === 'pending' || result.status === 'preparing') {
        clearTimeout(picker.timer);
        picker.timer = null;
        status.textContent = picker.cancelRequested ? 'Stopping file selection…'
          : result.status === 'preparing' ? 'Preparing the selected files locally…' : 'Choose files in the picker on the computer running Helios.';
      } else if (result.status === 'reconnecting') {
        status.textContent = 'Waiting for the previous file picker connection to close…';
      } else if (result.status === 'cancel-error') {
        picker.cancelFailed = true;
        remember(picker);
        status.textContent = result.detail || 'Could not stop file selection. Try Stop again.';
        update();
      } else if (picker.cancelRequested && !['cancelled', 'missing'].includes(result.status)) {
        socket.send(JSON.stringify({ action: 'cancel' }));
      } else finish(picker, result);
    };
    socket.onclose = () => {
      if (current !== picker || leaving || picker.socket !== socket) return;
      waitForConnection(picker);
      $('#attachment-file-status').textContent = 'Reconnecting to file picker…';
      picker.retry = setTimeout(() => connect(picker), 1000);
    };
  }

  async function begin({ recovering = false } = {}) {
    if (current || isBusy()) return;
    const saved = savedSession();
    const picker = { token: saved?.token || crypto.randomUUID(), scope: saved?.scope || {}, socket: null,
      cancelRequested: Boolean(saved), closeOnCancel: false, restart: Boolean(saved) && !recovering, retry: null, timer: null };
    current = picker;
    $('#attachment-file-status').hidden = false;
    $('#attachment-file-status').dataset.state = 'busy';
    $('#attachment-file-status').textContent = saved ? 'Closing the previous file picker…' : 'Opening file picker on the computer running Helios…';
    onChange();
    update();
    try {
      if (!saved) picker.scope = await getWorkspaceScope();
      if (current !== picker) return;
      if (picker.cancelRequested && !saved) { finish(picker, { status: 'cancelled' }); return; }
      remember(picker);
      connect(picker, !saved);
    } catch {
      finish(picker, { status: 'error', detail: 'Could not start file selection. Reopen this conversation or start a new chat.' });
    }
  }

  function cancel({ close = false } = {}) {
    const picker = current;
    if (!picker) return;
    picker.cancelRequested = true;
    picker.cancelFailed = false;
    picker.closeOnCancel ||= close;
    picker.restart = false;
    remember(picker);
    $('#attachment-file-status').textContent = 'Stopping file selection…';
    if (picker.socket?.readyState === WebSocket.OPEN) picker.socket.send(JSON.stringify({ action: 'cancel' }));
    waitForConnection(picker);
    update();
  }

  function init() {
    $('#choose-attachment-files').onclick = () => current ? cancel() : begin();
    window.addEventListener('pagehide', () => {
      leaving = true;
      if (!current) return;
      clearTimeout(current.retry);
      clearTimeout(current.timer);
      current.timer = null;
      current.socket?.close();
    });
    window.addEventListener('pageshow', () => {
      if (!leaving) return;
      leaving = false;
      if (current) connect(current);
    });
    if (savedSession()) begin({ recovering: true });
  }

  return { init, update, cancel, isPicking: () => current !== null };
}
