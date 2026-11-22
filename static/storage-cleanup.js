/* Review managed storage, confirm explicit selections, and report removal outcomes. */

import { $, formatSize, setRefreshBusy, setText, toast } from './ui.js';
import { showActionDialog } from './dialogs.js';
import { calendarDate, calendarDayStart, formatTimestamp, formatTimezoneLabel } from './timezone.js';
import { setStorageStatus } from './storage-results.js';

const pageSize = 50;
const selectionLimit = 100;
const storageUrl = '/api/storage';

const categories = {
  cache: {
    label: 'cache entry', plural: 'cache entries', empty: 'No cache entries found.',
    confirmation: 'A removed vocabulary can be downloaded again. Removed diagnostic reports can be replaced by running another check.'
  },
  backups: {
    label: 'backup', empty: 'No settings or profile backups found.', header: 'remove-migration-backups',
    confirmation: 'The selected migration backups will be permanently deleted.'
  },
  conversations: {
    label: 'conversation', empty: 'No saved conversations match this date.', header: 'remove-conversations', paged: true,
    confirmation: 'The selected conversations and their saved memory summaries will be permanently deleted. Recorded usage stays available.'
  },
  attachments: {
    label: 'attachment copy', plural: 'attachment copies', empty: 'No managed attachment copies found.',
    header: 'remove-attachment-copies', paged: true,
    confirmation: 'The selected copies will be removed from Helios storage. Original files stay where they are.'
  }
};

const cacheItems = new Map(Object.entries({
  tokenizer: { label: 'Tokenizer vocabulary', path: 'cache/tokenizer', header: 'remove-tokenizer' },
  basic: { label: 'Basic check result', path: 'diagnostics/results/basic', header: 'remove-check-result' },
  deep: { label: 'Deep check result', path: 'diagnostics/results/deep', header: 'remove-check-result' },
  'recovery-preview': { label: 'Recovery preview' },
  'recovery-copy': { label: 'Recovery copy result' },
  'tokenizer-staging': { label: 'Tokenizer staging files', path: 'cache/staging/tokenizer', header: 'remove-cache-staging' },
  'maintenance-staging': { label: 'Maintenance staging files', path: 'cache/staging/maintenance', header: 'remove-cache-staging' }
}));

const stateNotes = new Map(Object.entries({
  request_in_progress: 'A response is in progress.',
  workspace_in_use: 'The Workspace is in use.',
  memory_update_in_progress: 'A memory update is in progress.',
  work_run_paused: 'A work run is paused.',
  work_run_in_progress: 'A work run is in progress.',
  work_status_unavailable: 'Helios could not verify the work status.',
  in_use: 'Referenced by a conversation or a draft.',
  upload_grace_period: 'Recently uploaded. Helios is allowing time for it to be used.',
  references_unavailable: 'Helios could not verify attachment references.',
  metadata_unavailable: 'Helios could not verify the stored file details.',
  copy_missing: 'The saved copy is missing.',
  copy_unavailable: 'Helios could not access the saved copy.'
}));

const outcomeLabels = new Map(Object.entries({
  removed: ['removed', ''],
  missing: ['already absent', 'Already absent.'],
  changed: ['changed', 'Changed since review. Review it again before removing it.'],
  protected: ['protected', 'Now protected from removal.'],
  unavailable: ['unavailable', 'Helios could not verify the item.'],
  busy: ['busy', 'An operation is using this item.'],
  failed: ['failed', 'Removal failed.'],
  pending_removal: ['pending removal', 'Helios will retry removing the copy.'],
  unknown: ['unconfirmed', 'Helios could not confirm removal.']
}));

function countText(count, noun, plural = `${noun}s`) {
  return `${count} ${count === 1 ? noun : plural}`;
}

function itemKey(kind, item) {
  if (kind === 'backups') return JSON.stringify([item.registry, item.name]);
  return kind === 'attachments' ? item.attachment_id : item.id;
}

