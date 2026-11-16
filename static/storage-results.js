/* Render status, timestamps, and bounded finding lists for the Storage panel. */

import { refreshIcons, setText } from './ui.js';
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
  error: 'circle-alert',
  info: 'info'
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
  statusText.textContent = finding.label || (finding.status === 'info' && summaryFindings.has(finding.code) ? 'Summary' : label);
  status.append(marker, statusText);
  header.append(path, status);
  description.textContent = finding.message;
  row.append(header, description);
  return row;
}

export function setStorageStatus(status, message, tone = 'info') {
  if (!message) {
    status.replaceChildren();
    delete status.dataset.tone;
  } else if (status.dataset.tone !== tone) {
    const marker = document.createElement('span');
    marker.className = 'storage-status-icon';
    marker.append(statusIcon(noticeIcons[tone]));
    status.replaceChildren(marker, document.createTextNode(message));
    status.dataset.tone = tone;
    refreshIcons(status);
  } else if (status.lastChild.textContent !== message) {
    status.lastChild.textContent = message;
  }
}

export function refreshStorageTime(timing) {
  if (timing.hidden) return;
  const time = timing.querySelector('time');
  setText(time, formatTimestamp(time.dateTime));
  time.title = formatMessageTimestamp(time.dateTime)?.detail || '';
}

export function setStorageTime(timing, label, dateTime) {
  timing.hidden = !dateTime;
  if (timing.hidden) return;
  setText(timing.querySelector('span'), label);
  timing.querySelector('time').dateTime = dateTime;
  refreshStorageTime(timing);
}

export function showStorageResults(region, omitted, findings, omittedCount, noun = 'finding') {
  const list = region.querySelector('ul');
  list.replaceChildren(...findings.map(findingRow));
  refreshIcons(list);
  region.hidden = !list.children.length;
  region.scrollTop = 0;
  omitted.hidden = !omittedCount;
  if (omittedCount) setText(omitted, `${omittedCount} more ${noun}${omittedCount === 1 ? '' : 's'} in the totals above.`);
}
