/* Present attachment recovery in one dialog while maintenance continues independently. */

import { $, refreshIcons, setText } from './ui.js';
import { bindDialogBackdrop, showActionDialog } from './dialogs.js';
import { createDisclosure } from './disclosure.js';
import { createFieldValidation } from './field-validation.js';
import { refreshStorageTime, setStorageStatus, setStorageTime, showStorageResults } from './storage-results.js';

const PREVIEW = 'recovery-preview';
const APPLY = 'recovery-apply';

const problems = {
  upload_copy_missing: 'The stored copy is missing.',
  upload_size_mismatch: 'The stored copy has the wrong size.',
  upload_hash_mismatch: 'The stored copy has the wrong SHA-256 hash.'
};

const backupStates = {
  verified: ['Verified backup', 'ok'],
  missing: ['Backup missing', 'review'],
  mismatch: ['Backup differs', 'review'],
  unavailable: ['Backup unavailable', 'unavailable']
};

const freshPreviewErrors = new Set(['recovery_preview_required', 'recovery_preview_changed', 'backup_changed']);

function previewFinding({ path, problem, backup }) {
  const [label, status] = backupStates[backup] || backupStates.unavailable;
  return { path, label, status, message: problems[problem] || 'The stored copy needs review.' };
}

function setButtonPresentation(button, label, iconName) {
  setText(button.querySelector('span'), label);
  const icon = button.querySelector('[data-lucide]');
  if (icon.dataset.lucide === iconName) return;
  const next = document.createElement('i');
  next.dataset.lucide = iconName;
  next.setAttribute('aria-hidden', 'true');
  icon.replaceWith(next);
  refreshIcons(button);
}

