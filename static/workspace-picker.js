/* Manage the native folder picker connection, cancellation, and browser session restoration. */

import { $, toast } from './ui.js';

export function createWorkspacePicker({ openWorkspaceDialog, getWorkspaceRoot }) {
  const pickerStorageKey = 'helios.workspacePicker';
  let workspacePicker = null;
  let pickerPageLeaving = false;

  function disconnectWorkspacePicker(picker) {
    if (!picker || workspacePicker !== picker) return;
    picker.cancelRequested = true;
    picker.restartAfterCancel = false;
    sessionStorage.setItem(pickerStorageKey + '.cancel', picker.token);
    finishWorkspacePicker(picker, { status: 'disconnected' });
    toast('Connection lost. The picker could not be confirmed closed. Its status will be checked before the next Browse.', 'warning');
  }

  function waitForPickerConnection(picker) {
    if (!picker.connectionTimer) {
      // Bound connection recovery only; an active folder selection has no timeout.
      picker.connectionTimer = setTimeout(() => disconnectWorkspacePicker(picker), 10000);
    }
  }

  function finishWorkspacePicker(picker, result) {
    if (workspacePicker !== picker) return;
    workspacePicker = null;
    clearTimeout(picker.retry);
    clearTimeout(picker.connectionTimer);
    clearTimeout(picker.cancelTimer);
    if (result.status !== 'disconnected') {
      sessionStorage.removeItem(pickerStorageKey);
      sessionStorage.removeItem(pickerStorageKey + '.cancel');
      sessionStorage.removeItem(pickerStorageKey + '.path');
    }
    picker.socket?.close();
    picker.controls.forEach((control, index) => { control.disabled = picker.disabled[index]; });
    $('#workspace-path-help').textContent = picker.help;
    if (picker.closeOnCancel) {
      $('#workspace-dialog').close();
    } else if (result.status === 'selected' && $('#workspace-dialog').open) {
      $('#workspace-path-input').value = result.path;
    } else if (result.status === 'error' || result.status === 'missing') {
      toast(result.detail || 'The previous folder picker has closed. Browse again to choose a folder.', 'warning');
    }
    if ($('#workspace-dialog').open) $('#workspace-path-input').focus();
    if (picker.restartAfterCancel && ['cancelled', 'missing'].includes(result.status)) beginWorkspacePicker();
  }

  function connectWorkspacePicker(picker, start = false) {
    if (workspacePicker !== picker || pickerPageLeaving) return;
    waitForPickerConnection(picker);
    const url = new URL('/api/workspace/picker/' + picker.token, location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(url);
    picker.socket = socket;
    socket.onopen = () => {
      if (workspacePicker !== picker || picker.socket !== socket || pickerPageLeaving) { socket.close(); return; }
      socket.send(JSON.stringify({ action: start ? 'start' : 'resume' }));
      if (picker.cancelRequested) socket.send(JSON.stringify({ action: 'cancel' }));
    };
    socket.onmessage = (event) => {
      if (workspacePicker !== picker || picker.socket !== socket || pickerPageLeaving) return;
      const result = JSON.parse(event.data);
      if (result.status === 'reconnecting') {
        $('#workspace-path-help').textContent = 'Waiting for the previous picker connection to close…';
      } else if (result.status === 'pending') {
        clearTimeout(picker.connectionTimer);
        picker.connectionTimer = null;
        $('#workspace-path-help').textContent = picker.cancelRequested
          ? 'Closing folder picker…'
          : 'Choose a folder in the picker, or use Cancel here to close it.';
      } else if (result.status === 'cancel-error') {
        $('#workspace-path-help').textContent = result.detail;
        toast(result.detail, 'warning');
      } else if (picker.cancelRequested && !['cancelled', 'missing'].includes(result.status)) {
        // A selected result can race with Cancel. Wait for cancellation acknowledgement.
        socket.send(JSON.stringify({ action: 'cancel' }));
      } else {
        finishWorkspacePicker(picker, result);
      }
    };
    socket.onclose = () => {
      if (workspacePicker !== picker || picker.socket !== socket || pickerPageLeaving) return;
      waitForPickerConnection(picker);
      $('#workspace-path-help').textContent = 'Reconnecting to folder picker… You can use Cancel to leave this dialog.';
      picker.retry = setTimeout(() => connectWorkspacePicker(picker), 1000);
    };
  }

  function beginWorkspacePicker(token = null, resume = false) {
    if (workspacePicker) return;
    const savedToken = sessionStorage.getItem(pickerStorageKey);
    token = token || savedToken || crypto.randomUUID();
    const pendingCancel = sessionStorage.getItem(pickerStorageKey + '.cancel') === token;
    const restartAfterCancel = !resume && pendingCancel;
    resume = resume || token === savedToken;
    sessionStorage.setItem(pickerStorageKey, token);
    sessionStorage.setItem(pickerStorageKey + '.path', $('#workspace-path-input').value);
    const controls = Array.from($('#workspace-dialog').querySelectorAll('#browse-workspace-btn, #apply-workspace, input'));
    const picker = {
      token, controls, disabled: controls.map((control) => control.disabled),
      help: $('#workspace-path-help').textContent,
      cancelRequested: pendingCancel, closeOnCancel: pendingCancel && !restartAfterCancel,
      restartAfterCancel, socket: null, retry: null, connectionTimer: null, cancelTimer: null
    };
    workspacePicker = picker;
    controls.forEach((control) => { control.disabled = true; });
    $('#workspace-path-help').textContent = resume ? 'Reconnecting to folder picker…' : 'Opening folder picker…';
    connectWorkspacePicker(picker, !resume);
    if (pendingCancel) picker.cancelTimer = setTimeout(() => disconnectWorkspacePicker(picker), 5000);
  }

  function cancelWorkspacePicker() {
    const picker = workspacePicker;
    if (!picker) return;
    picker.cancelRequested = true;
    picker.closeOnCancel = true;
    picker.restartAfterCancel = false;
    sessionStorage.setItem(pickerStorageKey + '.cancel', picker.token);
    $('#workspace-path-help').textContent = 'Closing folder picker…';
    if (picker.socket?.readyState === WebSocket.OPEN) {
      picker.socket.send(JSON.stringify({ action: 'cancel' }));
      if (!picker.cancelTimer) picker.cancelTimer = setTimeout(() => disconnectWorkspacePicker(picker), 5000);
    } else {
      disconnectWorkspacePicker(picker);
    }
  }

  function restoreWorkspacePicker(workspaceReady) {
    const token = sessionStorage.getItem(pickerStorageKey);
    if (!token) return;
    openWorkspaceDialog();
    const input = $('#workspace-path-input');
    const savedPath = sessionStorage.getItem(pickerStorageKey + '.path');
    if (savedPath !== null) input.value = savedPath;
    const initialPath = input.value;
    let edited = false;
    const markEdited = () => { edited = true; };
    input.addEventListener('input', markEdited);
    beginWorkspacePicker(token, true);
    workspaceReady.then(() => {
      input.removeEventListener('input', markEdited);
      // Fill only missing initial data, never a selected path or a user's draft.
      if (savedPath === null && !edited && input.value === initialPath && !initialPath) input.value = getWorkspaceRoot();
    });
  }

  function init() {
    $('#browse-workspace-btn').onclick = () => beginWorkspacePicker();
    window.addEventListener('pagehide', () => {
      pickerPageLeaving = true;
      if (workspacePicker) {
        clearTimeout(workspacePicker.retry);
        clearTimeout(workspacePicker.connectionTimer);
        workspacePicker.connectionTimer = null;
        clearTimeout(workspacePicker.cancelTimer);
        workspacePicker.cancelTimer = null;
        workspacePicker.socket?.close();
      }
    });
    window.addEventListener('pageshow', () => {
      if (!pickerPageLeaving) return;
      pickerPageLeaving = false;
      if (workspacePicker) {
        connectWorkspacePicker(workspacePicker);
        if (workspacePicker.cancelRequested) workspacePicker.cancelTimer = setTimeout(() => disconnectWorkspacePicker(workspacePicker), 5000);
      }
    });
  }

  return { init, cancel: cancelWorkspacePicker, restore: restoreWorkspacePicker, isPicking: () => workspacePicker !== null };
}
