/* Run storage checks and coordinate one managed maintenance job across Settings. */

import { $, setText } from './ui.js';
import { showActionDialog } from './dialogs.js';
import { initializeStorageRecovery } from './storage-recovery.js';
import { refreshStorageTime, setStorageStatus, setStorageTime, showStorageResults } from './storage-results.js';

const maintenanceJobs = {
  'deep-check': { field: 'job', url: '/api/storage/maintenance', label: 'deep check' },
  'recovery-preview': { field: 'recovery_preview', url: '/api/storage/recovery/preview', label: 'recovery preview' },
  'recovery-apply': { field: 'recovery_copy', url: '/api/storage/recovery/apply', label: 'recovery copy' }
};

const savedRecoveryCodes = new Set(['upload_copy_missing', 'upload_size_mismatch', 'upload_hash_mismatch']);
const maintenanceTicketKey = 'helios.storageMaintenance';
const recoveryResultKey = 'helios.recoveryResult';

function readTicket(key) {
  try {
    const ticket = JSON.parse(sessionStorage.getItem(key));
    if (ticket && Object.hasOwn(maintenanceJobs, ticket.kind) && typeof ticket.id === 'string') return ticket;
  } catch { /* Storage can be unavailable in browser privacy modes. */ }
  return null;
}

function saveTicket(key, ticket) {
  try {
    if (ticket) sessionStorage.setItem(key, JSON.stringify(ticket));
    else sessionStorage.removeItem(key);
  } catch { /* Monitoring continues in memory. */ }
}

function describeReport({ summary: { problems, review, deferred } }) {
  const notices = [];
  if (problems) notices.push(`${problems} ${problems === 1 ? 'problem' : 'problems'} found`);
  if (review) notices.push(`${review} ${review === 1 ? 'item needs' : 'items need'} review`);
  if (deferred) notices.push(`${deferred} ${deferred === 1 ? 'check was' : 'checks were'} deferred`);
  return notices.length ? `${notices.join(' · ')}.` : 'Passed.';
}