function makeRow(kind, item) {
  const cache = kind === 'cache' ? cacheItems.get(item.id) : null;
  if (kind === 'cache' && (!cache || item.state === 'empty')) return null;
  const key = itemKey(kind, item);
  if (typeof key !== 'string' || !key) return null;
  let label;
  let note = '';
  let date;
  let removable = false;
  if (cache) {
    label = cache.label;
    date = item.finished_at;
    if (item.state === 'damaged') note = 'Damaged report.';
    if (item.state === 'protected') note = 'Kept with attachment recovery records.';
    if (item.state === 'busy') note = 'An operation is using this cache.';
    removable = Boolean(cache.path && (
      (item.state === 'available' && item.result_id && cache.header === 'remove-check-result')
      || (item.revision && (item.state === 'available' || (item.state === 'damaged' && cache.header === 'remove-check-result')))
    ));
  } else if (kind === 'backups') {
    label = item.name;
    date = item.modified_at;
    if (item.protected) note = 'Required by a live registry or migration.';
    removable = Boolean(item.protected === false && item.revision);
  } else {
    label = kind === 'conversations' ? item.title || 'Untitled conversation' : item.filename || 'Unnamed attachment';
    date = item.updated_at || item.created_at;
    note = stateNotes.get(item.reason) || '';
    if (item.pending_removal && item.state === 'available') note ||= 'Awaiting removal. You can retry now or let Helios retry.';
    removable = Boolean(item.state === 'available' && item.revision);
  }
  if (!removable) note ||= 'Unavailable for removal.';
  const metadata = [];
  if (Number.isSafeInteger(item.size_bytes) && item.size_bytes >= 0) metadata.push(formatSize(item.size_bytes));
  if (item.files > 1) metadata.push(countText(item.files, 'file'));
  if (date) metadata.push(formatTimestamp(date));
  return { key, label, metadata: metadata.join(' · '), note, removable, item };
}

function makeCandidate(row, index, checked, onSelect) {
  const item = document.createElement('li');
  const label = document.createElement('label');
  const checkbox = document.createElement('input');
  const name = document.createElement('strong');
  const details = document.createElement('span');
  checkbox.type = 'checkbox';
  checkbox.checked = checked;
  checkbox.disabled = !row.removable;
  checkbox.toggleAttribute('data-removable', row.removable);
  checkbox.setAttribute('aria-label', row.label);
  checkbox.addEventListener('change', () => onSelect(row, checkbox.checked));
  name.textContent = row.label;
  details.append(name);
  if (row.metadata || row.note) {
    const description = document.createElement('span');
    description.className = 'field-hint';
    if (row.metadata) description.textContent = row.metadata;
    if (row.note) {
      const reason = document.createElement('span');
      reason.className = 'storage-cleanup-reason';
      reason.textContent = row.note;
      if (row.metadata) description.append(' · ');
      description.append(reason);
      description.id = `storage-cleanup-note-${index}`;
      checkbox.setAttribute('aria-describedby', description.id);
    }
    details.append(description);
  }
  label.append(checkbox, details);
  item.append(label);
  return item;
}

async function removeCache(row) {
  const { path, header } = cacheItems.get(row.key);
  const { result_id: resultId, revision } = row.item;
  const query = new URLSearchParams(resultId ? { result_id: resultId } : { revision });
  const response = await fetch(`${storageUrl}/${path}?${query}`, {
    method: 'DELETE', headers: { 'X-Helios-Maintenance': header }, signal: AbortSignal.timeout(15_000)
  });
  if (response.status === 204) return 'removed';
  if (response.status === 409) {
    const code = String((await response.json().catch(() => null))?.detail?.code ?? '');
    if (code.endsWith('_changed')) return 'changed';
    if (code.endsWith('_busy') || code === 'storage_maintenance_in_progress') return 'busy';
    return 'unavailable';
  }
  return response.status === 403 ? 'unavailable' : 'failed';
}

