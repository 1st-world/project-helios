/* Coordinate ledger aggregates and filtered call history without estimating missing usage or repricing history. */

import { $, refreshIcons } from './ui.js';
import { describeRequestError } from './chat-feedback.js';
import { createUsageHistory } from './usage-history.js';

const TOKEN_FIELDS = ['input_tokens', 'output_tokens', 'total_tokens', 'uncached_input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens'];
const MISSING_FIELDS = ['unknown_usage_calls', 'unknown_cost_calls', 'partial_cost_calls', 'unknown_cost_completeness_calls', 'unknown_timestamp_calls', 'legacy_calls'];
const KIND_LABELS = { chat: 'Chat', regeneration: 'Regeneration', summary: 'Memory summaries', legacy_reply: 'Legacy replies' };
const STATUS_LABELS = { completed: 'Completed', incomplete: 'Incomplete', failed: 'Failed', cancelled: 'Cancelled', interrupted: 'Interrupted', in_progress: 'In progress', legacy: 'Legacy' };
const money = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 8 });
const isCount = value => Number.isSafeInteger(value) && value >= 0;

function validateReport(report) {
  const validTotals = total => total && isCount(total.calls) && typeof total.coverage_incomplete === 'boolean'
    && Number.isFinite(total.known_estimated_cost) && total.known_estimated_cost >= 0
    && TOKEN_FIELDS.every(field => isCount(total[field]) && isCount(total[`unknown_${field}_calls`]) && total[`unknown_${field}_calls`] <= total.calls)
    && MISSING_FIELDS.every(field => isCount(total[field]) && total[field] <= total.calls)
    && ['available', 'partial', 'unavailable'].includes(total.usage_status)
    && ['complete', 'partial', 'unavailable'].includes(total.cost_status)
    && [total.kinds, total.statuses].every(counts => counts && !Array.isArray(counts) && Object.values(counts).every(isCount));
  if (!report || !validTotals(report.totals) || !Array.isArray(report.by_date)
    || !report.by_date.every(row => validTotals(row) && (row.date === null || /^\d{4}-\d{2}-\d{2}$/.test(row.date)))
    || !isCount(report.recording_errors) || !isCount(report.import_errors) || !isCount(report.unknown_timestamp_calls_excluded)
    || typeof report.range_includes_untracked_history !== 'boolean') {
    throw new Error('Recorded usage data is incomplete or invalid. Refresh to retry.');
  }
}

function tokenValue(total, field) {
  const missing = total[`unknown_${field}_calls`];
  if (total.calls && missing === total.calls) return '—';
  return `${total[field].toLocaleString()}${missing ? '\u00a0+\u00a0?' : ''}`;
}

function costValue(total) {
  if (!total.calls || total.unknown_cost_calls === total.calls) return '—';
  return `${money.format(total.known_estimated_cost)}${total.cost_status === 'partial' ? ' (partial)' : ''}`;
}

function completeness(total) {
  if (!total.calls) return 'No recorded calls in this selection.';
  const usage = total.usage_status === 'available' ? 'reported' : total.usage_status;
  return `Core usage: ${usage}. Estimate: ${total.cost_status}.`;
}

function reportNotes(report) {
  const total = report.totals;
  const lines = ['Recorded consumption is separate from current context size. Cache tokens are included in input; reasoning tokens are included in output.'];
  if (TOKEN_FIELDS.some(field => total[`unknown_${field}_calls`] > 0)) {
    lines.push('Values with + ? are known subtotals with unreported tokens; — means no value is known.');
    lines.push(TOKEN_FIELDS.filter(field => total[`unknown_${field}_calls`] > 0)
      .map(field => `${field.replaceAll('_', ' ')}: unreported in ${total[`unknown_${field}_calls`].toLocaleString()} calls`).join('; ') + '.');
  }
  const kinds = Object.entries(total.kinds).map(([kind, count]) => `${KIND_LABELS[kind] || kind}: ${count.toLocaleString()}`);
  const statuses = Object.entries(total.statuses).map(([status, count]) => `${STATUS_LABELS[status] || status}: ${count.toLocaleString()}`);
  if (kinds.length) lines.push(kinds.join(' · '));
  if (statuses.length) lines.push(statuses.join(' · '));
  if (total.unknown_usage_calls) lines.push(`Core usage missing or incomplete for ${total.unknown_usage_calls.toLocaleString()} calls.`);
  if (total.unknown_cost_calls) lines.push(`Cost unavailable for ${total.unknown_cost_calls.toLocaleString()} calls.`);
  if (total.partial_cost_calls) lines.push(`Partial cost for ${total.partial_cost_calls.toLocaleString()} calls: rates or usage details are missing.`);
  if (total.unknown_cost_completeness_calls) lines.push(`Cost completeness unknown for ${total.unknown_cost_completeness_calls.toLocaleString()} calls.`);
  if (total.legacy_calls) lines.push(`${total.legacy_calls.toLocaleString()} imported calls may use message or summary timestamps instead of request start times.`);
  if (report.range_includes_untracked_history) lines.push('This range includes untracked history; earlier or unrecorded calls may be missing.');
  if (total.unknown_timestamp_calls) lines.push(`${total.unknown_timestamp_calls.toLocaleString()} calls have an unknown start date.`);
  if (report.unknown_timestamp_calls_excluded) lines.push(`${report.unknown_timestamp_calls_excluded.toLocaleString()} calls with unknown dates were excluded from this date range.`);
  if (report.recording_errors || report.import_errors) lines.push(`History storage errors: ${report.recording_errors.toLocaleString()} recording, ${report.import_errors.toLocaleString()} import. Totals may omit calls.`);
  if (report.tracking_started_at) lines.push(`Tracking started: ${report.tracking_started_at}.`);
  if (total.cost_status !== 'complete' || total.coverage_incomplete) lines.push('Known estimates do not establish the full usage or Azure bill for this selection.');
  return lines.join('\n');
}

