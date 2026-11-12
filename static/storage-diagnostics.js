/* Run the basic storage check from Settings and render its read-only report. */

import { $, refreshIcons, setText } from './ui.js';

const findingPresentation = {
  ok: ['Passed', 'check'],
  compatible_legacy: ['Compatible older format', 'info'],
  not_initialized: ['No saved data', 'circle-dashed'],
  info: ['Information', 'info'],
  review: ['Review', 'triangle-alert'],
  unavailable: ['Deferred', 'clock'],
  invalid_data: ['Problem', 'circle-alert'],
  unsupported_version: ['Unsupported format', 'circle-alert'],
  missing_reference: ['Missing reference', 'circle-alert']
};

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
  header.append(path);
  if (finding.code !== 'conversations_read') {
    const [label, icon] = findingPresentation[finding.status] || findingPresentation.info;
    const status = document.createElement('span');
    status.className = 'storage-finding-status';
    status.dataset.status = finding.status;
    status.append(statusIcon(icon), label);
    header.append(status);
  }
  description.textContent = finding.message;
  row.append(header, description);
  return row;
}

function describeReport({ problems, review, deferred }) {
  const notices = [];
  if (problems) notices.push(`${problems} ${problems === 1 ? 'problem' : 'problems'} found`);
  if (review) notices.push(`${review} ${review === 1 ? 'item needs' : 'items need'} review`);
  if (deferred) notices.push(`${deferred} ${deferred === 1 ? 'check was' : 'checks were'} deferred`);
  return notices.length ? `${notices.join(' · ')}.` : 'Basic check passed.';
}

export function initializeStorageDiagnostics(dialog) {
  const action = $('#run-storage-check');
  const status = $('#storage-check-status');
  const results = $('#storage-check-results');
  const list = results.querySelector('ul');
  const omitted = $('#storage-check-omitted');
  let current = null;

  function showStatus(message, tone) {
    status.replaceChildren(statusIcon(noticeIcons[tone]), message);
    status.dataset.tone = tone;
    refreshIcons(status);
  }

  function updateAction(running) {
    setText(action.querySelector('span'), running ? 'Stop check' : 'Run basic check');
    action.querySelector('[data-storage-action="check"]').toggleAttribute('hidden', running);
    action.querySelector('[data-storage-action="stop"]').toggleAttribute('hidden', !running);
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
    updateAction(true);
    results.hidden = true;
    omitted.hidden = true;
    list.replaceChildren();
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
      showStatus(describeReport(report.summary), tone);
    } catch {
      if (current === controller) showStatus('Could not run the storage check. Try again.', 'error');
    } finally {
      if (current === controller) {
        current = null;
        updateAction(false);
      }
    }
  }

  action.addEventListener('click', run);
  dialog.addEventListener('close', () => {
    if (!dialog.open && current) stop();
  });
}
