/* Own one-response exploration consent, paged run activity, and immutable saved evidence views. */

import { $, refreshIcons } from './ui.js';
import { formatMessageTimestamp } from './timezone.js';
import { showActionDialog } from './dialogs.js';

const toolLabels = { helios_project_list_files: 'List files', helios_project_search_text: 'Search text', helios_project_read_file: 'Read file' };
const activeStates = new Set(['preparing', 'model', 'tools', 'ready']);

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function usageText(usage) {
  if (!usage) return 'Usage not yet recorded';
  const total = usage.totals;
  const tokens = total.usage_status === 'unavailable' ? 'tokens unavailable'
    : `${Number(total.total_tokens).toLocaleString()} tokens${total.usage_status === 'partial' ? ' (partial)' : ''}`;
  const cost = total.cost_status === 'unavailable' ? 'cost unavailable'
    : `$${Number(total.known_estimated_cost).toFixed(6)}${total.cost_status === 'complete' ? '' : ' (partial estimate)'}`;
  return `${usage.expected_calls} response calls · ${tokens} · ${cost}${total.coverage_incomplete ? ' · incomplete records' : ''}`;
}

function rangeText(reference) {
  if (reference.start_line == null) return `${reference.path}${reference.kind === 'directory' ? '/' : ''}`;
  return `${reference.path} · ${reference.start_line}:${reference.start_column}–${reference.end_line}:${reference.end_column}`;
}