function localDate(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

export function createUsage({ footer, renderFooter }) {
  const dialog = $('#settings-dialog');
  const overview = $('#settings-usage-overview');
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const views = {
    footer: { root: footer, render: renderFooter, refresh: footer.querySelector('[data-usage-refresh]'), controller: null },
    settings: { root: overview, render: render => render(), refresh: $('#refresh-settings-usage'), controller: null }
  };
  let conversationId = null;
  let generating = false;
  let footerVisible = true;
  const history = createUsageHistory({ timezone, isVisible: settingsVisible });

  function settingsVisible() {
    return dialog.open && !$('#settings-usage').hidden;
  }

  function cancel(view) {
    view.controller?.abort();
    view.controller = null;
    view.root.setAttribute('aria-busy', 'false');
    view.refresh.disabled = false;
  }

  function showState(view, message, state) {
    view.render(() => {
      view.root.dataset.state = state;
      view.root.querySelector('[data-usage-state]').textContent = message;
      view.root.querySelector('[data-usage-completeness]').textContent = '';
      view.root.querySelector('[data-usage-notes]').textContent = '';
      view.root.querySelectorAll('[data-usage-field]').forEach(field => {
        field.textContent = ['input_output', 'cache'].includes(field.dataset.usageField) ? '— / —' : '—';
      });
      if (view === views.settings) $('#usage-daily-rows').replaceChildren();
    });
  }

  function renderDaily(rows) {
    const body = $('#usage-daily-rows');
    body.replaceChildren();
    const sorted = [...rows].sort((a, b) => a.date && b.date ? b.date.localeCompare(a.date) : a.date ? -1 : b.date ? 1 : 0);
    for (const total of sorted) {
      const row = document.createElement('tr');
      const values = [total.date || 'Unknown date', total.calls.toLocaleString(), `${tokenValue(total, 'input_tokens')} / ${tokenValue(total, 'output_tokens')}`,
        tokenValue(total, 'total_tokens'), costValue(total), completeness(total)];
      values.forEach((value, index) => {
        const cell = document.createElement(index ? 'td' : 'th');
        if (!index) cell.scope = 'row';
        cell.textContent = value;
        row.append(cell);
      });
      body.append(row);
    }
    if (!rows.length) {
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      cell.colSpan = 6;
      cell.textContent = 'No daily totals recorded in this selection.';
      row.append(cell);
      body.append(row);
    }
  }

  function renderReport(view, report) {
    const total = report.totals;
    const values = {
      calls: total.calls.toLocaleString(), input_output: `${tokenValue(total, 'input_tokens')} / ${tokenValue(total, 'output_tokens')}`,
      total_tokens: tokenValue(total, 'total_tokens'), uncached_input_tokens: tokenValue(total, 'uncached_input_tokens'),
      cache: `${tokenValue(total, 'cache_read_tokens')} / ${tokenValue(total, 'cache_write_tokens')}`,
      reasoning_tokens: tokenValue(total, 'reasoning_tokens'), summaries: (total.kinds.summary || 0).toLocaleString(), cost: costValue(total)
    };
    view.render(() => {
      view.root.dataset.state = 'ready';
      view.root.querySelector('[data-usage-state]').textContent = view === views.settings ? '' : 'Recorded totals. Refresh to include calls still finishing.';
      view.root.querySelector('[data-usage-completeness]').textContent = completeness(total);
      view.root.querySelectorAll('[data-usage-field]').forEach(field => { field.textContent = values[field.dataset.usageField]; });
      view.root.querySelector('[data-usage-notes]').textContent = reportNotes(report);
      if (view === views.settings) renderDaily(report.by_date);
    });
  }

  async function load(view, params) {
    cancel(view);
    const controller = new AbortController();
    view.controller = controller;
    view.root.setAttribute('aria-busy', 'true');
    view.refresh.disabled = true;
    showState(view, 'Loading recorded usage…', 'loading');
    try {
      const response = await fetch(`/api/usage?${params}`, { signal: controller.signal });
      const report = await response.json().catch(() => null);
      if (!response.ok) {
        const error = describeRequestError(report, 'Could not load recorded usage.', `HTTP ${response.status}`);
        throw new Error([error.message, ...error.details].join('\n'));
      }
      if (view.controller !== controller) return;
      validateReport(report);
      renderReport(view, report);
    } catch (error) {
      if (view.controller === controller && error.name !== 'AbortError') showState(view, `${error.message} Use Refresh to retry.`, 'error');
    } finally {
      if (view.controller === controller) {
        view.controller = null;
        view.root.setAttribute('aria-busy', 'false');
        view.refresh.disabled = false;
      }
    }
  }

  function refreshFooter() {
    cancel(views.footer);
    if (!conversationId) {
      showState(views.footer, 'Select a conversation to see recorded usage.', 'empty');
    } else if (footerVisible && !generating) {
      load(views.footer, new URLSearchParams({ conversation_id: conversationId, timezone }));
    }
  }

  function refreshSettings() {
    cancel(views.settings);
    if (!settingsVisible()) {
      history.suspend();
      return;
    }
    const custom = $('#usage-period').value === 'custom';
    $('#usage-date-range').hidden = !custom;
    const start = $('#usage-start-date');
    const end = $('#usage-end-date');
    for (const input of [start, end]) {
      input.required = custom;
      input.setCustomValidity('');
      input.removeAttribute('aria-invalid');
    }
    if (custom && start.value && end.value && end.value < start.value) end.setCustomValidity('End date must be on or after start date.');
    const invalid = custom && [start, end].find(input => !input.checkValidity());
    if (invalid) {
      invalid.setAttribute('aria-invalid', 'true');
      showState(views.settings, 'Choose valid start and end dates, with the end on or after the start.', 'error');
      history.select(null, 'Choose valid dates above to view recorded calls.');
      return;
    }
    const params = new URLSearchParams({ timezone });
    if ($('#usage-scope').value === 'conversation') {
      if (!conversationId) {
        history.select(null, 'Select a conversation or choose All conversations.');
        return showState(views.settings, 'Select a conversation or choose All conversations.', 'empty');
      }
      params.set('conversation_id', conversationId);
    }
    if ($('#usage-kind').value) params.set('kind', $('#usage-kind').value);
    const period = $('#usage-period').value;
    if (custom) {
      params.set('start_date', start.value);
      params.set('end_date', end.value);
    } else if (period !== 'all') {
      const today = new Date();
      params.set('end_date', localDate(today));
      if (period === 'week') today.setDate(today.getDate() - 6);
      params.set('start_date', localDate(today));
    }
    history.select(params);
    load(views.settings, params);
  }

  function selectConversation(id) {
    conversationId = id;
    refreshFooter();
    if ($('#usage-scope').value === 'conversation') refreshSettings();
  }

  function setGenerating(value) {
    generating = value;
    if (value) cancel(views.footer);
    else {
      refreshFooter();
      refreshSettings();
    }
  }

  function setFooterVisible(value) {
    footerVisible = value;
    refreshFooter();
  }

  function init() {
    history.init();
    const today = localDate(new Date());
    $('#usage-start-date').value = today;
    $('#usage-end-date').value = today;
    $('#usage-timezone').textContent = `Dates use call start times in ${timezone}, the browser timezone. Both range endpoints are included.`;
    views.footer.refresh.onclick = refreshFooter;
    views.settings.refresh.onclick = refreshSettings;
    for (const id of ['usage-scope', 'usage-period', 'usage-kind', 'usage-start-date', 'usage-end-date']) $(`#${id}`).addEventListener('change', refreshSettings);

    for (const button of overview.querySelectorAll('[data-date-picker]')) {
      const input = $(`#${button.dataset.datePicker}`);
      if (typeof input.showPicker !== 'function') continue;
      button.classList.remove('hidden');
      input.classList.add('date-picker-input');
      button.onclick = () => {
        input.focus({ preventScroll: true });
        input.showPicker();
      };
    }

    dialog.addEventListener('settings-panel-change', refreshSettings);
    dialog.addEventListener('close', () => {
      cancel(views.settings);
      history.suspend();
    });
    refreshIcons();
  }

  return { init, selectConversation, setGenerating, setFooterVisible };
}
