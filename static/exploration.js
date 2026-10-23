/* Own one-response exploration consent, paged run activity, and immutable saved evidence views. */

import { $, refreshIcons, toast } from './ui.js';
import { showActionDialog } from './dialogs.js';
import { renderExplorationEvidence } from './exploration-evidence.js';
import { createDisclosure } from './disclosure.js';
import { usageView, workRunMetadata } from './response-usage.js';
import { createResponseFooter } from './response-footer.js';
import { describeWorkRun, workStepStatus, WORK_ACTION_LABELS } from './work-run-status.js';

const toolLabels = { helios_project_list_files: 'List files', helios_project_search_text: 'Search text', helios_project_read_file: 'Read file' };
const activeStates = new Set(['preparing', 'model', 'tools', 'ready']);

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
}

export function createExploration({ getWorkspace, chooseWorkspace, getLimits = () => ({}), onAction = () => {}, renderContent = render => render() }) {
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
    cards.forEach(card => card.controls?.forEach(control => { control.disabled = busy; }));
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

  function resultAnchor(card, question) {
    const source = messageTargets.find(message => message.element === question);
    const answer = source && messageTargets.find(message => message.role === 'assistant' && message.index === source.index + 1);
    if (!answer?.element.isConnected) return question;
    const started = Date.parse(card.run.started_at);
    const saved = Date.parse(answer.created_at);
    return started >= saved || (!Number.isFinite(started - saved) && card.run.kind === 'regeneration') ? answer.element : question;
  }

  function placeCards() {
    const positions = new Map();
    let hasUnmatched = false;
    const ordered = [...cards.values()].sort((left, right) =>
      (Date.parse(left.run.started_at) || Infinity) - (Date.parse(right.run.started_at) || Infinity));
    for (const card of ordered) {
      const message = linkedMessage(card);
      let destination;
      if (card.hasAnswer && message) {
        if (!messageRegions.has(message)) {
          const group = node('section', null, 'exploration-message exploration-records');
          group.setAttribute('aria-label', 'Project explorations for this answer');
          message.querySelector('.message-body').append(group);
          messageRegions.set(message, group);
        }
        destination = messageRegions.get(message);
      } else {
        ensureResult(card);
        destination = card.resultActivity;
        if (message) {
          const anchor = resultAnchor(card, message);
          const previous = positions.get(anchor) || anchor;
          if (previous.nextSibling !== card.result) $('#chat').insertBefore(card.result, previous.nextSibling);
          positions.set(anchor, card.result);
        } else {
          hasUnmatched = true;
          if (card.result.parentElement !== region) region.append(card.result);
        }
      }
      if (card.details.parentElement !== destination) {
        const focused = document.activeElement;
        const hadFocus = focused && (card.details.contains(focused) || card.result?.contains(focused));
        destination.append(card.details);
        if (hadFocus) focused.focus({ preventScroll: true });
      }
      if (card.hasAnswer && card.result) {
        card.resultMetadata.destroy();
        card.result.remove();
        card.result = null;
        card.resultMetadata = null;
        card.resultActivity = null;
        card.outcomeHost = null;
      }
    }
    for (const [message, group] of messageRegions) {
      if (!group.childElementCount || !message.isConnected) {
        group.remove();
        messageRegions.delete(message);
      }
    }
    if (hasUnmatched) {
      if (!historyHeading.isConnected) region.prepend(historyHeading, historyHint);
    } else {
      historyHeading.remove();
      historyHint.remove();
    }
    attach();
  }

  function ensureResult(card) {
    if (card.result) return;
    card.result = node('article', null, 'message work-result');
    card.result.dataset.runId = card.run.id;
    card.result.setAttribute('aria-label', 'App work result');
    const avatar = node('div', null, 'avatar');
    const icon = node('i');
    icon.setAttribute('data-lucide', 'workflow');
    icon.setAttribute('aria-hidden', 'true');
    avatar.append(icon);
    const body = node('div', null, 'message-body');
    card.outcomeHost = node('section', null, 'exploration-result exploration-records');
    const footer = node('div', null, 'message-footer');
    card.resultActivity = node('section', null, 'exploration-message exploration-records');
    card.resultActivity.setAttribute('aria-label', 'Project explorations for this work result');
    body.append(card.outcomeHost, footer, card.resultActivity);
    card.result.append(avatar, body);
    card.resultMetadata = createResponseFooter(footer, { timeLabel: 'Run started', onResize() { renderContent(() => {}); } });
    refreshIcons(card.result);
  }

  function statusText(run) {
    return `Work activity · ${describeWorkRun(run).label}`;
  }

  function sourceMessage(card) {
    const owner = linkedMessage(card);
    const target = messageTargets.find(message => message.element === owner);
    if (!target) return null;
    return target.role === 'user' ? target : messageTargets.find(message => message.role === 'user' && message.index === target.index - 1) || null;
  }

  function renderOutcome(card) {
    card.hasAnswer = card.run.adopted === true && linkedMessage(card)?.classList.contains('assistant');
    const outcome = describeWorkRun({ ...card.run, adopted: card.hasAnswer });
    card.details.dataset.tone = outcome.tone;
    if (card.hasAnswer) {
      card.outcome?.remove();
      card.outcome = null;
      card.outcomeSignature = null;
      card.controls = [];
      return;
    }
    ensureResult(card);
    card.result.dataset.tone = outcome.tone;
    card.resultMetadata.update(workRunMetadata(card.run));
    card.resultMetadata.setUsageVisibility(historyVisibility);
    const source = sourceMessage(card);
    const actions = outcome.actions.filter(action => action !== 'prepare-exploration' || source);
    const signature = JSON.stringify([outcome, card.phase, actions, source?.index, source?.created_at, source?.content]);
    if (signature === card.outcomeSignature) return;
    const section = card.outcome || node('section', null, 'exploration-run-outcome');
    section.dataset.tone = outcome.tone;
    section.replaceChildren(node('p', 'App work result', 'exploration-outcome-source'),
      node('h4', card.phase?.replace('Work activity · ', '') || outcome.label),
      node('p', outcome.message || 'Project exploration is in progress. A final answer has not been saved yet.'));
    if (outcome.details.length) section.append(node('p', outcome.details.join('\n'), 'exploration-outcome-details'));
    const controls = node('div', null, 'exploration-outcome-actions');
    card.controls = actions.map(action => {
      const control = button(WORK_ACTION_LABELS[action], async () => {
        if (busy || !card.result?.isConnected || !cards.has(card.run.id)) return;
        const currentSource = sourceMessage(card);
        if (action === 'prepare-exploration' && !currentSource) return;
        control.disabled = true;
        try { await onAction(action, { conversationId, run: card.run, source: currentSource }); }
        catch { toast('Unable to open this action. Please try again.', 'error'); }
        finally { control.disabled = busy; }
      });
      control.dataset.mutatesConversation = '';
      control.disabled = busy;
      controls.append(control);
      return control;
    });
    if (card.controls.length) section.append(controls);
    if (!source && outcome.actions.includes('prepare-exploration')) {
      section.append(node('p', 'The original question cannot be verified in this conversation branch. Start a new request from the composer.', 'exploration-outcome-note'));
    }
    card.outcome = section;
    card.outcomeSignature = signature;
    if (section.parentElement !== card.outcomeHost) card.outcomeHost.append(section);
  }

  function updateCard(card) {
    const heading = card.phase || statusText(card.run);
    if (card.heading.textContent !== heading) card.heading.textContent = heading;
    card.context.textContent = 'Explore project';
    renderOutcome(card);
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
      node('span', workStepStatus(step.status), 'exploration-step-status'));
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
      if (offset === 0 && !data.steps.length && data.next_offset == null) {
        card.steps.append(node('p', 'No model or tool steps were recorded.', 'exploration-activity-empty'));
      }
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
        ? { ...card.run, ...run, status: card.run.status, adopted: card.run.adopted, reason: card.run.reason, failure: card.run.failure }
        : { ...run, cancellationRequested: activeStates.has(run.status) && card.run.cancellationRequested };
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
    if (live) heading.setAttribute('aria-live', 'polite');
    const context = node('span', null, 'exploration-run-context');
    const body = node('div', null, 'exploration-run-body');
    const summaryContent = node('div', null, 'exploration-run-summary');
    const chevron = node('i');
    chevron.setAttribute('data-lucide', 'chevron-down');
    chevron.setAttribute('aria-hidden', 'true');
    summaryContent.append(heading, context);
    summary.append(summaryContent, chevron);
    details.append(summary, body);
    const card = { details, heading, context, body, run, live, stepViews: new Map(), loaded: false, loading: false };
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
    focusedControl = [...cards.values()].some(card => focused && (card.details.contains(focused) || card.result?.contains(focused))) ? focused : null;
    const viewport = $('#chat').getBoundingClientRect();
    const visible = [...cards.values()].find(card => {
      const bounds = (card.result || card.details).getBoundingClientRect();
      return (card.result || card.details.open) && bounds.bottom > viewport.top && bounds.top < viewport.bottom;
    });
    renderAnchor = visible ? { card: visible, top: (visible.result || visible.details).getBoundingClientRect().top } : null;
    cards.forEach(card => {
      card.resultMetadata?.finish();
      card.disclosure.finish();
      card.stepViews.forEach(view => view.controller?.finish());
    });
  }

  function restorePosition({ preserveScroll = false } = {}) {
    const anchor = renderAnchor && (renderAnchor.card.result || renderAnchor.card.details);
    if (preserveScroll && anchor?.isConnected) {
      $('#chat').scrollTop += anchor.getBoundingClientRect().top - renderAnchor.top;
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
        card.resultMetadata?.destroy();
        card.result?.remove();
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
        card.resultMetadata?.finish();
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
    if (event.type === 'cancel_requested') {
      card.run.cancellationRequested = true;
      card.phase = null;
    }
    if (event.type === 'error' || event.type === 'done') {
      card.run.status = event.run_status || (event.type === 'done' ? 'completed' : 'failed');
      card.run.adopted = event.type === 'done';
      card.run.usage = event.run_usage || card.run.usage;
      card.run.cancellationRequested = false;
      if (event.type === 'error') {
        card.run.reason = event.code;
        card.run.failure = event;
      }
      card.phase = null;
      card.live = false;
    }
    updateCard(card);
    placeCards();
    if (card.details.open && !card.loading && !card.loaded) loadActivity(card);
  }

  async function selectForNextResponse(isCurrent = () => true) {
    if (busy || !isCurrent()) return false;
    if (!getWorkspace().root) {
      resetChoice();
      tools.open = false;
      const accepted = await showActionDialog({ title: 'Choose a workspace?',
        description: 'Explore project needs a workspace to find and read project files. Would you like to choose one?',
        confirmLabel: 'Choose workspace' });
      if (!accepted || busy || !isCurrent() || !await chooseWorkspace() || busy || !isCurrent()) return false;
    }
    choice.checked = true;
    tools.open = false;
    updateChoice();
    return true;
  }

  tools.addEventListener('change', async event => {
    if (!event.target.matches('input[data-work-tool]')) return;
    if (event.target === choice && choice.checked && !getWorkspace().root) {
      await selectForNextResponse();
      updateChoice();
      $('#work-tools-trigger').focus({ preventScroll: true });
    } else updateChoice();
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
    selection, resetChoice, show, prepareRender, restorePosition, selectForNextResponse,
    refreshTimestamps() { cards.forEach(card => card.resultMetadata?.refreshTimestamp()); },
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