export function initializeStorageDiagnostics(dialog) {
  const action = $('#run-storage-check');
  const deepAction = $('#run-storage-deep-check');
  const reports = { basic: $('#storage-basic-report'), deep: $('#storage-deep-report') };
  const checks = { basic: null, deep: null };
  let current = null;
  let state = null;
  let scheduling = null;
  let pending = readTicket(maintenanceTicketKey);
  let recoveryResult = readTicket(recoveryResultKey);
  let missing = null;
  let basicNotice = null;
  let waitingForDeepRestart = false;
  let unsavedBasic = null;
  let pollTimer = null;
  let statusVersion = 0;
  const recovery = initializeStorageRecovery(dialog, queueMaintenance, refreshMaintenance, acknowledgeRecoveryResult);

  function updateAction(running) {
    setText(action.querySelector('span'), running ? 'Stop check' : 'Run basic check');
    action.querySelector('[data-storage-action="check"]').toggleAttribute('hidden', running);
    action.querySelector('[data-storage-action="stop"]').toggleAttribute('hidden', !running);
    const busy = scheduling || Boolean(pending);
    action.disabled = busy;
    deepAction.disabled = !state?.available || running || busy;
    recovery.update(state, {
      busy: busy || running, pending, scheduling, recoveryResult,
      hasCheckReport: Boolean(checks.basic?.report || checks.deep?.report),
      lastCheckAt: latestResult(checks.basic?.report ? checks.basic : null, checks.deep?.report ? checks.deep : null)?.finished_at,
      hasRecoveryCandidates: hasRecoveryCandidates()
    });
  }

  function latestResult(first, second) {
    if (!second || (first && Date.parse(first.finished_at) > Date.parse(second.finished_at))) return first;
    return second;
  }

  function hasRecoveryCandidates() {
    const { basic, deep } = checks;
    // A newer completed deep check covers the attachment checks in the earlier basic report.
    const reports = deep?.report?.complete && latestResult(basic, deep) === deep ? [deep] : [basic, deep];
    return reports.some(job => {
      const report = job?.report;
      return report?.has_attachment_recovery_candidates ?? report?.findings.some(finding => savedRecoveryCodes.has(finding.code));
    });
  }

  function describeCheck(job) {
    if (!job) return ['No checks yet.', 'info'];
    if (job.status !== 'completed') return [job.message || 'The deep check did not finish.', 'error'];
    const { problems, review, deferred } = job.report.summary;
    const message = describeReport(job.report);
    if (job === unsavedBasic) return [`${message} Could not save this report.`, 'warning'];
    return [message, problems ? 'error' : review || deferred ? 'warning' : 'success'];
  }

  function deepNotice() {
    if (scheduling === 'deep-check') return ['Scheduling the deep check…', 'running'];
    if (pending?.kind === 'deep-check') return [waitingForDeepRestart ? 'Waiting for Helios to restart…'
      : 'Waiting for active requests to finish…', 'running'];
    if (missing === 'deep-check') return ['Helios restarted without a report for this check. Check the launcher output.', 'error'];
    return null;
  }

  function renderFindings(section, job) {
    const id = job?.id || '';
    if (section.dataset.resultId === id) return;
    section.dataset.resultId = id;
    const region = section.querySelector('.storage-result-list');
    const omitted = section.querySelector('.storage-result-omitted');
    showStorageResults(region, omitted, job?.report?.findings || [], job?.report?.omitted_findings || 0);

    const details = section.querySelector('details');
    const hideDetails = region.hidden && omitted.hidden;
    if (details.hidden === hideDetails) return;
    const placeholder = section.querySelector('.storage-check-placeholder');
    const target = hideDetails ? placeholder : details.querySelector('summary');
    target.prepend(section.querySelector('.storage-check-heading'));
    if (hideDetails) details.open = false;
    details.hidden = hideDetails;
    placeholder.hidden = !hideDetails;
  }

  function renderChecks() {
    const notices = { basic: basicNotice, deep: deepNotice() };
    for (const [kind, section] of Object.entries(reports)) {
      const job = checks[kind];
      const notice = notices[kind];
      renderFindings(section, job);
      setStorageStatus(section.querySelector('[data-check-status]'), ...(notice || describeCheck(job)));
      let label = notice ? 'Last checked' : 'Checked';
      if (job?.status === 'failed') label = notice ? 'Last attempt ended' : 'Attempt ended';
      setStorageTime(section.querySelector('[data-check-time]'), label, job?.finished_at);
    }
  }

  function stop() {
    current.abort();
    current = null;
    basicNotice = ['Check stopped.', 'stopped'];
    updateAction(false);
    renderChecks();
  }

  async function run() {
    if (current) {
      stop();
      return;
    }
    const controller = new AbortController();
    current = controller;
    updateAction(true);
    basicNotice = ['Checking saved data…', 'running'];
    renderChecks();

    try {
      const response = await fetch('/api/storage/diagnostics', { cache: 'no-store', signal: controller.signal });
      if (current !== controller) return;
      if (response.status === 409) {
        basicNotice = ['A storage check is already running. Try again shortly.', 'warning'];
        return;
      }
      if (!response.ok) throw new Error('Storage check failed.');
      const report = await response.json();
      if (current !== controller) return;
      const outcome = { id: report.id, status: 'completed', finished_at: report.finished_at, report };
      unsavedBasic = report.saved === false ? outcome : null;
      checks.basic = outcome;
      basicNotice = null;
      rememberRecoveryResult(null);
    } catch {
      if (current === controller) basicNotice = ['Could not run the basic check. Try again.', 'error'];
    } finally {
      if (current === controller) {
        current = null;
        renderChecks();
        updateAction(false);
      }
    }
  }

  function pollMaintenance() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refreshMaintenance, 1500);
  }

  function rememberTicket(ticket) {
    pending = ticket;
    saveTicket(maintenanceTicketKey, ticket);
  }

  function rememberRecoveryResult(ticket) {
    recoveryResult = ticket;
    saveTicket(recoveryResultKey, ticket);
  }

  function acknowledgeRecoveryResult(ticket) {
    if (recoveryResult?.id !== ticket.id || recoveryResult.kind !== ticket.kind) return;
    rememberRecoveryResult(null);
    updateAction(Boolean(current));
  }

  async function refreshMaintenance() {
    if (scheduling) return;
    const version = ++statusVersion;
    clearTimeout(pollTimer);
    try {
      const response = await fetch('/api/storage/maintenance', { cache: 'no-store' });
      if (!response.ok) throw new Error('Maintenance status unavailable.');
      const snapshot = await response.json();
      if (version !== statusVersion) return;
      state = snapshot;
      waitingForDeepRestart = false;
      if (!current) checks.basic = latestResult(unsavedBasic, state.basic);
      if (state.job?.status !== 'pending') checks.deep = state.job || null;
      const active = Object.entries(maintenanceJobs).find(([, operation]) => state[operation.field]?.status === 'pending');
      const serverTicket = active ? { kind: active[0], id: state[active[1].field].id } : null;
      const expected = pending;
      if (expected && expected.id !== serverTicket?.id) {
        const result = state[maintenanceJobs[expected.kind].field];
        if (expected.kind === 'deep-check') missing = result?.id !== expected.id ? expected.kind : null;
        else rememberRecoveryResult({ ...expected, missing: result?.id !== expected.id });
        document.dispatchEvent(new Event('helios-restarted'));
      }
      if (serverTicket && pending?.id !== serverTicket.id) rememberRecoveryResult(null);
      if (pending?.id !== serverTicket?.id) rememberTicket(serverTicket);
      if (pending) {
        if (current) stop();
        pollMaintenance();
      }
      renderChecks();
      updateAction(Boolean(current));
    } catch {
      if (version !== statusVersion) return;
      if (pending) {
        if (pending.kind === 'deep-check') {
          waitingForDeepRestart = true;
          renderChecks();
        } else recovery.waiting();
        pollMaintenance();
      } else if (!state) {
        recovery.unavailable();
      }
    }
  }

  async function queueMaintenance(kind, payload) {
    if (pending || current || scheduling) throw new Error('A storage check or recovery is already in progress.');
    const operation = maintenanceJobs[kind];
    scheduling = kind;
    statusVersion++;
    clearTimeout(pollTimer);
    updateAction(false);
    if (kind === 'deep-check') renderChecks();
    try {
      const headers = { 'X-Helios-Maintenance': kind };
      if (payload) headers['Content-Type'] = 'application/json';
      const response = await fetch(operation.url, {
        method: 'POST', headers, body: payload ? JSON.stringify(payload) : undefined
      });
      const job = await response.json().catch(() => null);
      if (!response.ok || !job?.id) {
        const detail = job?.detail;
        const error = new Error(detail?.message || (typeof detail === 'string' ? detail : `Could not schedule the ${operation.label}.`));
        error.code = detail?.code;
        throw error;
      }
      if (missing === kind) missing = null;
      rememberRecoveryResult(null);
      rememberTicket({ kind, id: job.id });
      pollMaintenance();
      return job;
    } finally {
      scheduling = null;
      if (kind === 'deep-check') renderChecks();
      updateAction(false);
      if (!pending) void refreshMaintenance();
    }
  }

  updateAction(false);
  renderChecks();
  action.addEventListener('click', run);
  deepAction.addEventListener('click', () => showActionDialog({
    title: 'Restart for a deep storage check?',
    description: 'Helios will finish active requests, restart after the check, and show the report here. Open file and folder pickers will close.',
    confirmLabel: 'Run deep check',
    onConfirm: () => queueMaintenance('deep-check')
  }));
  dialog.addEventListener('settings-panel-change', event => {
    if (event.detail.name === 'storage') refreshMaintenance();
  });
  dialog.addEventListener('close', () => {
    if (!dialog.open && current) stop();
  });
  document.addEventListener('timezone-change', () => {
    for (const section of Object.values(reports)) refreshStorageTime(section.querySelector('[data-check-time]'));
  });
  document.addEventListener('storage-cleanup-complete', event => {
    if (event.detail.kind === 'cache') void refreshMaintenance();
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && dialog.open && !$('#settings-storage').hidden) refreshMaintenance();
  });
  if (pending) void refreshMaintenance();
}
