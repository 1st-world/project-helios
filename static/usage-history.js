/* Render bounded pages of saved API call metadata and prices, keeping unknown values and late responses explicit. */

import { $, refreshIcons } from './ui.js';
import { describeRequestError } from './chat-feedback.js';

const PAGE_SIZE = 25;
const KINDS = { chat: 'Chat', regeneration: 'Regeneration', summary: 'Memory summary', legacy_reply: 'Legacy reply' };
const STATUSES = { completed: 'Completed', incomplete: 'Incomplete', failed: 'Failed', cancelled: 'Cancelled', interrupted: 'Interrupted', in_progress: 'In progress' };
const SOURCES = { live: 'Live call', legacy_message: 'Imported message', legacy_summary: 'Imported summary' };
const BASES = { message_created_at: 'Message creation time', summary_recorded_at: 'Summary recording time' };
const TOKENS = { input_tokens: 'Input tokens', output_tokens: 'Output tokens', total_tokens: 'Total tokens', uncached_input_tokens: 'Ordinary input tokens', cache_read_tokens: 'Cache read tokens', cache_write_tokens: 'Cache write tokens', reasoning_tokens: 'Reasoning tokens' };
const PRICES = { input_price_per_million: 'Standard input', output_price_per_million: 'Standard output', cache_read_price_per_million: 'Standard cache read', cache_write_price_per_million: 'Standard cache write', long_input_price_per_million: 'Long input', long_output_price_per_million: 'Long output', long_cache_read_price_per_million: 'Long cache read', long_cache_write_price_per_million: 'Long cache write' };
const money = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 8 });
const count = value => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : 'Unknown';
const text = value => typeof value === 'string' && value ? value : 'Unknown';
const label = (labels, value) => Object.hasOwn(labels, value) ? labels[value] : text(value);

function definitionList(entries) {
  const list = document.createElement('dl');
  list.className = 'usage-call-fields';
  for (const [name, value] of entries) {
    const group = document.createElement('div');
    const term = document.createElement('dt');
    const description = document.createElement('dd');
    term.textContent = name;
    description.textContent = value;
    group.append(term, description);
    list.append(group);
  }
  return list;
}

function disclosure(title, content) {
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = title;
  details.append(summary, content);
  return details;
}

function cost(usage) {
  if (!usage || !Number.isFinite(usage.estimated_cost) || usage.estimated_cost < 0 || usage.cost_status === 'unavailable') return 'Unknown';
  const qualification = usage.cost_status === 'complete' ? '' : usage.cost_status === 'partial' ? ' (partial)' : ' (completeness unknown)';
  return money.format(usage.estimated_cost) + qualification;
}

function savedPrices(snapshot) {
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) {
    const note = document.createElement('p');
    note.textContent = 'Historical rate snapshot unavailable. The stored cost estimate is preserved.';
    return note;
  }
  const container = document.createElement('div');
  const note = document.createElement('p');
  note.textContent = `Saved rates per 1M tokens. Currency: ${text(snapshot.currency)}. Each blank long-context rate uses the corresponding standard rate; an unset standard rate is unknown.`;
  const entries = Object.entries(PRICES).map(([field, title]) => {
    const value = snapshot[field];
    return [title, value == null ? 'Not configured' : Number.isFinite(value) && value >= 0 ? value.toLocaleString(undefined, { maximumSignificantDigits: 21 }) : 'Unknown'];
  });
  entries.push(['Long-context threshold (tokens)', count(snapshot.long_context_threshold)]);
  container.append(note, definitionList(entries));
  return container;
}

function validateRecords(data) {
  if (!data || !Array.isArray(data.calls) || data.calls.length > PAGE_SIZE + 1
    || !data.calls.every(call => call && typeof call.id === 'string' && call.id && typeof call.kind === 'string'
      && typeof call.status === 'string' && typeof call.source === 'string'
      && (call.usage == null || (typeof call.usage === 'object' && !Array.isArray(call.usage))))
    || new Set(data.calls.map(call => call.id)).size !== data.calls.length) {
    throw new Error('Call history data is incomplete or invalid.');
  }
}

