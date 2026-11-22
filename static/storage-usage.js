/* Measure Storage usage on demand and render a breakdown of measured bytes. */

import { $, formatSize, setRefreshBusy, setText } from './ui.js';
import { refreshStorageTime, setStorageStatus, setStorageTime } from './storage-results.js';

const categories = new Map([
  ['conversations', 'Conversations'],
  ['attachments', 'Attachment copies'],
  ['app_data', 'App data'],
  ['usage_records', 'Model usage records'],
  ['logs', 'Logs & recovery records'],
  ['cache', 'Cache'],
  ['backups', 'Settings & profile backups']
]);
const shareNumber = new Intl.NumberFormat('en', { style: 'percent', maximumFractionDigits: 1 });

function formatShare(value) {
  if (value > 0 && value < 0.001) return '<0.1%';
  if (value > 0.999 && value < 1) return '>99.9%';
  return shareNumber.format(value);
}

function categoryRow(row, total) {
  const item = document.createElement('li');
  const caption = document.createElement('div');
  caption.className = 'storage-usage-category-caption';
  const name = document.createElement('span');
  name.textContent = categories.get(row.id) || row.id;
  const value = document.createElement('span');
  value.className = 'storage-usage-category-value';
  const share = total ? row.bytes / total : 0;
  const unavailable = row.skipped > 0 && row.files === 0;
  value.textContent = unavailable ? 'Not measured'
    : total ? `${formatSize(row.bytes)} · ${formatShare(share)}` : formatSize(row.bytes);
  caption.append(name, value);
  item.append(caption);
  const track = document.createElement('span');
  track.className = 'storage-usage-track';
  track.setAttribute('aria-hidden', 'true');
  track.toggleAttribute('data-unmeasured', unavailable);
  if (row.bytes && total && !unavailable) {
    const bar = document.createElement('span');
    bar.style.width = `${share * 100}%`;
    track.append(bar);
  }
  item.append(track);
  if (row.skipped && !unavailable) {
    const hint = document.createElement('span');
    hint.className = 'field-hint';
    hint.textContent = 'Some files or locations could not be measured.';
    item.append(hint);
  }
  return item;
}

function validReport(report) {
  if (!report || typeof report.complete !== 'boolean' || !Number.isFinite(Date.parse(report.measured_at))
    || !Number.isSafeInteger(report.total_bytes) || report.total_bytes < 0 || !Array.isArray(report.categories)) return false;
  return report.categories.every(row => row && typeof row.id === 'string'
    && ['bytes', 'files', 'skipped'].every(key => Number.isSafeInteger(row[key]) && row[key] >= 0))
    && report.total_bytes === report.categories.reduce((sum, row) => sum + row.bytes, 0);
}

export function initializeStorageUsage(dialog) {
  const refresh = $('#refresh-storage-usage');
  const status = $('#storage-usage-status');
  const announcement = $('#storage-usage-announcement');
  const result = $('#storage-usage-result');
  const coverage = $('#storage-usage-coverage');
  const total = $('#storage-usage-total');
  const time = $('#storage-usage-time');
  const empty = $('#storage-usage-empty');
  const list = $('#storage-usage-categories');
  let report = null;
  let request = null;
  let shouldMeasure = true;

  function renderReport(measured) {
    const changed = !report || measured.complete !== report.complete
      || JSON.stringify(measured.categories) !== JSON.stringify(report.categories);
    report = measured;
    result.hidden = false;
    setText(total, formatSize(report.total_bytes));
    setStorageTime(time, 'Measured', report.measured_at);
    coverage.hidden = report.complete;
    const warning = report.complete ? '' : 'Partial measurement. The total and shares reflect only the files counted.';
    setStorageStatus(coverage, warning, 'warning');
    if (!changed) return;
    const isEmpty = report.complete && report.categories.every(row => !row.files);
    empty.hidden = !isEmpty;
    list.hidden = isEmpty;
    list.replaceChildren(...(isEmpty ? [] : report.categories.map(row => categoryRow(row, report.total_bytes))));
  }

  function stop() {
    if (!request) return;
    request.abort();
    request = null;
    shouldMeasure = true;
    setRefreshBusy(refresh, false);
    if (status.dataset.tone === 'running') setStorageStatus(status, '');
  }

  async function measure() {
    if (request) return;
    shouldMeasure = false;
    const active = new AbortController();
    request = active;
    setRefreshBusy(refresh, true);
    setText(announcement, '');
    if (!report && !status.textContent) setStorageStatus(status, 'Measuring storage usage…', 'running');
    try {
      const response = await fetch('/api/storage/usage', { cache: 'no-store', signal: active.signal });
      if (request !== active) return;
      if (response.status === 403) {
        setStorageStatus(status, 'Open Helios on its host computer to measure storage usage.', 'info');
        return;
      }
      if (response.status === 409) {
        setStorageStatus(status, report ? 'A measurement is already running. Showing the previous result.'
          : 'A measurement is already running. Try again shortly.', 'info');
        return;
      }
      if (!response.ok) throw new Error('Storage usage measurement failed.');
      const measured = await response.json();
      if (request !== active) return;
      if (!validReport(measured)) throw new Error('Invalid storage usage report.');
      renderReport(measured);
      setStorageStatus(status, '');
      if (report.complete) setText(announcement, `Storage usage measured: ${formatSize(report.total_bytes)}.`);
    } catch {
      if (request === active) setStorageStatus(status, report ? 'Could not refresh storage usage. Showing the previous result.'
        : 'Could not measure storage usage. Use Refresh to try again.', 'error');
    } finally {
      if (request === active) {
        request = null;
        setRefreshBusy(refresh, false);
        if (shouldMeasure && dialog.open && !$('#settings-storage').hidden) void measure();
      }
    }
  }

  refresh.addEventListener('click', measure);
  dialog.addEventListener('settings-panel-change', event => {
    if (event.detail.name !== 'storage') stop();
    else if (dialog.open && shouldMeasure) void measure();
  });
  dialog.addEventListener('close', () => {
    if (dialog.open) return;
    stop();
    shouldMeasure = true;
  });
  document.addEventListener('storage-cleanup-complete', () => {
    if (dialog.open && !$('#settings-storage').hidden && !request) void measure();
    else shouldMeasure = true;
  });
  document.addEventListener('timezone-change', () => refreshStorageTime(time));
}