export function initializeStorageRecovery(settingsDialog, queueMaintenance, refreshMaintenance, acknowledgeResult) {
  const dialog = $('#storage-recovery-dialog');
  const entry = $('#review-attachment-recovery');
  const heading = $('#storage-recovery-title');
  const body = dialog.querySelector('.storage-recovery-body');
  const footer = dialog.querySelector('.storage-recovery-footer');
  const setup = $('#storage-recovery-setup');
  const backupStep = $('#storage-recovery-backup-step');
  const previewTitle = $('#storage-recovery-preview-title');
  const outputStep = $('#storage-recovery-output-step');
  const copyTitle = $('#storage-recovery-copy-title');
  const backButton = $('#back-storage-recovery');
  const createCopyButton = $('#create-storage-recovery-copy');
  const restartButton = $('#restart-storage-recovery');
  const restartLabel = restartButton.querySelector('span');
  const submitStatus = $('#storage-recovery-submit-status');
  const overviewStatus = $('#storage-recovery-overview-status');
  const overviewTime = $('#storage-recovery-overview-time');
  const backup = $('#storage-recovery-backup');
  const output = $('#storage-recovery-output');
  const previewButton = $('#preview-storage-recovery');
  const usePreviewButton = $('#use-storage-recovery-preview');
  const approval = $('#storage-recovery-approval');
  const copyOutput = $('#storage-recovery-copy-output');
  const copyRecord = $('#storage-recovery-copy-record');
  const pickers = [backup, output].map(field => ({ field, button: $(`#browse-${field.id}`), status: $(`#${field.id}-picker-status`) }));
  const followProgress = 'You can close this dialog and follow progress in Storage.';
  const views = {
    [PREVIEW]: { title: 'recovery preview', timeLabel: 'Previewed', field: 'recovery_preview', prefix: 'storage-recovery-preview',
      progressNote: `Helios checks the backup during restart. ${followProgress}` },
    [APPLY]: { title: 'recovery copy', timeLabel: 'Completed', field: 'recovery_copy', prefix: 'storage-recovery-copy',
      progressNote: followProgress }
  };
  for (const view of Object.values(views)) {
    for (const part of ['view', 'status', 'time', 'results', 'omitted', 'details', 'note']) view[part] = $(`#${view.prefix}-${part}`);
    view.summary = view.details.querySelector('summary');
    view.summary.addEventListener('click', event => {
      if (view.summary.getAttribute('aria-disabled') !== 'true') return;
      event.preventDefault();
      event.stopImmediatePropagation();
    }, { capture: true });
    view.disclosure = createDisclosure(view.details, view.details.querySelector(':scope > .settings-disclosure-body'));
  }
  const previewView = views[PREVIEW];

  const validation = createFieldValidation(setup);
  let state = null;
  let activity = {};
  let picker = null;
  let reviewed = null;
  let backupCheck = null;
  let copyConfirmation = null;
  let choosingBackup = true;
  let discardedPreviewId = null;
  let shownResult = null;
  let submitError = '';
  let submitField = null;
  let waitingForRestart = false;
  let statusUnavailable = false;

  function recentResult() {
    const ticket = activity.recoveryResult;
    const view = views[ticket?.kind];
    if (!view || !state || !Object.hasOwn(state, view.field)) return null;
    const job = state[view.field];
    if (ticket.missing) return { ...ticket, job: null };
    if (job?.id !== ticket.id || !job.finished_at || Date.parse(activity.lastCheckAt) > Date.parse(job.finished_at)) return null;
    return { ...ticket, job };
  }

  function currentPreview() {
    const recent = recentResult();
    if (recent?.kind === PREVIEW && !choosingBackup) return recent.job;
    const preview = state?.recovery_preview;
    return canRecover() && preview?.can_apply && preview.id !== discardedPreviewId && !needsNewPreview(preview) ? preview : null;
  }

  function needsNewPreview(preview) {
    const copy = state?.recovery_copy;
    return copy?.status === 'failed' && freshPreviewErrors.has(copy.code)
      && Date.parse(copy.finished_at) >= Date.parse(preview.finished_at);
  }

  function recoveryInProgress() {
    const kind = activity.pending?.kind || activity.scheduling;
    return kind === PREVIEW || kind === APPLY ? kind : null;
  }

  function canRetryOutput() {
    const copy = recentResult()?.job;
    return ['output_changed', 'invalid_output_dir'].includes(copy?.code) && currentPreview()?.can_apply;
  }

  function canRecover() {
    return Boolean(state?.available && Object.hasOwn(state, 'recovery_preview') && activity.hasRecoveryCandidates);
  }

  function isCleanPreview(job) {
    return job?.status === 'completed' && job.report.verified === 0 && job.report.unresolved === 0;
  }

  function recoveryUnavailableMessage() {
    if (!state) return statusUnavailable ? 'Could not load storage status. Reopen Storage to try again.' : 'Loading storage status…';
    if (!Object.hasOwn(state, 'recovery_preview')) return 'Open Helios on its host computer to use attachment recovery.';
    if (!state.available) return 'Start Helios with python app.py to preview or create a recovery copy.';
    if (activity.hasCheckReport) return 'Storage checks found no missing or damaged attachment copies.';
    return 'Run a storage check to look for missing or damaged attachment copies.';
  }

  function cancelBackupCheck() {
    if (!backupCheck) return;
    clearTimeout(backupCheck.timer);
    backupCheck.controller.abort();
    backupCheck = null;
  }

  function backupMatchesPreview(preview, path) {
    if (reviewed?.id === preview.id && reviewed.path === path) {
      cancelBackupCheck();
      return true;
    }
    if (backupCheck?.id === preview.id && backupCheck.path === path) return backupCheck.matches;
    cancelBackupCheck();
    if (!dialog.open || activity.busy) return false;
    const check = { id: preview.id, path, matches: false, controller: new AbortController() };
    backupCheck = check;
    check.timer = setTimeout(async () => {
      try {
        const response = await fetch('/api/storage/recovery/preview/backup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Helios-Maintenance': 'recovery-preview-backup' },
          body: JSON.stringify({ preview_id: check.id, backup_path: check.path }),
          signal: check.controller.signal
        });
        check.matches = response.status === 204;
        if (backupCheck === check) renderDialog();
      } catch { /* Keep reuse unavailable until the folder can be checked. */ }
    }, 220);
    return false;
  }

  function updateResultNote(kind, job, working, unavailableReason) {
    const view = views[kind];
    const hasResult = job?.finished_at && (kind !== PREVIEW || !isCleanPreview(job));
    const message = working === kind ? view.progressNote : hasResult ? unavailableReason : '';
    view.note.hidden = !message;
    setText(view.note, message);
  }

  function updateControls() {
    const available = canRecover();
    const enabled = available && !activity.busy;
    const preview = currentPreview();
    const selected = backup.value.trim();
    const working = recoveryInProgress();
    const recent = recentResult();
    const unavailableReason = available || working ? '' : recoveryUnavailableMessage();
    entry.disabled = !(enabled || working || recent);
    entry.title = entry.disabled ? (available ? 'A storage check is in progress.' : unavailableReason) : '';
    if (working) setButtonPresentation(entry, 'View recovery progress', 'activity');
    else if (recent) setButtonPresentation(entry, 'View recovery result', 'file-text');
    else setButtonPresentation(entry, 'Attachment recovery', 'database-backup');
    const showingCopy = working === APPLY || (!working && recent?.kind === APPLY);
    const previewShowing = working === PREVIEW || !working && !choosingBackup && Boolean(preview || recent?.kind === PREVIEW);
    const canGoBack = available && previewShowing && !working && !showingCopy;
    const canApply = preview?.can_apply && !needsNewPreview(preview);
    const canCheckBackup = available && canApply && Boolean(selected);
    if (!canCheckBackup) cancelBackupCheck();
    const canChooseOutput = canCheckBackup && backupMatchesPreview(preview, selected);
    setup.hidden = showingCopy;
    views[APPLY].view.hidden = !showingCopy;
    backupStep.hidden = previewShowing || !available;
    outputStep.hidden = !previewShowing || !canChooseOutput || working === PREVIEW;
    previewView.view.hidden = !previewShowing;
    updateResultNote(PREVIEW, preview, working, unavailableReason);
    updateResultNote(APPLY, recent?.kind === APPLY ? recent.job : null, working, unavailableReason);
    backButton.hidden = !canGoBack && !working;
    backButton.disabled = Boolean(working);
    const showingBackup = !setup.hidden && !backupStep.hidden;
    previewButton.hidden = !showingBackup && working !== PREVIEW;
    usePreviewButton.hidden = !showingBackup || !canApply;
    createCopyButton.hidden = working !== APPLY && (setup.hidden || outputStep.hidden);
    approval.hidden = !previewShowing || !available || working === PREVIEW || !canApply || canChooseOutput;
    restartButton.hidden = !showingCopy || Boolean(working) || !available;
    restartButton.disabled = !enabled;
    setText(restartLabel, canRetryOutput() ? 'Choose another output folder' : 'Start a new preview');
    footer.hidden = [...footer.children].every(child => child.hidden);
    previewButton.disabled = previewButton.hidden || !enabled || Boolean(picker) || !selected;
    usePreviewButton.disabled = usePreviewButton.hidden || !enabled || Boolean(picker) || !canChooseOutput;
    createCopyButton.disabled = createCopyButton.hidden || !enabled || Boolean(picker) || !output.value.trim();
    for (const item of pickers) {
      item.field.disabled = !enabled || Boolean(picker);
      const active = picker?.item === item;
      item.button.disabled = active ? picker.stopping : !enabled || Boolean(picker);
      const label = active ? 'Stop' : 'Browse';
      setButtonPresentation(item.button, label, active ? 'square' : 'folder-open');
      const folder = item.field === backup ? 'backup' : 'output';
      item.button.setAttribute('aria-label', active ? `Stop ${folder} folder selection` : `Browse for ${folder} folder`);
    }
  }

  function finishPicker(active, message = '', tone = 'info', focus = false) {
    if (active !== picker) return;
    picker = null;
    clearTimeout(active.timer);
    active.socket.close();
    setStorageStatus(active.item.status, message, tone);
    updateControls();
    if (focus && dialog.open && !$('#settings-storage').hidden) {
      (message ? active.item.button : active.item.field).focus({ preventScroll: true });
    }
  }

  function stopPicker(leaving = false) {
    if (!picker) return;
    const active = picker;
    if (active.socket.readyState !== WebSocket.OPEN) {
      finishPicker(active, '', 'info', !leaving);
      return;
    }
    if (!active.stopping) active.socket.send(JSON.stringify({ action: 'cancel' }));
    if (leaving) {
      finishPicker(active);
      return;
    }
    active.stopping = true;
    updateControls();
    setStorageStatus(active.item.status, 'Stopping folder selection…', 'running');
    clearTimeout(active.timer);
    active.timer = setTimeout(() => finishPicker(active, 'Could not confirm folder selection stopped. Try Browse again.', 'warning'), 6000);
  }

  function browse(item) {
    if (picker?.item === item) {
      stopPicker();
      return;
    }
    if (picker || !canRecover() || activity.busy) return;
    const url = new URL(`/api/storage/recovery/picker/${crypto.randomUUID()}`, location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(url);
    const active = { item, socket, stopping: false, timer: null };
    picker = active;
    updateControls();
    setStorageStatus(item.status, 'Opening folder picker…', 'running');
    active.timer = setTimeout(() => finishPicker(active, 'Could not open the folder picker. Enter the path manually.', 'error', true), 10000);
    socket.onopen = () => {
      if (picker === active) socket.send(JSON.stringify({ action: 'start' }));
    };
    socket.onmessage = ({ data }) => {
      if (picker !== active) return;
      const result = JSON.parse(data);
      if (result.status === 'pending') {
        if (!active.stopping) {
          clearTimeout(active.timer);
          setStorageStatus(item.status, 'Waiting for folder selection…', 'running');
        }
      } else if (result.status === 'cancel-error') {
        clearTimeout(active.timer);
        active.stopping = false;
        setStorageStatus(item.status, 'Could not stop folder selection. Try Stop again.', 'error');
        updateControls();
      } else if (active.stopping && result.status === 'selected') {
        socket.send(JSON.stringify({ action: 'cancel' }));
      } else if (result.status === 'selected') {
        item.field.value = result.path;
        item.field.dispatchEvent(new Event('input', { bubbles: true }));
        finishPicker(active, '', 'info', true);
      } else if (result.status === 'cancelled') {
        finishPicker(active, 'Folder selection cancelled.', 'info', true);
      } else {
        finishPicker(active, result.detail || 'Could not choose a folder. Enter the path manually.', 'error', true);
      }
    };
    socket.onclose = () => finishPicker(active, 'Folder picker disconnected. Try Browse again or enter the path manually.', 'warning', true);
  }

  function render(kind) {
    const view = views[kind];
    const recent = recentResult();
    const job = kind === PREVIEW ? currentPreview() : recent?.kind === APPLY ? recent.job : null;
    const available = canRecover();
    const running = recoveryInProgress() === kind;
    const missing = recent?.kind === kind && recent.missing;
    const key = running ? `pending:${activity.pending?.id || 'scheduling'}:${waitingForRestart}` : missing ? 'missing' : `${job?.id || ''}:${available}`;
    if (key === view.key) return;
    view.key = key;
    view.disclosure.finish(false);
    setStorageTime(view.time, job?.status === 'failed' ? 'Attempt ended' : view.timeLabel,
      running || missing ? null : job?.finished_at);
    if (kind === APPLY) copyOutput.hidden = copyRecord.hidden = true;
    if (running || missing || !job || job.status !== 'completed') {
      view.details.hidden = !running;
      view.summary.setAttribute('aria-disabled', 'true');
      showStorageResults(view.results, view.omitted, [], 0);
      if (running) setStorageStatus(view.status, progressText(kind), 'running');
      else if (missing) setStorageStatus(view.status, `Helios restarted without a report for the ${view.title}. Check the launcher output.`, 'error');
      else setStorageStatus(view.status, job?.message || '', 'error');
      return;
    }
    const report = job.report;
    if (kind === PREVIEW) {
      let summary = 'No missing or damaged attachment copies found.';
      if (!isCleanPreview(job)) {
        summary = `Preview complete. ${report.verified} verified · ${report.unresolved} unresolved.`;
        if (available && !report.verified) summary += ' Choose another backup folder to try again.';
        else if (available && !job.can_apply) summary += ' Run a new preview to create a copy.';
      }
      setStorageStatus(view.status, summary, report.unresolved ? 'warning' : 'success');
      showStorageResults(view.results, view.omitted, report.attachments.map(previewFinding), report.omitted_attachments, 'attachment');
    } else {
      const { problems: problemsFound, review, deferred } = report.diagnostics.summary;
      const warning = report.unresolved || problemsFound || review || deferred;
      const summary = `Copy created. ${report.restored} restored · ${report.unresolved} unresolved.`;
      setStorageStatus(view.status, summary, warning ? 'warning' : 'success');
      setText(copyOutput.querySelector('code'), report.output);
      copyOutput.hidden = copyRecord.hidden = false;
      showStorageResults(view.results, view.omitted, report.diagnostics.findings, report.diagnostics.omitted_findings);
    }
    const hasDetails = !view.results.hidden || !view.omitted.hidden;
    view.details.hidden = false;
    view.summary.setAttribute('aria-disabled', String(!hasDetails));
  }

  function progressText(kind, inOverview = false) {
    if (waitingForRestart) return 'Waiting for Helios to restart…';
    if (activity.scheduling === kind) {
      const label = kind === PREVIEW && !inOverview ? 'preview' : views[kind].title;
      return `Starting ${label}…`;
    }
    return 'Waiting for active requests to finish…';
  }

  function renderOverview() {
    const working = recoveryInProgress();
    const recent = recentResult();
    const job = recent?.job;
    setStorageTime(overviewTime, 'Finished', working ? null : job?.finished_at);
    if (working) setStorageStatus(overviewStatus, progressText(working, true), 'running');
    else if (recent?.missing) setStorageStatus(overviewStatus, 'Recovery report unavailable.', 'error');
    else if (submitError) setStorageStatus(overviewStatus, 'Could not start attachment recovery.', 'error');
    else if (job?.status === 'failed') setStorageStatus(overviewStatus, 'Attachment recovery needs review.', 'error');
    else if (recent?.kind === APPLY && job?.report) {
      const { restored, unresolved } = job.report;
      setStorageStatus(overviewStatus, `Recovery copy created. ${restored} restored · ${unresolved} unresolved.`, unresolved ? 'warning' : 'success');
    } else if (job?.report) {
      const { verified, unresolved } = job.report;
      setStorageStatus(overviewStatus, `Recovery preview. ${verified} verified · ${unresolved} unresolved.`, unresolved ? 'warning' : 'success');
    } else if (activity.hasRecoveryCandidates) setStorageStatus(overviewStatus, 'A check found missing or damaged attachment copies.', 'warning');
    else setStorageStatus(overviewStatus, '');
  }

  function currentHeading() {
    if (!setup.hidden && !previewView.view.hidden) return previewTitle;
    return views[APPLY].view.hidden ? heading : copyTitle;
  }

  function renderDialog() {
    const active = document.activeElement;
    const previewStep = previewView.view;
    const wasShowingPreview = !setup.hidden && !previewStep.hidden;
    render(PREVIEW);
    render(APPLY);
    updateControls();
    const recent = recentResult();
    const working = recoveryInProgress();
    if (dialog.open) shownResult = !working && (recent?.kind === APPLY || recent?.kind === PREVIEW && !choosingBackup) ? recent : null;
    const error = submitField ? '' : submitError;
    const busy = activity.busy && !working ? 'A storage check is in progress.' : '';
    const unavailable = !working && !recent && !canRecover() ? recoveryUnavailableMessage() : '';
    setStorageStatus(submitStatus, error || busy || unavailable, error ? 'error' : busy ? 'running' : 'info');
    if (dialog.open && !wasShowingPreview && !setup.hidden && !previewStep.hidden) {
      body.scrollTop = 0;
      previewTitle.focus({ preventScroll: true });
    } else if (dialog.open && dialog.contains(active) && (!active.getClientRects().length || active.disabled)) {
      currentHeading().focus({ preventScroll: true });
    }
  }

  async function submit(kind, payload) {
    submitError = '';
    submitField = null;
    try {
      const job = await queueMaintenance(kind, payload);
      validation.reset();
      if (kind === PREVIEW) {
        reviewed = { id: job.id, path: payload.backup_path };
        discardedPreviewId = null;
        choosingBackup = false;
      }
    } catch (error) {
      submitError = error.message;
      if (['invalid_backup_dir', 'backup_changed'].includes(error.code)) {
        if (kind === APPLY) {
          reviewed = null;
          cancelBackupCheck();
        }
        choosingBackup = true;
        submitField = backup;
      } else if (['invalid_output_dir', 'recovery_copy_exists'].includes(error.code)) {
        submitField = output;
      }
    }
    renderDialog();
    if (submitField) validation.showFieldError(submitField, submitError, {
      focus: dialog.open && Boolean(submitField.getClientRects().length) && !submitField.disabled
    });
    renderOverview();
  }

  function dismiss() {
    if (!dialog.open) return;
    copyConfirmation?.abort();
    stopPicker(true);
    cancelBackupCheck();
    dialog.close();
  }

  previewButton.addEventListener('click', () => {
    if (!previewButton.disabled) void submit(PREVIEW, { backup_path: backup.value.trim() });
  });
  usePreviewButton.addEventListener('click', () => {
    if (usePreviewButton.disabled) return;
    choosingBackup = false;
    renderDialog();
  });
  createCopyButton.addEventListener('click', async () => {
    if (createCopyButton.disabled || copyConfirmation) return;
    const payload = { preview_id: currentPreview().id, backup_path: backup.value.trim(), output_dir: output.value.trim() };
    const request = new AbortController();
    copyConfirmation = request;
    const accepted = await showActionDialog({
      title: 'Create a recovery copy?',
      description: 'Helios will finish active requests, create and verify a separate copy during restart, and reopen with your current storage. The copy may contain conversations and connection credentials.',
      confirmLabel: 'Create copy',
      signal: request.signal
    });
    copyConfirmation = null;
    if (!accepted || request.signal.aborted || !dialog.open || !settingsDialog.open) return;
    if (createCopyButton.disabled || currentPreview()?.id !== payload.preview_id
      || backup.value.trim() !== payload.backup_path || output.value.trim() !== payload.output_dir) {
      submitError = 'Recovery inputs changed. Check the current preview and output folder before trying again.';
      submitField = null;
      renderDialog();
      return;
    }
    await submit(APPLY, payload);
  });
  backButton.addEventListener('click', () => {
    stopPicker(true);
    choosingBackup = true;
    submitError = '';
    submitField = null;
    const recent = recentResult();
    if (recent?.kind === PREVIEW) acknowledgeResult(recent);
    renderDialog();
    body.scrollTop = 0;
    (backup.disabled ? heading : backup).focus({ preventScroll: true });
  });
  restartButton.addEventListener('click', () => {
    const chooseOutput = canRetryOutput();
    choosingBackup = !chooseOutput || !backup.value.trim();
    if (chooseOutput) output.value = '';
    else discardedPreviewId = currentPreview()?.id || null;
    submitError = '';
    submitField = null;
    validation.reset();
    const recent = recentResult();
    if (recent?.kind === APPLY) acknowledgeResult(recent);
    renderDialog();
    (chooseOutput && !outputStep.hidden ? output : backup).focus();
  });
  for (const item of pickers) {
    item.button.addEventListener('click', () => browse(item));
    item.field.addEventListener('input', () => {
      submitError = '';
      submitField = null;
      renderDialog();
      renderOverview();
    });
  }
  $('#close-storage-recovery').addEventListener('click', dismiss);
  entry.addEventListener('click', () => {
    if (entry.disabled || dialog.open || !settingsDialog.open) return;
    choosingBackup = !recoveryInProgress() && recentResult()?.kind !== PREVIEW;
    dialog.showModal();
    renderDialog();
    (recoveryInProgress() ? currentHeading() : heading).focus({ preventScroll: true });
    void refreshMaintenance();
  });
  bindDialogBackdrop(dialog, dismiss);
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    dismiss();
  });
  dialog.addEventListener('close', () => {
    if (dialog.open) return;
    copyConfirmation?.abort();
    stopPicker(true);
    cancelBackupCheck();
    for (const view of Object.values(views)) view.disclosure.finish();
    if (shownResult) acknowledgeResult(shownResult);
    shownResult = null;
    if (settingsDialog.open && entry.getClientRects().length) {
      (entry.disabled ? $('#settings-storage-tab') : entry).focus({ preventScroll: true });
    }
  });
  settingsDialog.addEventListener('close', dismiss);
  settingsDialog.addEventListener('settings-panel-change', event => {
    if (event.detail.name !== 'storage') dismiss();
  });
  window.addEventListener('pagehide', () => stopPicker(true));
  document.addEventListener('timezone-change', () => {
    for (const view of Object.values(views)) refreshStorageTime(view.time);
    refreshStorageTime(overviewTime);
  });

  return {
    update(snapshot, work) {
      state = snapshot;
      activity = work;
      waitingForRestart = false;
      if (snapshot) {
        statusUnavailable = false;
        if (!canRecover()) {
          stopPicker(true);
          discardedPreviewId = null;
        }
      }
      if (work.pending) {
        stopPicker(true);
        if (recoveryInProgress()) {
          choosingBackup = false;
          submitError = '';
          submitField = null;
        }
      }
      renderDialog();
      renderOverview();
    },
    waiting() {
      waitingForRestart = true;
      renderDialog();
      renderOverview();
    },
    unavailable() {
      statusUnavailable = true;
      renderDialog();
    }
  };
}