export function createUsageHistory({ timezone, isVisible }) {
  const root = $('#usage-call-history');
  const list = $('#usage-calls-list');
  const message = $('#usage-calls-state');
  const refresh = $('#refresh-usage-calls');
  const previous = $('#usage-calls-previous');
  const next = $('#usage-calls-next');
  let query = null;
  let controller = null;
  let offset = 0;
  let hasPage = false;
  let hasNext = false;

  function updateControls() {
    root.setAttribute('aria-busy', String(Boolean(controller)));
    refresh.disabled = !query || Boolean(controller);
    previous.disabled = Boolean(controller) || !hasPage || offset === 0;
    next.disabled = Boolean(controller) || !hasPage || !hasNext;
  }

  function cancel() {
    controller?.abort();
    controller = null;
    updateControls();
  }

  function clear(state, description) {
    root.dataset.state = state;
    message.textContent = description;
    list.replaceChildren();
    hasPage = false;
    hasNext = false;
    $('#usage-calls-page').textContent = '';
    updateControls();
  }

  function timestamp(value) {
    if (typeof value !== 'string' || !/(Z|[+-]\d{2}:\d{2})$/.test(value)) return 'Unknown';
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? date.toLocaleString(undefined, { timeZone: timezone }) : 'Unknown';
  }

  function renderCall(call) {
    const item = document.createElement('li');
    item.dataset.usageCallId = call.id;
    const details = document.createElement('details');
    details.className = 'usage-call';
    const summary = document.createElement('summary');
    const heading = document.createElement('strong');
    heading.textContent = `${label(KINDS, call.kind)} · ${label(STATUSES, call.status)}`;
    const context = document.createElement('span');
    context.textContent = `${timestamp(call.started_at)} · ${text(call.profile_name || call.deployment)}`;
    summary.append(heading, context);
    const body = document.createElement('div');
    body.className = 'usage-call-body';
    const usage = call.usage || {};
    const entries = Object.entries(TOKENS).map(([field, title]) => [title, count(usage[field])]);
    entries.push(['Estimated cost (USD)', cost(usage)], ['Core usage status', usage.usage_status === 'available' ? 'Reported' : text(usage.usage_status)], ['Cost completeness', text(usage.cost_status)]);
    body.append(definitionList(entries));
    const metadata = [
      ['Call ID', call.id], ['Response ID', text(call.response_id)], ['Conversation ID', text(call.conversation_id)],
      ['Profile ID', text(call.profile_id)], ['Deployment', text(call.deployment)], ['Source', label(SOURCES, call.source)],
      [`Started (${timezone})`, timestamp(call.started_at)], [`Finished (${timezone})`, timestamp(call.finished_at)],
      ['Timestamp basis', call.timestamp_basis ? label(BASES, call.timestamp_basis) : call.source === 'live' ? 'Request start time' : 'Unknown'],
      ['Response time (ms)', count(call.response_time_ms ?? usage.response_time_ms)]
    ];
    for (const [field, title] of Object.entries({ error_code: 'Error code', error_type: 'Error type', incomplete_reason: 'Incomplete reason', interruption_reason: 'Interruption reason' })) {
      if (call[field]) metadata.push([title, text(call[field])]);
    }
    if (call.recovered_at) metadata.push([`Recovered (${timezone})`, timestamp(call.recovered_at)]);
    body.append(disclosure('Call details', definitionList(metadata)), disclosure('Saved prices', savedPrices(call.price_snapshot)));
    details.append(summary, body);
    item.append(details);
    return item;
  }

  async function loadPage(requestedOffset = 0) {
    cancel();
    if (!query || !root.open || !isVisible()) return;
    const current = new AbortController();
    controller = current;
    clear('loading', 'Loading recorded calls…');
    const params = new URLSearchParams(query);
    // One lookahead row identifies the next page without relying on a separate aggregate snapshot.
    params.set('limit', PAGE_SIZE + 1);
    params.set('offset', requestedOffset);
    try {
      const response = await fetch(`/api/usage/calls?${params}`, { signal: current.signal });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const feedback = describeRequestError(data, 'Could not load call history.', `HTTP ${response.status}`);
        throw new Error([feedback.message, ...feedback.details].join('\n'));
      }
      if (controller !== current) return;
      validateRecords(data);
      const calls = data.calls.slice(0, PAGE_SIZE);
      list.replaceChildren(...calls.map(renderCall));
      offset = requestedOffset;
      hasPage = true;
      hasNext = data.calls.length > PAGE_SIZE;
      root.dataset.state = 'ready';
      message.textContent = calls.length ? 'Recorded calls. Expand a call for saved usage and pricing.' : 'No records on this page. Refresh to check the newest records.';
      $('#usage-calls-page').textContent = calls.length ? `${offset + 1}–${offset + calls.length}` : 'No records';
      root.querySelector('.usage-calls-scroll').scrollTop = 0;
    } catch (error) {
      if (controller === current && error.name !== 'AbortError') clear('error', `${error.message} Use Refresh to retry from the newest records.`);
    } finally {
      if (controller === current) {
        controller = null;
        updateControls();
        refreshIcons();
      }
    }
  }

  function select(params, description = 'Open to load recorded calls.') {
    cancel();
    query = params ? params.toString() : null;
    offset = 0;
    clear('empty', description);
    if (query && root.open && isVisible()) loadPage();
  }

  function suspend() {
    cancel();
    clear('empty', 'Open to load recorded calls.');
  }

  function init() {
    refresh.onclick = () => loadPage();
    previous.onclick = () => loadPage(Math.max(0, offset - PAGE_SIZE));
    next.onclick = () => loadPage(offset + PAGE_SIZE);
    root.addEventListener('toggle', () => {
      if (root.open && isVisible()) loadPage();
      else cancel();
    });
    updateControls();
  }

  return { init, select, suspend };
}
