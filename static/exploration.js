/* Own one-response exploration consent, paged run activity, and immutable saved evidence views. */

import { $, refreshIcons } from './ui.js';
import { formatMessageTimestamp } from './timezone.js';
import { showActionDialog } from './dialogs.js';
import { renderExplorationEvidence } from './exploration-evidence.js';
import { createDisclosure } from './disclosure.js';
import { usageView } from './response-usage.js';

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

export function createExploration({ getWorkspace, chooseWorkspace, getLimits = () => ({}), renderContent = render => render() }) {
  const choice = $('#explore-project');
  const tools = $('#work-tools');
  const evidence = $('#exploration-evidence-dialog');
  const region = node('section', null, 'exploration-history exploration-records');
  region.setAttribute('aria-label', 'Exploration history');
  const historyHeading = node('h3', 'Other explorations', 'exploration-history-title');
  const historyHint = node('p', 'These saved attempts have no matching question or answer in the current conversation.', 'exploration-history-hint');
  const cards = new Map();
  const messageRegions = new Map();
  let messageTargets = [];
  let renderAnchor = null;
  let focusedControl = null;
  let conversationId = null;
  let generation = 0;
  let busy = false;
  let pageLoading = false;
  let nextBefore = null;
  let historyController = null;
  let evidenceController = null;
  let evidenceOpener = null;
  let evidenceDisclosures = [];
  let historyVisibility = true;
  let refreshTimer = null;
  let historyError = null;
  let historyMore = null;
  let historyRetry = null;
  let historyPaged = false;

  function updateChoice() {
    choice.disabled = busy;

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

  function selection() {
    if (!choice.checked) return {};
    const workspace = getWorkspace();
    if (!workspace.root || workspace.revision == null || !workspace.sessionId) throw new Error('Choose a workspace before exploring.');
    const goals = getLimits();
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

  function linkedMessage(card) {
    const answers = messageTargets.filter(message => message.role === 'assistant' && message.run_id === card.run.id);
    if (answers.length > 1) return null;
    if (answers.length === 1 && answers[0].element.isConnected) {
      const source = messageTargets.find(message => message.index === answers[0].index - 1 && message.role === 'user');
      if (source) card.sourceIdentity = { index: source.index, created_at: source.created_at, content: source.content };
      return answers[0].element;
    }
    const live = card.targets;
    if (card.run.adopted && live?.assistant?.isConnected) return live.assistant;
    if (live?.user?.isConnected) return live.user;
    const index = card.run.message_link?.user_index;
    const source = messageTargets.find(message => message.index === index && message.role === 'user');
    if (!source?.element.isConnected) return null;
    // An index can survive a question edit or be reused after branch replacement.
    if (card.sourceIdentity && (card.sourceIdentity.index !== source.index
        || card.sourceIdentity.created_at !== source.created_at || card.sourceIdentity.content !== source.content)) return null;
    card.sourceIdentity = { index: source.index, created_at: source.created_at, content: source.content };
    return source.element;
  }

  function placeCards() {
    const destinations = new Map();
    const ordered = [...cards.values()].sort((left, right) =>
      (Date.parse(left.run.started_at) || Infinity) - (Date.parse(right.run.started_at) || Infinity));
    for (const card of ordered) {
      const message = linkedMessage(card);
      let destination = region;
      if (message) {
        if (!messageRegions.has(message)) {
          const group = node('section', null, 'exploration-message exploration-records');
          group.setAttribute('aria-label', message.classList.contains('assistant')
            ? 'Project explorations for this answer' : 'Project explorations for this question');
          message.querySelector('.message-body').append(group);
          messageRegions.set(message, group);
        }
        destination = messageRegions.get(message);
      }
      if (!destinations.has(destination)) destinations.set(destination, []);
      destinations.get(destination).push(card);
    }
    for (const [destination, group] of destinations) {
      group.forEach((card, index) => {
        const sibling = destination.querySelectorAll(':scope > .exploration-run')[index];
        if (sibling === card.details) return;
        const focused = document.activeElement;
        const hadFocus = focused && card.details.contains(focused);
        destination.insertBefore(card.details, sibling || null);
        if (hadFocus) focused.focus({ preventScroll: true });
      });
    }
    for (const [message, group] of messageRegions) {
      if (!group.childElementCount || !message.isConnected) {
        group.remove();
        messageRegions.delete(message);
      }
    }
    if (destinations.has(region)) {
      if (!historyHeading.isConnected) region.prepend(historyHeading, historyHint);
    } else {
      historyHeading.remove();
      historyHint.remove();
    }
    attach();
  }

  function statusText(run) {
    const status = activeStates.has(run.status) ? 'In progress' : run.status;
    return `Work activity · ${status}${run.adopted ? '' : run.status === 'completed' ? ' · answer outside current branch' : ''}`;
  }

  function updateCard(card) {
    card.heading.textContent = card.phase || statusText(card.run);
    const stamp = formatMessageTimestamp(card.run.started_at);
    card.context.textContent = card.run.adopted ? 'Explore project'
      : ['Explore project', card.run.profile?.name || card.run.profile?.deployment, stamp?.label].filter(Boolean).join(' · ');
    card.context.hidden = !card.context.textContent;
    if (stamp) card.context.title = `Exploration started: ${stamp.detail}`;
    card.usage.textContent = usageText(card.run.usage);
    card.usage.hidden = !historyVisibility || card.run.adopted === true;
  }

  function addFact(list, label, value) {
    const row = node('div');
    row.append(node('dt', label), node('dd', value));
    list.append(row);
  }

  function runSettings(card) {
    const section = node('section', null, 'exploration-run-settings');
    section.append(node('h4', 'Run settings'));
    const facts = node('dl', null, 'exploration-run-facts');
    const goals = card.run.goals || {};
    addFact(facts, 'Tools', 'Explore project');
    addFact(facts, 'Workspace', card.run.workspace_name || 'Unavailable');
    addFact(facts, 'Model call limit', goals.max_model_calls == null ? 'No limit' : String(goals.max_model_calls));
    addFact(facts, 'Time target', goals.elapsed_time_target_seconds == null ? 'Not set' : `${goals.elapsed_time_target_seconds}s`);
    addFact(facts, 'Cost target', goals.cost_goal_usd == null ? 'Not set' : `$${goals.cost_goal_usd}`);
    if (card.run.reason) addFact(facts, 'Stop reason', card.run.reason);
    section.append(facts);
    return section;
  }

  function appendStep(card, step, position) {
    const key = `${step.kind}:${step.response_id ?? ''}:${step.call_id ?? position}`;
    const signature = JSON.stringify([step.name, step.status, step.record?.usage, step.result_available]);
    const previous = card.stepViews.get(key);
    if (previous?.signature === signature) {
      card.steps.append(previous.row);
      return;
    }
    const expanded = previous?.details?.open === true;
    previous?.controller?.destroy();
    const row = previous?.row || node('div', null, 'exploration-step');
    row.replaceChildren();
    const heading = node('div', null, 'exploration-step-heading');
    heading.append(node('strong', step.kind === 'model' ? 'Model response' : toolLabels[step.name] || 'Unsupported tool'),
      node('span', step.status, 'exploration-step-status'));
    row.append(heading);
    const view = { row, signature };
    if (step.kind === 'model') {
      const usage = usageView(step.record?.usage || { usage_status: 'unavailable', cost_status: 'unavailable' });
      const details = node('details', null, 'exploration-call-usage');
      details.open = expanded;
      details.hidden = !historyVisibility;
      const summary = node('summary');
      const metrics = node('span', null, 'exploration-call-metrics');
      for (const item of usage.summary) metrics.append(node('span', item.text, `exploration-call-metric${item.cost ? ' exploration-call-cost' : ''}`));
      const toggle = node('span', 'Details', 'exploration-call-toggle');
      const chevron = node('i');
      chevron.setAttribute('data-lucide', 'chevron-down');
      chevron.setAttribute('aria-hidden', 'true');
      toggle.append(chevron);
      summary.append(metrics, toggle);
      const body = node('div', null, 'exploration-call-body');
      const fields = node('dl', null, 'exploration-call-fields');
      for (const field of usage.rows) addFact(fields, field.label, field.value);
      if (usage.longTier) addFact(fields, 'Pricing tier', 'Long tier');
      body.append(fields);
      for (const hint of usage.hints) body.append(node('p', hint, 'exploration-call-hint'));
      details.append(summary, body);
      row.append(details);
      card.steps.append(row);
      view.details = details;
      view.controller = createDisclosure(details, body);
    } else {
      if (step.result_available) {
        const open = button('Open saved result', () => openEvidence(card.run.id, step, open));
        row.append(open);
      }
      card.steps.append(row);
    }
    card.stepViews.set(key, view);
    refreshIcons(row);
  }

  function clearEvidence() {
    evidenceDisclosures.forEach(controller => controller.destroy());
    evidenceDisclosures = [];
    $('#exploration-evidence-content').replaceChildren();
  }

  async function openEvidence(runId, step, opener) {
    evidenceController?.abort();
    clearEvidence();
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
      renderExplorationEvidence(target, result, step.name);
      refreshIcons(target);
      for (const details of target.querySelectorAll('.evidence-details')) {
        evidenceDisclosures.push(createDisclosure(details, details.querySelector('.evidence-details-body')));
      }
    } catch (error) {
      if (error.name !== 'AbortError' && controller === evidenceController) {
        clearEvidence();
        target.replaceChildren(node('p', error.message), button('Retry', () => openEvidence(runId, step, opener)));
      }
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
    const revision = card.activityRevision || 0;
    const token = {};
    card.activityToken = token;
    card.loadingNode = loading;
    try {
      const data = await json(`${base()}/${encodeURIComponent(card.run.id)}?offset=${offset}`, historyController?.signal);
      if (version !== generation || card.activityToken !== token || revision !== (card.activityRevision || 0)) return;
      card.run = { ...card.run, ...data.run, usage: data.usage };
      updateCard(card);
      card.sourceIdentity = null;
      placeCards();
      if (offset === 0) {
        card.steps = node('div', null, 'exploration-steps');
        const activity = node('section', null, 'exploration-run-activity');
        activity.append(node('h4', 'Activity'), card.steps);
        card.body.replaceChildren(runSettings(card), activity);
      }
      data.steps.forEach((step, index) => appendStep(card, step, offset + index));
      if (data.next_offset != null) {
        card.more = button('Show more activity', () => loadActivity(card, data.next_offset));
        card.body.append(card.more);
      }
      card.nextOffset = data.next_offset;
      card.loaded = true;
    } catch (error) {
      if (error.name !== 'AbortError' && version === generation && card.activityToken === token && revision === (card.activityRevision || 0)) {
        card.more = button('Retry activity', () => loadActivity(card, offset));
        card.error = node('p', error.message);
        card.body.append(card.error, card.more);
      }
    } finally {
      loading.remove();
      if (card.activityToken === token) {
        card.loading = false;
        card.loadingNode = null;
        if (version === generation && revision !== (card.activityRevision || 0) && card.details.open) loadActivity(card);
      }
    }
  }

  function addCard(run, live = false) {
    if (cards.has(run.id)) {
      const card = cards.get(run.id);
      if (run.status !== card.run.status || run.usage?.expected_calls !== card.run.usage?.expected_calls) {
        card.activityRevision = (card.activityRevision || 0) + 1;
        card.loaded = false;
      }
      card.run = !activeStates.has(card.run.status) && activeStates.has(run.status)
        ? { ...run, status: card.run.status, adopted: card.run.adopted } : run;
      card.sourceIdentity = null;
      if (live) card.live = true;
      if (!activeStates.has(run.status)) card.phase = null;
      updateCard(card);
      placeCards();
      if (card.details.open && !card.loading && !card.loaded) loadActivity(card);
      return card;
    }
    const details = node('details', null, 'exploration-run');
    details.dataset.runId = run.id;
    const summary = node('summary');
    const heading = node('strong');
    const context = node('span', null, 'exploration-run-context');
    const usage = node('span', null, 'exploration-run-usage');
    const body = node('div', null, 'exploration-run-body');
    const summaryContent = node('div', null, 'exploration-run-summary');
    const chevron = node('i');
    chevron.setAttribute('data-lucide', 'chevron-down');
    chevron.setAttribute('aria-hidden', 'true');
    summaryContent.append(heading, context, usage);
    summary.append(summaryContent, chevron);
    details.append(summary, body);
    const card = { details, heading, context, usage, body, run, live, stepViews: new Map(), loaded: false, loading: false };
    details.addEventListener('toggle', () => { if (details.open && !card.loaded) loadActivity(card); });
    cards.set(run.id, card);
    updateCard(card);
    placeCards();
    card.disclosure = createDisclosure(details, body);
    refreshIcons(details);
    return card;
  }

  async function loadPage(before = null) {
    if (!conversationId || pageLoading) return;
    pageLoading = true;
    const version = generation;
    if (before != null) historyMore?.remove();
    historyRetry?.remove();
    historyError?.remove();
    const status = node('p', 'Loading project explorations…');
    region.append(status);
    try {
      const data = await json(`${base()}${before == null ? '' : `?before=${before}`}`, historyController.signal);
      if (version !== generation) return;
      [...data.runs].reverse().forEach(run => addCard(run));
      if (before != null || !historyPaged) nextBefore = data.next_before;
      if (before != null) historyPaged = true;
      if (nextBefore != null && historyMore?.parentElement !== region) {
        historyMore = button('Show older explorations', () => loadPage(nextBefore));
        region.prepend(historyMore);
      } else if (nextBefore == null) historyMore?.remove();
    } catch (error) {
      if (error.name !== 'AbortError' && version === generation) {
        historyError = node('p', error.message);
        historyRetry = button('Retry explorations', () => loadPage(before));
        region.append(historyError, historyRetry);
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

  function prepareRender() {
    const focused = document.activeElement;
    focusedControl = [...cards.values()].some(card => focused && card.details.contains(focused)) ? focused : null;
    const viewport = $('#chat').getBoundingClientRect();
    const visible = [...cards.values()].find(card => {
      const bounds = card.details.getBoundingClientRect();
      return card.details.open && bounds.bottom > viewport.top && bounds.top < viewport.bottom;
    });
    renderAnchor = visible ? { card: visible, top: visible.details.getBoundingClientRect().top } : null;
    cards.forEach(card => {
      card.disclosure.finish();
      card.stepViews.forEach(view => view.controller?.finish());
    });
  }

  function restorePosition({ preserveScroll = false } = {}) {
    if (preserveScroll && renderAnchor?.card.details.isConnected) {
      $('#chat').scrollTop += renderAnchor.card.details.getBoundingClientRect().top - renderAnchor.top;
    }
    if (focusedControl?.isConnected) focusedControl.focus({ preventScroll: true });
    renderAnchor = null;
    focusedControl = null;
  }

  function show(id, messages = []) {
    const sameConversation = id && id === conversationId;
    generation++;
    clearTimeout(refreshTimer);
    historyController?.abort();
    historyController = new AbortController();
    if (!sameConversation) {
      evidenceController?.abort();
      clearEvidence();
      if (evidence.open) evidence.close();
      cards.forEach(card => {
        card.stepViews.forEach(view => view.controller?.destroy());
        card.disclosure.destroy();
      });
      cards.clear();
      region.replaceChildren();
      nextBefore = null;
      historyPaged = false;
      renderAnchor = null;
      focusedControl = null;
    } else {
      cards.forEach(card => {
        card.disclosure.finish();
        card.stepViews.forEach(view => view.controller?.finish());
        card.activityToken = null;
        card.loadingNode?.remove();
        card.loading = false;
        if (card.loaded && card.nextOffset != null && card.more?.parentElement !== card.body) {
          card.more = button('Show more activity', () => loadActivity(card, card.nextOffset));
          card.body.append(card.more);
        }
      });
    }
    conversationId = id;
    messageTargets = messages;
    cards.forEach(card => {
      card.run.adopted = messages.some(message => message.role === 'assistant' && message.run_id === card.run.id);
      updateCard(card);
    });
    messageRegions.forEach(group => group.remove());
    messageRegions.clear();
    pageLoading = false;
    if (id) {
      placeCards();
      loadPage();
      cards.forEach(card => { if (card.details.open && !card.loaded) loadActivity(card); });
    } else region.remove();
  }

  function updateProgress(event, targets) {
    if (!event.run_id) return;
    if (event.type === 'start') {
      resetChoice();
      if (conversationId !== event.conversation_id) show(event.conversation_id);
      addCard({ id: event.run_id, status: 'preparing', goals: {}, profile: {} }, true);
      attach();
    }
    const card = cards.get(event.run_id);
    if (!card) return;
    if (targets) card.targets = targets;
    if (['work_progress', 'work_usage', 'error', 'done'].includes(event.type)) {
      card.activityRevision = (card.activityRevision || 0) + 1;
      card.loaded = false;
    }
    if (event.type === 'work_progress') {
      card.phase = `Work activity · ${event.phase === 'model' ? 'Model responding' : `${toolLabels[event.name] || 'Tool'}${event.phase === 'tool_completed' ? event.failed ? ' failed' : ' complete' : ' in progress'}`}`;
      card.loaded = false;
    }
    if (event.type === 'work_usage') {
      card.phase = 'Work activity · Response recorded';
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
    placeCards();
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

  document.addEventListener('timezone-change', () => cards.forEach(updateCard));
  document.addEventListener('workspace-change', event => { if (event.detail.changed) resetChoice(); else updateChoice(); });
  evidence.addEventListener('close', () => {
    if (evidence.open) return;
    evidenceController?.abort();
    evidenceController = null;
    clearEvidence();
    if (evidenceOpener?.isConnected) evidenceOpener.focus({ preventScroll: true });
  });
  updateChoice();
  refreshIcons();

  return {
    selection, resetChoice, show, prepareRender, restorePosition,
    progress(event, targets) { renderContent(() => updateProgress(event, targets)); },
    describeSelection: () => choice.checked ? 'Explore project is selected for this response.' : 'Work tools are off for this response.',
    setGenerating(value) { busy = value; updateChoice(); },
    setUsageVisibility(value) {
      historyVisibility = value;
      cards.forEach(card => {
        updateCard(card);
        card.stepViews.forEach(view => {
          view.controller?.finish();
          if (view.details) view.details.hidden = !value;
        });
      });
    }
  };
}