async function removeRows(kind, chosen, cutoff) {
  if (kind === 'cache') {
    // Shared storage locks mean that cache removals must finish one at a time.
    const results = [];
    for (const row of chosen) results.push(await removeCache(row).catch(() => 'unknown'));
    return results;
  }
  const items = chosen.map(({ item }) => {
    const { id, attachment_id, registry, name, revision } = item;
    if (kind === 'backups') return { registry, name, revision };
    return kind === 'attachments' ? { attachment_id, revision } : { id, revision };
  });
  const response = await fetch(`${storageUrl}/${kind}/remove`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Helios-Maintenance': categories[kind].header },
    body: JSON.stringify(kind === 'conversations' ? { updated_before: cutoff, items } : { items }),
    signal: AbortSignal.timeout(30_000)
  });
  if (response.status === 409) return chosen.map(() => 'busy');
  if (response.status === 403) return chosen.map(() => 'unavailable');
  if (!response.ok) throw new Error('Removal was not confirmed.');
  const report = await response.json();
  const outcomes = new Map(report.items.map(item => [itemKey(kind, item), item.outcome]));
  return chosen.map(({ key }) => outcomeLabels.has(outcomes.get(key)) ? outcomes.get(key) : 'unknown');
}

export function initializeStorageCleanup(settingsDialog) {
  const open = $('#review-storage-cleanup');
  const dialog = $('#storage-cleanup-dialog');
  const title = $('#storage-cleanup-title');
  const category = $('#storage-cleanup-kind');
  const dateFilter = $('#storage-cleanup-date');
  const date = $('#storage-cleanup-before');
  const datePicker = dateFilter.querySelector('[data-date-picker]');
  const timezone = $('#storage-cleanup-timezone');
  const listActions = dialog.querySelector('.storage-cleanup-list-actions');
  const refresh = $('#refresh-storage-cleanup');
  const status = $('#storage-cleanup-status');
  const empty = $('#storage-cleanup-empty');
  const region = $('#storage-cleanup-list');
  const list = region.querySelector('ul');
  const pages = $('#storage-cleanup-pages');
  const range = $('#storage-cleanup-range');
  const previous = $('#storage-cleanup-previous');
  const next = $('#storage-cleanup-next');
  const announcement = $('#storage-cleanup-announcement');
  const outcome = $('#storage-cleanup-outcome');
  const issues = $('#storage-cleanup-issues');
  const selectionCount = $('#storage-cleanup-selection');
  const remove = $('#remove-storage-cleanup');
  const selected = new Map();
  let offset = 0;
  let nextOffset = null;
  let reviewedCutoff = null;
  let request = null;
  let dialogLifetime;
  let confirming = false;
  let removing = false;
  let reviewReady = false;

  function updateSelection() {
    const locked = Boolean(request || confirming || !reviewReady);
    const full = selected.size >= selectionLimit;
    let visible = 0;
    for (const input of list.querySelectorAll('input[data-removable]')) {
      if (input.checked) visible++;
      input.disabled = locked || (full && !input.checked);
    }
    remove.disabled = locked || !selected.size;
    const summary = [`${selected.size} selected`];
    if (visible < selected.size) summary.push(`${visible} on this page`);
    if (full) summary.push('Limit reached');
    setText(selectionCount, summary.join(' · '));
  }

  function updateControls() {
    category.disabled = date.disabled = datePicker.disabled = confirming;
    setRefreshBusy(refresh, Boolean(request?.refreshing), Boolean(request || confirming || listActions.hidden));
    previous.disabled = Boolean(request || confirming || !offset);
    next.disabled = Boolean(request || confirming || nextOffset === null);
    region.setAttribute('aria-busy', String(Boolean(request)));
    updateSelection();
  }

  function setEmpty(message) {
    setText(empty, message);
    empty.hidden = !message;
  }

  function reconcileSelection(rows, complete) {
    const current = new Map(rows.map(row => [row.key, row]));
    let cleared = 0;
    for (const [key, reviewed] of selected) {
      const row = current.get(key);
      if (!row && !complete) continue;
      if (row?.removable && row.item.revision === reviewed.item.revision && row.item.result_id === reviewed.item.result_id) continue;
      selected.delete(key);
      cleared++;
    }
    return cleared;
  }

  function renderRows(rows, total) {
    region.hidden = !rows.length;
    list.replaceChildren(...rows.map((row, index) => makeCandidate(row, index, selected.has(row.key), (candidate, checked) => {
      if (checked) selected.set(candidate.key, candidate);
      else selected.delete(candidate.key);
      updateSelection();
    })));
    region.scrollTop = 0;
    pages.hidden = total === null;
    range.hidden = pages.hidden;
    region.toggleAttribute('data-paged', Boolean(offset || nextOffset !== null));
    const first = offset + 1;
    const last = offset + rows.length;
    const visibleRange = first === last ? String(first) : `${first}–${last}`;
    setText(range, pages.hidden ? '' : rows.length ? `${visibleRange} of ${total}` : '0 items');
  }

  function clearOutcome() {
    setText(announcement, '');
    if (outcome.textContent) setStorageStatus(outcome, '');
    issues.hidden = true;
    issues.replaceChildren();
  }

  function showRemovalDetails(kind, chosen, results) {
    const counts = new Map();
    const notes = [];
    results.forEach((result, index) => {
      counts.set(result, (counts.get(result) || 0) + 1);
      if (result === 'removed') return;
      const row = chosen[index];
      const item = document.createElement('li');
      if (kind === 'backups' || (kind === 'attachments' && row.item.filename)) {
        const filename = document.createElement('code');
        filename.textContent = row.label;
        item.append(filename);
      } else item.append(row.label);
      item.append(` · ${outcomeLabels.get(result)[1]}`);
      notes.push(item);
    });
    const countsText = [...outcomeLabels].filter(([result]) => counts.has(result))
      .map(([result, [label]]) => `${counts.get(result)} ${label}`).join(' · ');
    setStorageStatus(outcome, 'Removal details', 'warning');
    issues.replaceChildren(...notes);
    issues.hidden = !notes.length;
    return `${countsText}.`;
  }

  function clearSelection() {
    selected.clear();
    list.querySelectorAll(':checked').forEach(input => { input.checked = false; });
  }

  async function load({ target = offset, reset = false, refreshing = false } = {}) {
    request?.controller.abort();
    request = null;
    reviewReady = false;
    reviewedCutoff = null;
    const kind = category.value;
    const { paged, empty: emptyMessage } = categories[kind];
    dateFilter.hidden = kind !== 'conversations';
    date.max = calendarDate();
    setText(timezone, `Dates use conversation update times in ${formatTimezoneLabel()}.`);
    const cutoff = kind === 'conversations' && date.validity.valid ? calendarDayStart(date.value) : null;
    listActions.hidden = kind === 'conversations' && !cutoff;
    if (reset) {
      clearSelection();
      offset = 0;
      nextOffset = null;
      renderRows([], null);
      setStorageStatus(status, '');
    }
    setEmpty('');
    if (listActions.hidden) {
      setEmpty(date.value ? 'Choose today or an earlier date.' : 'Choose a date to review older conversations.');
      updateControls();
      return;
    }
    const active = { controller: new AbortController(), refreshing };
    request = active;
    updateControls();
    if (region.hidden) setStorageStatus(status, 'Reviewing storage…', 'running');
    try {
      const query = new URLSearchParams();
      if (paged) {
        query.set('offset', target);
        query.set('limit', pageSize);
      }
      if (cutoff) query.set('updated_before', cutoff);
      const response = await fetch(`${storageUrl}/${kind}${query.size ? `?${query}` : ''}`, {
        cache: 'no-store', signal: AbortSignal.any([active.controller.signal, AbortSignal.timeout(15_000)])
      });
      if (request !== active) return;
      if (response.status === 403 || response.status === 409) {
        setStorageStatus(status, response.status === 403 ? 'Open Helios on its host computer to review storage cleanup.'
          : 'Storage maintenance is in progress. Refresh when it finishes.', 'info');
        return;
      }
      if (!response.ok) throw new Error('Storage review failed.');
      const report = await response.json();
      if (request !== active) return;
      const rows = report.items.map(item => makeRow(kind, item)).filter(Boolean);
      const total = paged ? report.total : rows.length;
      if (!Number.isSafeInteger(total) || total < 0) throw new Error('Invalid review result.');
      if (target && target >= total) {
        return load({ target: Math.max(0, Math.floor((total - 1) / pageSize) * pageSize), refreshing });
      }
      offset = target;
      nextOffset = paged ? report.next_offset ?? null : offset + pageSize < total ? offset + pageSize : null;
      const incomplete = kind === 'backups' && report.incomplete?.length;
      const cleared = reconcileSelection(rows, !incomplete && (!paged || (!offset && nextOffset === null)));
      renderRows(paged ? rows : rows.slice(offset, offset + pageSize), total);
      const warnings = [];
      if (incomplete) warnings.push('Some backups could not be reviewed.');
      if (cleared) warnings.push(`Cleared ${countText(cleared, 'selection')} because ${cleared === 1 ? 'its item' : 'the items'} changed or became unavailable.`);
      setStorageStatus(status, warnings.join(' '), 'warning');
      if (!rows.length) setEmpty(emptyMessage);
      reviewedCutoff = cutoff;
      reviewReady = true;
      return true;
    } catch {
      if (request !== active) return;
      const message = target !== offset ? 'Could not load that page. Try again or use Refresh.'
        : region.hidden ? 'Could not review storage. Use Refresh to try again.'
          : 'Could not refresh this list. Refresh before removing items.';
      setStorageStatus(status, message, 'error');
    } finally {
      if (request === active) {
        request = null;
        updateControls();
      }
    }
  }

  async function confirmRemoval() {
    if (remove.disabled) return;
    const kind = category.value;
    const cutoff = reviewedCutoff;
    const chosen = [...selected.values()];
    const { label, plural, confirmation } = categories[kind];
    const count = countText(chosen.length, label, plural);
    const description = chosen.length > list.querySelectorAll('input:checked').length
      ? `${confirmation} This includes selections from other pages.` : confirmation;
    let feedback;
    const decision = showActionDialog({
      title: `Remove ${count}?`, description, confirmLabel: 'Remove', danger: true,
      signal: dialogLifetime.signal,
      async onConfirm() {
        removing = true;
        try {
          const results = await removeRows(kind, chosen, cutoff).catch(() => chosen.map(() => 'unknown'));
          clearOutcome();
          feedback = results.every(result => result === 'removed')
            ? { message: `Removed ${count}.`, type: 'success' }
            : { message: showRemovalDetails(kind, chosen, results), type: 'warning' };
          const removedIds = chosen.filter((_, index) => ['removed', 'missing'].includes(results[index])).map(row => row.key);
          clearSelection();
          void load();
          document.dispatchEvent(new CustomEvent('storage-cleanup-complete', { detail: { kind, removedIds } }));
        } finally {
          removing = false;
        }
      }
    });
    confirming = true;
    updateControls();
    const accepted = await decision;
    confirming = false;
    if (!dialog.open) return;
    updateControls();
    if (accepted) {
      if (feedback.type === 'warning') outcome.focus();
      else title.focus({ preventScroll: true });
      setText(announcement, feedback.message);
      toast(feedback.message, feedback.type);
    } else if (!remove.disabled) remove.focus({ preventScroll: true });
  }

  function resetReview() {
    clearOutcome();
    void load({ target: 0, reset: true });
  }

  async function page(target) {
    if (await load({ target }) && dialog.open && !region.hidden) region.focus({ preventScroll: true });
  }

  open.addEventListener('click', () => {
    if (dialog.open) return;
    category.value = 'cache';
    date.value = '';
    dialogLifetime = new AbortController();
    dialog.showModal();
    title.focus({ preventScroll: true });
    resetReview();
  });
  category.addEventListener('change', resetReview);
  date.addEventListener('change', resetReview);
  refresh.addEventListener('click', () => {
    clearSelection();
    clearOutcome();
    void load({ refreshing: true });
  });
  previous.addEventListener('click', () => page(Math.max(0, offset - pageSize)));
  next.addEventListener('click', () => { if (nextOffset !== null) page(nextOffset); });
  remove.addEventListener('click', confirmRemoval);
  dialog.addEventListener('close', () => {
    if (dialog.open) return;
    dialogLifetime.abort();
    request?.controller.abort();
    request = null;
    if (settingsDialog.open) open.focus({ preventScroll: true });
  });
  settingsDialog.addEventListener('close', () => {
    if (!settingsDialog.open && dialog.open && !removing) dialog.close();
  });
  document.addEventListener('timezone-change', () => {
    if (!dialog.open || confirming) return;
    if (category.value === 'conversations') resetReview();
    else void load();
  });
  return () => !removing;
}

