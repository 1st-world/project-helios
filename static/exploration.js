/* Own tool-enabled response presentation, one-response consent, paged run activity, and immutable evidence views. */

import { $, refreshIcons, toast } from './ui.js';
import { showActionDialog } from './dialogs.js';
import { renderExplorationEvidence } from './exploration-evidence.js';
import { createDisclosure } from './disclosure.js';
import { usageView, workRunMetadata } from './response-usage.js';
import { createResponseFooter } from './response-footer.js';
import { createResponseProgress, workProgressLabel } from './response-progress.js';
import { createReasoningSummary, renderReasoningText } from './reasoning-summary.js';
import { describeWorkRun, workStepStatus, WORK_ACTION_LABELS } from './work-run-status.js';

const toolLabels = { helios_project_list_files: 'List files', helios_project_search_text: 'Search text', helios_project_read_file: 'Read file' };
const toolIcons = { helios_project_list_files: 'folder', helios_project_search_text: 'search', helios_project_read_file: 'file-text' };
const activeStates = new Set(['preparing', 'model', 'tools', 'ready']);

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
}

export function createExploration({ getWorkspace, chooseWorkspace, getLimits = () => ({}), onAction = () => {}, renderContent = render => render(), onPausedChange = () => {} }) {
  const choice = $('#explore-project');
  const tools = $('#work-tools');
  const evidence = $('#exploration-evidence-dialog');
  const region = node('section', null, 'exploration-history exploration-records');
  region.setAttribute('aria-label', 'Work history');
  const historyHeading = node('h3', 'Other runs', 'exploration-history-title');
  const historyHint = node('p', 'These saved attempts have no matching question or answer in the current conversation.', 'exploration-history-hint');
  const cards = new Map();
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
  let runStateRevision = 0;

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
    if (!workspace.root || workspace.revision == null || !workspace.sessionId) throw new Error('Choose a Workspace to use Explore project.');
    const goals = getLimits();
    return { explore_project: true, ...(Object.keys(goals).length ? { exploration_limits: goals } : {}) };
  }

  async function json(url, signal, label = 'Saved work') {
    const response = await fetch(url, { signal });
    if (!response.ok) throw new Error(`${label} could not be loaded (HTTP ${response.status}).`);
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
      const answering = card.showingAnswer && card.targets?.assistant?.isConnected;
      const ordinary = !card.hasTools && activeStates.has(card.run.status) && card.targets?.assistant?.isConnected;
      if (card.hasAnswer && message || answering || ordinary) {
        destination = (answering || ordinary ? card.targets.assistant : message).querySelector('.message-body');
        if (ordinary) card.targets.assistant.hidden = false;
      } else {
        if (card.targets?.assistant && activeStates.has(card.run.status)) card.targets.assistant.hidden = true;
        ensureResult(card);
        destination = card.result.querySelector('.message-body');
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
        destination.insertBefore(card.details, destination.firstChild);
        if (hadFocus) focused.focus({ preventScroll: true });
      }
      if (card.progressView && !card.hasTools && card.details.hidden) {
        card.summaryView.setProgress(null);
        destination.insertBefore(card.progressView.el, destination.querySelector('.message-footer'));
      }
      if (card.hasAnswer && card.result) {
        card.resultMetadata.destroy();
        card.result.remove();
        card.result = null;
        card.resultMetadata = null;
        card.outcomeHost = null;
      } else if (card.result) card.result.hidden = Boolean(answering || ordinary);
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
    body.append(card.outcomeHost, footer);
    card.result.append(avatar, body);
    card.resultMetadata = createResponseFooter(footer, { timeLabel: 'Run started', onResize() { renderContent(() => {}); } });
    card.resultMetadata.update(workRunMetadata(card.run));
    card.resultMetadata.setUsageVisibility(historyVisibility);
    refreshIcons(card.result);
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
    if (!activeStates.has(card.run.status)) {
      card.summaryView?.setProgress(null);
      card.progressView?.destroy();
      card.progressView = null;
      for (const step of card.stepData.values()) {
        if (step.status === 'in_progress') updateStep(card, { ...step, status: 'interrupted' });
      }
    }
    if (card.hasAnswer) {
      card.outcome?.remove();
      card.outcome = null;
      card.outcomeSignature = null;
      card.controls = [];
      return;
    }
    if (activeStates.has(card.run.status)) {
      card.outcome?.remove();
      card.outcome = null;
      card.outcomeSignature = null;
      card.controls = [];
      card.progressView ||= createResponseProgress({ startedAt: card.targets?.startedAt ?? card.run.started_at,
        label: card.run.cancellationRequested ? 'stopping' : card.run.pauseRequested || card.run.pause_requested ? 'pausing' : card.phase || 'Preparing request' });
      card.progressView.update(card.run.cancellationRequested ? 'stopping' : card.run.pauseRequested || card.run.pause_requested ? 'pausing' : card.phase || 'Preparing request');
      card.summaryView.setProgress(card.details.hidden ? null : card.progressView);
      if (card.result) {
        card.resultMetadata.update(workRunMetadata(card.run));
        card.resultMetadata.setUsageVisibility(historyVisibility);
      }
      return;
    }
    ensureResult(card);
    card.result.dataset.tone = outcome.tone;
    card.resultMetadata.update(workRunMetadata(card.run));
    card.resultMetadata.setUsageVisibility(historyVisibility);
    const source = sourceMessage(card);
    const actions = outcome.actions.filter(action => !['prepare-request', 'resume-run'].includes(action) || source);
    const signature = JSON.stringify([outcome, card.phase, actions, source?.index, source?.created_at, source?.content]);
    if (signature === card.outcomeSignature) return;
    const section = card.outcome || node('section', null, 'exploration-run-outcome status-callout');
    section.dataset.tone = outcome.tone;
    section.replaceChildren(node('p', 'App work result', 'exploration-outcome-source status-callout-source'),
      node('h4', card.phase || outcome.label, 'status-callout-title'),
      node('p', outcome.message || 'Work is in progress. A final answer has not been saved yet.', 'status-callout-message'));
    if (outcome.details.length) section.append(node('p', outcome.details.join('\n'), 'exploration-outcome-details status-callout-details'));
    const controls = node('div', null, 'exploration-outcome-actions status-callout-actions');
    card.controls = actions.map(action => {
      const control = button(WORK_ACTION_LABELS[action], async () => {
        if (busy || !card.result?.isConnected || !cards.has(card.run.id)) return;
        const currentSource = sourceMessage(card);
        if (['prepare-request', 'resume-run'].includes(action) && !currentSource) return;
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
    if (!source && outcome.actions.includes('prepare-request')) {
      section.append(node('p', 'The original question cannot be verified in this conversation branch. Start a new request from the composer.', 'exploration-outcome-note status-callout-details'));
    }
    card.outcome = section;
    card.outcomeSignature = signature;
    if (section.parentElement !== card.outcomeHost) card.outcomeHost.append(section);
  }

  function updateCard(card) {
    const hadTools = card.presentingWork;
    card.hasTools ||= card.run.has_tool_activity === true || (card.run.has_tool_activity == null && !card.live);
    card.summaryView.setWork(card.hasTools);
    card.steps.hidden = !card.hasTools;
    if (!card.hasTools) {
      const saved = messageTargets.find(message => message.role === 'assistant' && message.run_id === card.run.id);
      const parts = !card.live && saved ? saved.reasoning_summary || []
        : card.stepData.size ? [...card.stepData.values()].flatMap(step => step.reasoning_summary || []) : [];
      card.summaryView.update(parts);
    } else if (!hadTools) card.summaryView.update([]);
    card.presentingWork = card.hasTools;
    renderOutcome(card);
  }

  function loadVisibleActivity(card) {
    const needsSummary = !card.hasTools && !card.live && !card.hasAnswer;
    if (!card.loading && !card.loaded && (card.hasTools && card.details.open || needsSummary)) loadActivity(card);
  }

  function addFact(list, label, value) {
    const row = node('div');
    row.append(node('dt', label), node('dd', value));
    list.append(row);
  }

  function appendStep(card, step, position) {
    card.empty?.remove();
    card.empty = null;
    const key = stepKey(step, position);
    const signature = JSON.stringify([step.name, step.status, step.record?.usage, step.result_available, step.reasoning_summary, step.intermediate_text]);
    card.stepData.set(key, step);
    let view = card.stepViews.get(key);
    if (view?.signature === signature) return;
    const followBottom = card.live && card.details.open && card.body.scrollHeight - card.body.scrollTop - card.body.clientHeight <= 32;
    if (!view) {
      const row = node('div', null, 'exploration-step');
      const stepDetails = node('details', null, 'exploration-step-disclosure');
      stepDetails.open = activeStates.has(card.run.status) && !card.answering;
      const heading = node('summary', null, 'exploration-step-heading');
      const status = node('span', null, 'exploration-step-status');
      const title = node('strong');
      const icon = node('i');
      icon.setAttribute('data-lucide', step.kind === 'model' ? 'bot' : toolIcons[step.name] || 'wrench');
      icon.setAttribute('aria-hidden', 'true');
      title.append(icon, node('span', step.kind === 'model' ? 'Model call' : toolLabels[step.name] || 'Unsupported tool'));
      const toggle = node('i', null, 'exploration-step-toggle');
      toggle.setAttribute('data-lucide', 'chevron-down');
      toggle.setAttribute('aria-hidden', 'true');
      heading.append(title, status, toggle);
      const body = node('div', null, 'exploration-step-body');
      stepDetails.append(heading, body);
      row.append(stepDetails);
      card.steps.append(row);
      view = { row, stepDetails, body, status };
      view.stepController = createDisclosure(stepDetails, body);
      if (step.kind === 'model') {
        view.content = node('div', null, 'exploration-model-content');
        view.parts = new Map();
        body.append(view.content);
      }
      card.stepViews.set(key, view);
    }
    view.status.textContent = workStepStatus(step.status);
    if (step.kind === 'model') {
      renderModelContent(view, step);
      renderCallUsage(view, step);
      view.pendingUsage = step.status === 'in_progress' && !step.record?.usage;
      view.details.hidden = !historyVisibility || view.pendingUsage;
    } else if (step.result_available && !view.resultButton) {
      const open = button('Open saved result', () => openEvidence(card.run.id, card.stepData.get(key), open));
      view.resultButton = open;
      view.body.append(open);
    } else if (!step.result_available) {
      view.resultButton?.remove();
      view.resultButton = null;
    }
    updateStepBody(view);
    view.signature = signature;
    if (followBottom) card.body.scrollTop = card.body.scrollHeight;
    refreshIcons(view.row);
  }

  function renderCallUsage(view, step) {
    const signature = JSON.stringify(step.record?.usage);
    if (view.details && view.usageSignature === signature) return;
    view.controller?.finish();
    const expanded = view.details?.open === true;
    view.controller?.destroy();
    view.details?.remove();
    const usage = usageView(step.record?.usage || { usage_status: 'unavailable', cost_status: 'unavailable' });
    const details = node('details', null, 'exploration-call-usage');
    details.open = expanded;
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
    view.body.append(details);
    view.details = details;
    view.usageSignature = signature;
    view.controller = createDisclosure(details, body);
  }

  function updateStepBody(view) {
    view.body.hidden = !view.summary && !view.output && !view.resultButton && (!view.details || view.details.hidden);
  }

  function beginAnswer(card) {
    if (card.answering) return;
    card.answering = true;
    if (card.details.contains(document.activeElement)) card.details.querySelector('summary').focus({ preventScroll: true });
    card.summaryView.answerStarted();
    card.stepViews.forEach(view => {
      view.controller?.finish();
      view.stepController.finish(false);
    });
  }

  function renderModelContent(view, step) {
    const retained = new Set();
    const parts = (step.reasoning_summary || []).filter(part => part.text?.trim());
    if (parts.length && !view.summary) {
      const section = node('section', null, 'exploration-model-section');
      const content = node('div', null, 'exploration-model-parts');
      section.append(node('p', 'Reasoning summary', 'exploration-content-label'), content);
      view.summary = { section, content };
      view.content.insertBefore(section, view.content.firstChild);
    }
    for (const [position, part] of parts.entries()) {
      const key = JSON.stringify([part.item_id, part.summary_index]);
      retained.add(key);
      let partView = view.parts.get(key);
      if (!partView) {
        partView = { el: node('div', null, 'reasoning-summary-content') };
        view.parts.set(key, partView);
      }
      if (partView.text !== part.text) {
        partView.el.innerHTML = renderReasoningText(part.text);
        partView.text = part.text;
      }
      if (view.summary.content.children[position] !== partView.el) view.summary.content.insertBefore(partView.el, view.summary.content.children[position] || null);
    }
    for (const [key, partView] of view.parts) {
      if (retained.has(key)) continue;
      partView.el.remove();
      view.parts.delete(key);
    }
    if (!parts.length) {
      view.summary?.section.remove();
      view.summary = null;
    }
    if (step.intermediate_text?.trim()) {
      if (!view.output) {
        const section = node('section', null, 'exploration-model-section');
        const content = node('div', null, 'exploration-model-output reasoning-summary-content');
        section.append(node('p', 'Model message', 'exploration-content-label'), content);
        view.output = { section, content };
        view.content.append(section);
      }
      if (view.output.text !== step.intermediate_text) {
        view.output.content.innerHTML = renderReasoningText(step.intermediate_text);
        view.output.text = step.intermediate_text;
      }
    } else {
      view.output?.section.remove();
      view.output = null;
    }
  }

  function updateStep(card, step) {
    const key = stepKey(step);
    appendStep(card, { ...card.stepData.get(key), ...step }, card.stepData.size);
  }

  function stepKey(step, position = '') {
    return `${step.kind}:${step.kind === 'tool' ? `${step.response_id}:` : ''}${step.call_id ?? position}`;
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
      const result = await json(`${base(id)}/${encodeURIComponent(runId)}/evidence?${query}`, controller.signal, 'Saved result');
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
    const loading = node('p', 'Loading work process…');
    card.body.append(loading);
    const version = generation;
    const revision = card.activityRevision || 0;
    const token = {};
    card.activityToken = token;
    card.loadingNode = loading;
    try {
      const data = await json(`${base()}/${encodeURIComponent(card.run.id)}?offset=${offset}`, historyController?.signal, 'Work process');
      if (version !== generation || card.activityToken !== token || revision !== (card.activityRevision || 0)) return;
      card.run = { ...card.run, ...data.run, usage: data.usage };
      updateCard(card);
      card.sourceIdentity = null;
      placeCards();
      data.steps.forEach((step, index) => {
        const current = card.stepData.get(stepKey(step, offset + index));
        appendStep(card, card.live && current ? { ...step, ...current, record: step.record || current.record } : step, offset + index);
      });
      updateCard(card);
      placeCards();
      if (offset === 0 && !card.stepViews.size && !data.steps.length && data.next_offset == null && !activeStates.has(card.run.status)) {
        card.empty = node('p', 'No model or tool steps were recorded.', 'exploration-activity-empty');
        card.steps.append(card.empty);
      }
      if (data.next_offset != null) {
        card.more = button('Show more steps', () => loadActivity(card, data.next_offset));
        card.body.append(card.more);
      }
      card.nextOffset = data.next_offset;
      card.loaded = true;
    } catch (error) {
      if (error.name !== 'AbortError' && version === generation && card.activityToken === token && revision === (card.activityRevision || 0)) {
        card.more = button('Retry loading steps', () => loadActivity(card, offset));
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

  function addCard(run, live = false, targets = null) {
    if (cards.has(run.id)) {
      const card = cards.get(run.id);
      if (targets) card.targets = targets;
      if (run.status !== card.run.status || run.usage?.expected_calls !== card.run.usage?.expected_calls) {
        card.activityRevision = (card.activityRevision || 0) + 1;
        card.loaded = false;
      }
      card.run = !live && !activeStates.has(card.run.status) && activeStates.has(run.status)
        ? { ...card.run, ...run, status: card.run.status, adopted: card.run.adopted, reason: card.run.reason, failure: card.run.failure }
        : { ...(live ? card.run : {}), ...run, cancellationRequested: activeStates.has(run.status) && card.run.cancellationRequested,
          pauseRequested: activeStates.has(run.status) && (run.pause_requested || !live && card.run.pauseRequested) };
      card.sourceIdentity = null;
      if (live) card.live = true;
      if (!activeStates.has(card.run.status)) {
        card.phase = null;
        card.live = false;
      }
      updateCard(card);
      placeCards();
      loadVisibleActivity(card);
      return card;
    }
    const summaryView = createReasoningSummary();
    const details = summaryView.el;
    details.classList.add('exploration-run');
    details.dataset.runId = run.id;
    const body = summaryView.content;
    body.classList.add('exploration-records');
    const steps = node('div', null, 'exploration-steps');
    body.append(steps);
    const card = { details, body, steps, summaryView, run, live, targets, hasTools: false,
      stepViews: new Map(), stepData: new Map(), loaded: false, loading: false, disclosure: summaryView };
    details.addEventListener('toggle', () => loadVisibleActivity(card));
    cards.set(run.id, card);
    updateCard(card);
    placeCards();
    refreshIcons(details);
    loadVisibleActivity(card);
    return card;
  }

  async function loadPage(before = null) {
    if (!conversationId || pageLoading) return;
    pageLoading = true;
    const version = generation;
    const revisions = new Map([...cards].map(([id, card]) => [id, card.activityRevision || 0]));
    const stateRevision = runStateRevision;
    if (before != null) historyMore?.remove();
    historyRetry?.remove();
    historyError?.remove();
    const status = node('p', 'Loading work history…');
    region.append(status);
    try {
      const data = await json(`${base()}${before == null ? '' : `?before=${before}`}`, historyController.signal, 'Work history');
      if (version !== generation) return;
      [...data.runs].reverse().forEach(run => {
        const card = cards.get(run.id);
        if (run.status === 'paused' && card?.live && activeStates.has(card.run.status)
            && revisions.get(run.id) !== (card.activityRevision || 0)) return;
        addCard(run);
      });
      if (stateRevision === runStateRevision && Object.hasOwn(data, 'paused_run')) {
        onPausedChange(conversationId, data.paused_run);
      }
      if (before != null || !historyPaged) nextBefore = data.next_before;
      if (before != null) historyPaged = true;
      if (nextBefore != null && historyMore?.parentElement !== region) {
        historyMore = button('Show older runs', () => loadPage(nextBefore));
        region.prepend(historyMore);
      } else if (nextBefore == null) historyMore?.remove();
    } catch (error) {
      if (error.name !== 'AbortError' && version === generation) {
        historyError = node('p', error.message);
        historyRetry = button('Retry loading history', () => loadPage(before));
        region.append(historyError, historyRetry);
      }
    } finally {
      status.remove();
      if (version === generation) {
        pageLoading = false;
        clearTimeout(refreshTimer);
        if ([...cards.values()].some(card => activeStates.has(card.run.status))) refreshTimer = setTimeout(() => loadPage(), 1500);
        else {
          const deadlines = [...cards.values()].filter(card => card.run.status === 'paused')
            .map(card => Date.parse(card.run.pause_expires_at)).filter(value => value > Date.now());
          if (deadlines.length) refreshTimer = setTimeout(() => loadPage(), Math.min(...deadlines) - Date.now());
        }
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
      card.stepViews.forEach(view => { view.controller?.finish(); view.stepController.finish(); });
      card.disclosure.finish();
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

  function show(id, messages = [], pausedRun = null) {
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
        card.progressView?.destroy();
        card.stepViews.forEach(view => { view.controller?.destroy(); view.stepController.destroy(); });
        card.resultMetadata?.destroy();
        card.disclosure.destroy();
        card.result?.remove();
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
        card.stepViews.forEach(view => { view.controller?.finish(); view.stepController.finish(); });
        card.disclosure.finish();
        card.activityToken = null;
        card.loadingNode?.remove();
        card.loading = false;
        if (card.loaded && card.nextOffset != null && card.more?.parentElement !== card.body) {
          card.more = button('Show more steps', () => loadActivity(card, card.nextOffset));
          card.body.append(card.more);
        }
      });
    }
    conversationId = id;
    messageTargets = messages;
    if (pausedRun?.status === 'paused') addCard(pausedRun);
    cards.forEach(card => {
      card.run.adopted = messages.some(message => message.role === 'assistant' && message.run_id === card.run.id);
      updateCard(card);
    });
    pageLoading = false;
    if (id) {
      placeCards();
      loadPage();
      cards.forEach(loadVisibleActivity);
    } else region.remove();
  }

  function updateProgress(event, targets) {
    if (!event.run_id) return;
    if (['start', 'paused', 'error', 'done'].includes(event.type)) runStateRevision++;
    if (event.type === 'start') {
      onPausedChange(event.conversation_id, null);
      resetChoice();
      if (conversationId !== event.conversation_id) show(event.conversation_id);
      const previous = cards.get(event.run_id);
      if (previous?.run.status === 'paused') {
        previous.progressView?.destroy();
        previous.progressView = null;
        previous.summaryView.finish(true);
      }
      addCard({ ...(previous ? {} : { has_tool_activity: false, goals: {}, profile: {} }), id: event.run_id,
        status: 'preparing', pause_requested: false }, true, targets);
      attach();
    }
    const card = cards.get(event.run_id);
    if (!card) return;
    if (targets) card.targets = targets;
    if (['work_progress', 'work_usage', 'error', 'done', 'paused'].includes(event.type)) {
      card.activityRevision = (card.activityRevision || 0) + 1;
      card.loaded = false;
    }
    if (event.type === 'work_progress') {
      card.phase = workProgressLabel(event);
      card.loaded = false;
      if (event.phase === 'model' && event.call_id) {
        card.currentCallId = event.call_id;
        updateStep(card, { kind: 'model', call_id: event.call_id, status: 'in_progress' });
      } else if (event.call_id && ['tool', 'tool_completed'].includes(event.phase)) {
        card.hasTools = true;
        updateStep(card, { kind: 'tool', call_id: event.call_id, response_id: event.response_id,
          name: event.name, status: event.phase === 'tool' ? 'in_progress' : event.failed ? 'failed' : 'completed',
          result_available: event.phase === 'tool_completed' });
      }
    }
    if (event.type === 'work_output') {
      if (event.delta || event.text) card.phase = card.hasTools ? workProgressLabel(event) : 'Receiving response';
      const previous = card.stepData.get(`model:${event.call_id}`);
      updateStep(card, { kind: 'model', call_id: event.call_id, response_id: event.response_id || previous?.response_id,
        status: previous?.status || 'in_progress',
        intermediate_text: event.text ?? `${previous?.intermediate_text || ''}${event.delta || ''}` });
    }
    if (event.type === 'work_usage') {
      card.phase = workProgressLabel(event);
      card.loaded = false;
      if (event.call_id) updateStep(card, { kind: 'model', call_id: event.call_id, status: 'completed', record: { usage: event.usage } });
      loadPage();
    }
    if (event.type === 'run_usage') card.run.usage = event.usage;
    if (event.type === 'reasoning_summary') {
      updateStep(card, { kind: 'model', call_id: event.call_id,
        reasoning_summary: event.parts || [] });
      card.phase = event.parts?.some(part => part.status === 'in_progress') ? workProgressLabel(event) : 'Waiting for model';
    }
    if (event.type === 'delta') {
      card.phase = workProgressLabel(event);
      beginAnswer(card);
      card.showingAnswer = true;
      if (card.targets?.assistant) card.targets.assistant.hidden = false;
    }
    if (event.type === 'cancel_requested') {
      card.run.cancellationRequested = true;
      card.phase = null;
    }
    if (event.type === 'pause_requested') {
      card.run.pauseRequested = true;
      card.phase = null;
    }
    if (event.type === 'pause_failed') {
      card.run.pauseRequested = false;
      card.run.pause_requested = false;
      card.phase = null;
    }
    if (event.type === 'paused') {
      Object.assign(card.run, event.run || {}, { status: 'paused', adopted: false,
        usage: event.usage || event.run?.usage || card.run.usage,
        resume_available: event.run?.resume_available ?? event.resume_available ?? true,
        cancellationRequested: false, pauseRequested: false, pause_requested: false });
      card.phase = null;
      card.live = false;
      card.showingAnswer = false;
      onPausedChange(conversationId, card.run);
    }
    if (event.type === 'error' || event.type === 'done') {
      if (event.type === 'done') beginAnswer(card);
      card.run.status = event.run_status || (event.type === 'done' ? 'completed' : 'failed');
      card.run.adopted = event.type === 'done';
      if (event.response_metadata?.has_tool_activity === true) card.hasTools = true;
      card.run.usage = event.run_usage || card.run.usage;
      card.run.cancellationRequested = false;
      card.run.pauseRequested = false;
      card.run.pause_requested = false;
      card.showingAnswer = false;
      if (event.type === 'error') {
        card.run.reason = event.code;
        card.run.failure = event;
      }
      card.phase = null;
      card.live = false;
      if (event.type === 'error') {
        for (const step of card.stepData.values()) {
          if (step.kind !== 'model') continue;
          updateStep(card, { ...step, reasoning_summary: step.reasoning_summary?.map(part =>
            part.status === 'in_progress' ? { ...part, status: 'incomplete' } : part) });
        }
      }
    }
    updateCard(card);
    placeCards();
    loadVisibleActivity(card);
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
          if (view.details) view.details.hidden = !value || view.pendingUsage;
          updateStepBody(view);
        });
      });
    }
  };
}
