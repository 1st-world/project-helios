/* Run storage checks from Settings and recover managed reports after a restart. */

import { $, refreshIcons, setText } from './ui.js';
import { showActionDialog } from './dialogs.js';
import { formatMessageTimestamp, formatTimestamp } from './timezone.js';

const findingPresentation = {
  ok: ['Passed', 'check'],
  compatible_legacy: ['Compatible older format', 'info'],
  not_initialized: ['No saved data', 'circle-dashed'],
  info: ['Information', 'info'],
  review: ['Needs review', 'triangle-alert'],
  unavailable: ['Deferred', 'clock'],
  invalid_data: ['Invalid data', 'circle-alert'],
  unsupported_version: ['Unsupported format', 'circle-alert'],
  missing_reference: ['Missing reference', 'circle-alert']
};

const summaryFindings = new Set(['conversations_read', 'attachment_hashes_checked']);

const noticeIcons = {
  running: 'loader-circle',
  stopped: 'square',
  success: 'check',
  warning: 'triangle-alert',
  error: 'circle-alert'
};

function statusIcon(name) {
  const icon = document.createElement('i');
  icon.dataset.lucide = name;
  icon.setAttribute('aria-hidden', 'true');
  return icon;
}

function findingRow(finding) {
  const row = document.createElement('li');
  const header = document.createElement('div');
  const path = document.createElement('code');
  const description = document.createElement('p');
  path.textContent = finding.path;
  const [label, icon] = Object.hasOwn(findingPresentation, finding.status)
    ? findingPresentation[finding.status] : findingPresentation.info;
  const status = document.createElement('span');
  const marker = document.createElement('span');
  const statusText = document.createElement('span');
  status.className = 'storage-finding-status';
  status.dataset.status = finding.status;
  marker.className = 'storage-finding-icon';
  marker.append(statusIcon(icon));
  statusText.textContent = finding.status === 'info' && summaryFindings.has(finding.code) ? 'Summary' : label;
  status.append(marker, statusText);
  header.append(path, status);
  description.textContent = finding.message;
  row.append(header, description);
  return row;
}

function describeReport({ summary: { problems, review, deferred }, scope }) {
  const notices = [];
  if (problems) notices.push(`${problems} ${problems === 1 ? 'problem' : 'problems'} found`);
  if (review) notices.push(`${review} ${review === 1 ? 'item needs' : 'items need'} review`);
  if (deferred) notices.push(`${deferred} ${deferred === 1 ? 'check was' : 'checks were'} deferred`);
  const check = scope === 'deep' ? 'Deep check' : 'Basic check';
  if (!notices.length) return `${check} passed.`;
  return `${check} finished. ${notices.join(' · ')}.`;
}