export function createExploration({ getWorkspace, chooseWorkspace, renderContent = render => render() }) {
  const choice = $('#explore-project');
  const tools = $('#work-tools');
  const settings = $('#exploration-settings-dialog');
  const evidence = $('#exploration-evidence-dialog');
  const region = node('section', null, 'exploration-history');
  region.setAttribute('aria-label', 'Project explorations');
  const cards = new Map();
  let conversationId = null;
  let generation = 0;
  let busy = false;
  let pageLoading = false;
  let nextBefore = null;
  let historyController = null;
  let evidenceController = null;
  let evidenceOpener = null;
  let historyVisibility = true;
  let refreshTimer = null;
  let historyError = null;

  function updateChoice() {
    const workspace = getWorkspace();
    choice.disabled = busy;
    $('#exploration-goals').disabled = busy;
    $('#exploration-scope').textContent = workspace.root ? `Workspace: ${workspace.root}` : 'Choose a workspace from the sidebar before enabling exploration.';

    const count = tools.querySelectorAll('input[data-work-tool]:checked').length;
    const badge = $('#work-tools-count');
    badge.textContent = String(count);
    badge.hidden = count === 0;
    $('#work-tools-trigger').setAttribute('aria-label', count
      ? `Work tools: ${count} ${count === 1 ? 'tool' : 'tools'} selected for the next response` : 'Work tools: no tools selected');
    tools.classList.toggle('tools-selected', count > 0);
  }

  function resetChoice() {
    choice.checked = false;
    updateChoice();
  }

  function limits() {
    const result = {};
    for (const [id, key] of [['call', 'max_model_calls'], ['time', 'elapsed_time_target_seconds'], ['cost', 'cost_goal_usd']]) {
      const input = $(`#exploration-${id}-goal`);
      input.setCustomValidity('');
      if (input.validity.badInput || (input.value !== '' && (!Number.isFinite(input.valueAsNumber) || input.valueAsNumber <= 0
          || (id === 'call' && !Number.isSafeInteger(input.valueAsNumber))))) {
        input.setCustomValidity(id === 'call' ? 'Enter a positive whole number.' : 'Enter a number greater than zero.');
      }
      if (!input.checkValidity()) {
        if (!settings.open) settings.showModal();
        input.reportValidity();
        input.focus();
        throw new Error('Review the exploration goals before sending.');
      }
      if (input.value !== '') result[key] = input.valueAsNumber;
    }
    return result;
  }

  function selection() {
    if (!choice.checked) return {};
    const workspace = getWorkspace();
    if (!workspace.root || workspace.revision == null || !workspace.sessionId) throw new Error('Choose a workspace before exploring.');
    const goals = limits();
    return { explore_project: true, ...(Object.keys(goals).length ? { exploration_limits: goals } : {}) };
  }

  async function json(url, signal) {
    const response = await fetch(url, { signal });
    if (!response.ok) throw new Error(`Saved exploration could not be loaded (HTTP ${response.status}).`);
    return response.json();
  }

  function base(id = conversationId) {
    return `/api/conversations/${encodeURIComponent(id)}/work-runs`;
  }

  function button(label, action) {
    const control = node('button', label, 'quiet-btn');
    control.type = 'button';
    control.onclick = action;
    return control;
  }

  function attach() {
    if (!region.isConnected) $('#chat').append(region);
  }

  function statusText(run) {
    const status = activeStates.has(run.status) ? 'In progress' : run.status;
    return `Explore project · ${status}${run.adopted ? ' · answer saved' : run.status === 'completed' ? ' · answer outside current branch' : ''}`;
  }

  function updateCard(card) {
    card.heading.textContent = card.phase || statusText(card.run);
    card.usage.textContent = usageText(card.run.usage);
    card.usage.hidden = !historyVisibility;
  }

  async function openEvidence(runId, step, opener) {
    evidenceController?.abort();
    const controller = new AbortController();
    evidenceController = controller;
    evidenceOpener = opener;
    const target = $('#exploration-evidence-content');
    target.replaceChildren(node('p', 'Loading saved result…'));
    if (!evidence.open) evidence.showModal();
    const id = conversationId;
    try {
      const query = new URLSearchParams({ response_id: step.response_id, call_id: step.call_id });
      const result = await json(`${base(id)}/${encodeURIComponent(runId)}/evidence?${query}`, controller.signal);
      if (controller !== evidenceController || !evidence.open || id !== conversationId) return;
      target.replaceChildren();
      const references = node('ul', null, 'evidence-references');
      for (const reference of result.references) {
        const item = node('li', rangeText(reference));
        if (reference.file_version) item.append(node('small', `File revision: ${reference.file_version}`));
        if (reference.byte_start != null) item.append(node('small', `Bytes ${reference.byte_start}–${reference.byte_end} (end exclusive)`));
        references.append(item);
      }
      if (references.childElementCount) {
        target.append(node('p', 'Lines are 1-based; Unicode columns are 0-based. End positions are exclusive.'), references);
      }
      let payload;
      try { payload = JSON.parse(result.output); } catch { payload = null; }
      if (payload) {
        target.append(node('p', `Scope: ${payload.scope ?? 'not available'} · ${payload.complete === true ? 'Page sequence ended' : 'Partial result'}${payload.next_cursor ? ' · continuation available' : ''}`));
        target.append(node('p', `Omissions on this page: ${JSON.stringify(payload.omissions || {})}. Accumulated omissions: ${JSON.stringify(payload.omission_totals || {})}.`));
        for (const item of payload.items || []) {
          if (typeof item.text !== 'string') continue;
          target.append(node('p', `${item.path || payload.scope || ''} · line ${item.line}, column ${item.snippet_column ?? item.column}${item.snippet_is_partial ? ' · partial snippet' : ''}`), node('pre', item.text));
        }
      }
      const raw = node('details');
      raw.append(node('summary', 'Original result (JSON): coverage, omissions and continuations'), node('pre', result.output));
      target.append(raw);
    } catch (error) {
      if (error.name !== 'AbortError' && controller === evidenceController) target.replaceChildren(node('p', error.message), button('Retry', () => openEvidence(runId, step, opener)));
    }
  }

  async function loadActivity(card, offset = 0) {
    if (card.loading) return;
    card.loading = true;
    card.more?.remove();
    card.error?.remove();
    const loading = node('p', 'Loading activity…');
    card.body.append(loading);
    const version = generation;
    try {
      const data = await json(`${base()}/${encodeURIComponent(card.run.id)}?offset=${offset}`, historyController?.signal);
      if (version !== generation) return;
      card.run = { ...card.run, ...data.run, usage: data.usage };
      updateCard(card);
      if (offset === 0) card.body.replaceChildren();
      const workspace = card.run.workspace_name || 'Workspace unavailable';
      if (offset === 0) {
        const stamp = formatMessageTimestamp(card.run.started_at)?.detail || 'Time unknown';
        card.body.append(node('p', `${workspace} · ${card.run.profile.name || card.run.profile.deployment || 'Profile unavailable'} · ${stamp}`));
        const goals = card.run.goals;
        card.body.append(node('p', `Goals: ${goals.max_model_calls == null ? 'no response-call limit' : `${goals.max_model_calls} response calls`}; ${goals.elapsed_time_target_seconds == null ? 'no time target' : `${goals.elapsed_time_target_seconds}s target`}; ${goals.cost_goal_usd == null ? 'no cost goal' : `$${goals.cost_goal_usd} estimated cost goal`}.`));
        if (card.run.reason) card.body.append(node('p', `Stop reason: ${card.run.reason}`));
      }
      for (const step of data.steps) {
        const row = node('div', null, 'exploration-step');
        row.append(node('strong', `${step.kind === 'model' ? 'Model response' : toolLabels[step.name] || 'Unsupported tool'} · ${step.status}`));
        if (step.kind === 'model') {
          const usage = step.record?.usage;
          const cost = usage?.estimated_cost;
          const detail = node('p', `Input / output: ${usage?.input_tokens ?? 'unknown'} / ${usage?.output_tokens ?? 'unknown'} · Total: ${usage?.total_tokens ?? 'unknown'} (${usage?.usage_status || 'completeness unknown'}) · Cache read / write: ${usage?.cache_read_tokens ?? 'unknown'} / ${usage?.cache_write_tokens ?? 'unknown'} · Reasoning: ${usage?.reasoning_tokens ?? 'unknown'} · Time: ${Number.isFinite(usage?.response_time_ms) ? `${(usage.response_time_ms / 1000).toFixed(1)}s` : 'unknown'} · Estimated cost: ${Number.isFinite(cost) ? `$${cost.toFixed(6)} (${usage.cost_status || 'completeness unknown'})` : 'unavailable'}`, 'exploration-call-usage');
          detail.hidden = !historyVisibility;
          row.append(detail);
        } else if (step.result_available) {
          const open = button('Open saved result', () => openEvidence(card.run.id, step, open));
          row.append(open);
        }
        card.body.append(row);
      }
      if (data.next_offset != null) {
        card.more = button('Show more activity', () => loadActivity(card, data.next_offset));
        card.body.append(card.more);
      }
      card.loaded = true;
    } catch (error) {
      if (error.name !== 'AbortError' && version === generation) {
        card.more = button('Retry activity', () => loadActivity(card, offset));
        card.error = node('p', error.message);
        card.body.append(card.error, card.more);
      }
    } finally {
      loading.remove();
      card.loading = false;
    }
  }

  function addCard(run, prepend = false, live = false) {
    if (cards.has(run.id)) {
      const card = cards.get(run.id);
      card.run = run;
      if (live) card.live = true;
      if (!activeStates.has(run.status)) card.phase = null;
      updateCard(card);
      return card;
    }
    const details = node('details', null, 'exploration-run');
    const summary = node('summary');
    const heading = node('strong');
    const usage = node('span', null, 'exploration-run-usage');
    const body = node('div', null, 'exploration-run-body');
    summary.append(heading, usage);
    details.append(summary, body);
    const card = { details, heading, usage, body, run, live, loaded: false, loading: false };
    details.addEventListener('toggle', () => { if (details.open && !card.loaded) loadActivity(card); });
    cards.set(run.id, card);
    updateCard(card);
    const active = !live && [...cards.values()].find(other => other !== card && other.live);
    if (prepend) region.prepend(details);
    else if (active) region.insertBefore(details, active.details);
    else region.append(details);
    return card;
  }

  async function loadPage(before = null) {
    if (!conversationId || pageLoading) return;
    pageLoading = true;
    const version = generation;
    region.querySelector(':scope > button')?.remove();
    historyError?.remove();
    const status = node('p', 'Loading project explorations…');
    region.append(status);
    try {
      const data = await json(`${base()}${before == null ? '' : `?before=${before}`}`, historyController.signal);
      if (version !== generation) return;
      if (before == null) [...data.runs].reverse().forEach(run => addCard(run));
      else data.runs.forEach(run => addCard(run, true));
      nextBefore = data.next_before;
      if (nextBefore != null) region.prepend(button('Show older explorations', () => loadPage(nextBefore)));
    } catch (error) {
      if (error.name !== 'AbortError' && version === generation) {
        historyError = node('p', error.message);
        region.append(historyError, button('Retry explorations', () => loadPage(before)));
      }
    } finally {
      status.remove();
      if (version === generation) {
        pageLoading = false;
        clearTimeout(refreshTimer);
        if ([...cards.values()].some(card => activeStates.has(card.run.status))) refreshTimer = setTimeout(() => loadPage(), 1500);
      }
    }
  }

  function show(id) {
    generation++;
    clearTimeout(refreshTimer);
    historyController?.abort();
    historyController = new AbortController();
    evidenceController?.abort();
    if (evidence.open) evidence.close();
    conversationId = id;
    cards.clear();
    region.replaceChildren();
    pageLoading = false;
    if (id) { attach(); loadPage(); } else region.remove();
  }

  function updateProgress(event) {
    if (!event.run_id) return;
    if (event.type === 'start') {
      resetChoice();
      if (conversationId !== event.conversation_id) show(event.conversation_id);
      addCard({ id: event.run_id, status: 'preparing', goals: {}, profile: {} }, false, true);
      attach();
    }
    const card = cards.get(event.run_id);
    if (!card) return;
    if (event.type === 'work_progress') {
      card.phase = `Explore project · ${event.phase === 'model' ? 'Model responding' : `${toolLabels[event.name] || 'Tool'}${event.phase === 'tool_completed' ? event.failed ? ' failed' : ' complete' : ' in progress'}`}`;
      card.loaded = false;
    }
    if (event.type === 'work_usage') {
      card.phase = 'Explore project · Response recorded';
      card.loaded = false;
      loadPage();
    }
    if (event.type === 'run_usage') card.run.usage = event.usage;
    if (event.type === 'error' || event.type === 'done') {
      card.run.status = event.run_status || (event.type === 'done' ? 'completed' : 'failed');
      card.run.adopted = event.type === 'done';
      card.run.usage = event.run_usage || card.run.usage;
      card.phase = null;
      card.live = false;
    }
    updateCard(card);
    if (card.details.open && !card.loading && !card.loaded) loadActivity(card);
  }

  tools.addEventListener('change', async event => {
    if (!event.target.matches('input[data-work-tool]')) return;
    if (event.target === choice && choice.checked && !getWorkspace().root) {
      resetChoice();
      tools.open = false;
      const accepted = await showActionDialog({ title: 'Choose a workspace?',
        description: 'Explore project needs a workspace to find and read project files. Would you like to choose one?',
        confirmLabel: 'Choose workspace' });
      if (accepted && !busy && await chooseWorkspace() && !busy) choice.checked = true;
      updateChoice();
      $('#work-tools-trigger').focus({ preventScroll: true });
      return;
    }
    updateChoice();
  });

  $('#explore-settings').onclick = () => { tools.open = false; updateChoice(); settings.showModal(); };
  $('#exploration-goals-form').onsubmit = event => {
    event.preventDefault();
    try { limits(); settings.close(); } catch { /* Native validation identifies the field. */ }
  };
  settings.querySelectorAll('input').forEach(input => input.addEventListener('input', () => input.setCustomValidity('')));
  settings.addEventListener('close', () => { if (!settings.open) $('#work-tools-trigger').focus({ preventScroll: true }); });
  document.addEventListener('workspace-change', event => { if (event.detail.changed) resetChoice(); else updateChoice(); });
  evidence.addEventListener('close', () => {
    if (evidence.open) return;
    evidenceController?.abort();
    evidenceController = null;
    $('#exploration-evidence-content').replaceChildren();
    if (evidenceOpener?.isConnected) evidenceOpener.focus({ preventScroll: true });
  });
  updateChoice();
  refreshIcons();

  return {
    selection, resetChoice, show,
    progress(event) { renderContent(() => updateProgress(event)); },
    describeSelection: () => choice.checked ? 'Explore project is selected for this response.' : 'Work tools are off for this response.',
    setGenerating(value) { busy = value; updateChoice(); },
    setUsageVisibility(value) {
      historyVisibility = value;
      region.querySelectorAll('.exploration-run-usage, .exploration-call-usage').forEach(element => { element.hidden = !value; });
    }
  };
}