export function initializeStorageDiagnostics(dialog) {
  const action = $('#run-storage-check');
  const deepAction = $('#run-storage-deep-check');
  const status = $('#storage-check-status');
  const timing = $('#storage-check-time');
  const time = timing.querySelector('time');
  const results = $('#storage-check-results');
  const list = results.querySelector('ul');
  const omitted = $('#storage-check-omitted');
  let current = null;
  let available = false;
  let scheduling = false;
  let pending = null;
  let shownResultId = null;
  let unsavedBasic = null;
  let pollTimer = null;
  let statusVersion = 0;

  function showStatus(message, tone) {
    if (status.dataset.tone !== tone) {
      status.replaceChildren(statusIcon(noticeIcons[tone]), document.createTextNode(message));
      status.dataset.tone = tone;
      refreshIcons(status);
    } else if (status.lastChild.textContent !== message) {
      status.lastChild.textContent = message;
    }
  }

  function updateAction(running) {
    setText(action.querySelector('span'), running ? 'Stop check' : 'Run basic check');
    action.querySelector('[data-storage-action="check"]').toggleAttribute('hidden', running);
    action.querySelector('[data-storage-action="stop"]').toggleAttribute('hidden', !running);
    action.disabled = scheduling || Boolean(pending);
    deepAction.disabled = !available || running || scheduling || Boolean(pending);
    deepAction.title = available ? '' : 'Available with the Helios managed launcher';
  }

  function clearReport() {
    shownResultId = null;
    timing.hidden = true;
    results.hidden = true;
    omitted.hidden = true;
    list.replaceChildren();
  }

  function formatResultTime() {
    setText(time, formatTimestamp(time.dateTime));
    time.title = formatMessageTimestamp(time.dateTime)?.detail || '';
  }

  function showSavedResult(job) {
    if (job.status === 'completed') showReport(job.report);
    else {
      clearReport();
      showStatus(job.message || 'The deep check did not finish.', 'error');
    }
    shownResultId = job.id;
    setText(timing.querySelector('span'), job.status === 'completed' ? 'Checked' : 'Attempt ended');
    time.dateTime = job.finished_at;
    formatResultTime();
    timing.hidden = false;
  }

  function latestResult(basic, deep) {
    const latestBasic = unsavedBasic ?? basic;
    if (!deep || (latestBasic && Date.parse(latestBasic.finished_at) > Date.parse(deep.finished_at))) return latestBasic;
    return deep;
  }

  function showReport(report) {
    clearReport();
    const { problems, review, deferred } = report.summary;
    const tone = problems ? 'error' : review || deferred ? 'warning' : 'success';
    list.append(...report.findings.map(findingRow));
    refreshIcons(list);
    results.hidden = !list.children.length;
    results.scrollTop = 0;
    omitted.hidden = !report.omitted_findings;
    if (report.omitted_findings) {
      setText(omitted, `${report.omitted_findings} more ${report.omitted_findings === 1 ? 'finding' : 'findings'} in the totals above.`);
    }
    showStatus(describeReport(report), tone);
  }

  function stop() {
    current.abort();
    current = null;
    updateAction(false);
    showStatus('Storage check stopped.', 'stopped');
  }

  async function run() {
    if (current) {
      stop();
      return;
    }
    const controller = new AbortController();
    current = controller;
    unsavedBasic = null;
    updateAction(true);
    clearReport();
    showStatus('Checking saved data…', 'running');

    try {
      const response = await fetch('/api/storage/diagnostics', { cache: 'no-store', signal: controller.signal });
      if (current !== controller) return;
      if (response.status === 409) {
        showStatus('A storage check is already running. Try again shortly.', 'warning');
        return;
      }
      if (!response.ok) throw new Error('Storage check failed.');
      const report = await response.json();
      if (current !== controller) return;
      const outcome = { id: report.id, status: 'completed', finished_at: report.finished_at, report };
      if (report.saved === false) unsavedBasic = outcome;
      showSavedResult(outcome);
      if (unsavedBasic) showStatus(`${describeReport(report)} Could not save this report.`, 'warning');
    } catch {
      if (current === controller) showStatus('Could not run the storage check. Try again.', 'error');
    } finally {
      if (current === controller) {
        current = null;
        updateAction(false);
      }
    }
  }

  function pollMaintenance() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refreshMaintenance, 1500);
  }

  async function refreshMaintenance() {
    if (scheduling) return;
    const version = ++statusVersion;
    clearTimeout(pollTimer);
    try {
      const response = await fetch('/api/storage/maintenance', { cache: 'no-store' });
      if (!response.ok) throw new Error('Maintenance status unavailable.');
      const state = await response.json();
      if (version !== statusVersion) return;
      available = state.available;
      const job = state.job;
      if (job?.status === 'pending') {
        if (current) stop();
        if (pending !== job.id) clearReport();
        pending = job.id;
        showStatus('Waiting for requests to finish before the deep check…', 'running');
        pollMaintenance();
      } else {
        const expected = pending;
        pending = null;
        if (expected) document.dispatchEvent(new Event('helios-restarted'));
        if (expected && job?.id !== expected) {
          clearReport();
          showStatus('Helios restarted without a report for this check. Check the launcher output.', 'error');
        } else if (!current) {
          const outcome = expected ? job : latestResult(state.basic, job);
          if (outcome && outcome.id !== shownResultId) showSavedResult(outcome);
          else if (!outcome && shownResultId) {
            clearReport();
            status.replaceChildren();
            delete status.dataset.tone;
          }
        }
      }
      updateAction(Boolean(current));
    } catch {
      if (version === statusVersion && pending) {
        showStatus('Waiting for Helios to restart. The launcher keeps the deep check report.', 'running');
        pollMaintenance();
      }
    }
  }

  async function queueDeepCheck() {
    scheduling = true;
    statusVersion++;
    clearTimeout(pollTimer);
    updateAction(false);
    try {
      const response = await fetch('/api/storage/maintenance', {
        method: 'POST', headers: { 'X-Helios-Maintenance': 'deep-check' }
      });
      const job = await response.json();
      if (!response.ok) throw new Error(job.detail || 'Could not schedule the deep check.');
      pending = job.id;
      clearReport();
      showStatus('Waiting for requests to finish before the deep check…', 'running');
      pollMaintenance();
    } finally {
      scheduling = false;
      updateAction(false);
    }
  }

  action.addEventListener('click', run);
  deepAction.addEventListener('click', () => showActionDialog({
    title: 'Restart for a deep storage check?',
    description: 'Helios will finish active requests, restart after the check, and show the report here. Open file and folder pickers will close.',
    confirmLabel: 'Run deep check',
    onConfirm: queueDeepCheck
  }));
  dialog.addEventListener('settings-panel-change', event => {
    if (event.detail.name === 'storage') refreshMaintenance();
  });
  dialog.addEventListener('close', () => {
    if (!dialog.open && current) stop();
  });
  document.addEventListener('timezone-change', () => {
    if (!timing.hidden) formatResultTime();
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && dialog.open && !$('#settings-storage').hidden) refreshMaintenance();
  });
}
