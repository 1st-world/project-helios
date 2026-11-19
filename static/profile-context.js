/* Edit profile context settings and preview their effective policy without provider calls. */

import { $, setRefreshBusy, setText } from './ui.js';
import { PREFLIGHT_HELP, describeMemoryState, describeRequestError } from './chat-feedback.js';
import { describeEstimation } from './estimation.js';
import { setFieldFeedback } from './field-validation.js';

export function createProfileContext({ isSaving }) {
  const dialog = $('#settings-dialog');
  const disclosure = $('#profile-context-settings');
  const policyTitle = $('#profile-context-effective-title');
  const policyStatus = $('#profile-context-effective-status');
  const policyValues = $('#profile-context-effective');
  const retry = $('#profile-context-retry');
  const fields = new Map([...disclosure.querySelectorAll('[data-profile-context]')].map(input => [input.dataset.profileContext, input]));
  const resets = [...disclosure.querySelectorAll('[data-clear-profile-context]')];
  const state = { profile: null, timer: null, controller: null, loadedKey: null, usesLegacyAppBudget: false };

  function savedValue(name) {
    if (name === 'memory_enabled') return state.profile ? state.profile[name] ?? null : false;
    if (name === 'context_input_budget_mode') return state.profile ? state.profile[name] ?? 'legacy' : 'inherit';
    if (name === 'output_token_limit' && state.profile && !Object.hasOwn(state.profile, name)) {
      return state.profile.model_max_output_tokens ?? null;
    }
    return state.profile?.[name] ?? null;
  }

  function updateBudgetReview() {
    const review = $('#profile-input-budget-review');
    const input = fields.get('context_input_budget_mode');
    let message = '';
    if (input.value === 'legacy') {
      message = 'Choose and save an input budget to replace this profile’s previous policy.';
    } else if (input.value === 'inherit' && state.usesLegacyAppBudget) {
      message = 'This profile inherits the previous app policy. Update Input budget in Context & attachments or choose a budget for this profile.';
    }
    setText(review, message);
    setFieldFeedback(input, review, Boolean(message));
  }

  function updateControls() {
    const budgetMode = fields.get('context_input_budget_mode').value;
    const memoryMode = fields.get('memory_enabled');
    fields.get('context_input_budget').disabled = isSaving() || budgetMode !== 'custom';
    fields.get('context_input_budget').closest('.settings-preference-row').hidden = budgetMode !== 'custom';
    fields.get('context_input_budget_mode').querySelector('[value="legacy"]').hidden = savedValue('context_input_budget_mode') !== 'legacy';
    memoryMode.querySelector('[value=""]').hidden = savedValue('memory_enabled') !== null;
    setFieldFeedback(memoryMode, $('#profile-memory-review'), Boolean(state.profile) && memoryMode.value === '');
    updateBudgetReview();
    resets.forEach(button => {
      const input = fields.get(button.dataset.clearProfileContext);
      button.hidden = button.dataset.clearProfileContext === 'context_input_budget_mode' ? input.value === 'inherit' : !input.value && !input.validity.badInput;
      if (button.dataset.clearProfileContext === 'context_input_budget' && budgetMode !== 'custom') button.hidden = true;
      if (button.dataset.clearProfileContext === 'memory_enabled') button.hidden = input.value === 'false';
      button.disabled = isSaving() || button.hidden;
    });
    $('#profile-context-preflight-help').textContent = PREFLIGHT_HELP[fields.get('context_preflight_mode').value]
      || 'Uses the input check selected in Context & attachments.';
  }

  function cancelPreview() {
    clearTimeout(state.timer);
    state.timer = null;
    state.controller?.abort();
    state.controller = null;
    setRefreshBusy(retry, false);
  }

  function invalidate() {
    cancelPreview();
    state.loadedKey = null;
    state.usesLegacyAppBudget = false;
  }

  function select(profile) {
    if (!profile || state.profile?.id !== profile.id) policyValues.replaceChildren();
    state.profile = profile;
    invalidate();
    for (const [name, input] of fields) {
      input.value = savedValue(name) ?? '';
      input.setCustomValidity('');
    }
    updateControls();
    schedulePreview();
  }

  function setPreviewFeedback(message = '') {
    const stale = Boolean(message) && policyValues.hasChildNodes();
    policyValues.toggleAttribute('data-stale', stale);
    const separator = message.includes('\n') ? '\n' : ' ';
    setText(policyStatus, stale ? `${message}${separator}Showing previous values.` : message);
  }

  function schedulePreview(delay = 0, { retrying = false } = {}) {
    cancelPreview();
    if (!disclosure.open || !dialog.open || $('#settings-connections').hidden) return;

    const profileId = state.profile?.id ?? null;
    const edits = changes(Boolean(profileId));
    const invalid = [...fields.values()].some(input => fieldError(input));
    const draft = invalid || !profileId || Object.keys(edits).length > 0;
    const key = JSON.stringify([profileId, draft ? edits : null]);
    setText(policyTitle, draft ? 'Draft effective policy' : 'Saved effective policy');
    retry.hidden = !retrying;
    if (invalid) {
      retry.hidden = true;
      setPreviewFeedback('Check the context fields.');
      return;
    }
    if (key === state.loadedKey) {
      retry.hidden = true;
      setPreviewFeedback();
      return;
    }

    if (!retrying) setPreviewFeedback('Updating preview.');
    const payload = draft ? { profile_id: profileId, ...edits } : null;
    if (delay) {
      state.timer = setTimeout(() => {
        state.timer = null;
        loadPreview(profileId, payload, key);
      }, delay);
    } else {
      loadPreview(profileId, payload, key, retrying);
    }
  }

  async function loadPreview(profileId, payload, key, retrying = false) {
    const controller = new AbortController();
    state.controller = controller;
    if (retrying) setRefreshBusy(retry, true);
    try {
      const response = payload
        ? await fetch('/api/context-policy/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          signal: controller.signal,
        })
        : await fetch(`/api/context-policy?profile_id=${encodeURIComponent(profileId)}`, { signal: controller.signal });
      const data = await response.json();
      if (state.controller !== controller) return;
      if (!response.ok) {
        const feedback = describeRequestError(data, 'Could not load the effective policy.', `HTTP ${response.status}`);
        throw new Error([feedback.message, ...feedback.details].join('\n'));
      }
      renderPolicy(data);
      state.loadedKey = key;
      setPreviewFeedback();
      retry.hidden = true;
    } catch (error) {
      if (error.name !== 'AbortError' && state.controller === controller) {
        setPreviewFeedback(error.message || 'Could not load the effective policy.');
        retry.hidden = false;
      }
    } finally {
      if (state.controller === controller) {
        state.controller = null;
        setRefreshBusy(retry, false);
      }
    }
  }

  function renderPolicy(data) {
    const declaredOutput = data.declared_model_max_output_tokens ?? null;
    const memory = data.summary_policy;
    const generationReady = memory?.state === 'ready' && memory.generation_enabled !== false;
    const planningHelp = {
      missing_input_capacity: 'Add a declared maximum input or context window.',
      missing_output_headroom: 'Add summary output headroom, an explicit output limit, or declared maximum output.',
      invalid_capacity: 'The context window must exceed summary output headroom.',
      capacity_too_small: 'The effective summary trigger is too small to plan memory.'
    };
    const headroomSources = { planning: 'Planning value', output_limit: 'Selected output limit', declared_output: 'Declared maximum output', unknown: 'Unverified' };
    if (!['warn', 'block', 'off'].includes(data.mode) || (data.input_budget !== null && (!Number.isSafeInteger(data.input_budget) || data.input_budget < (data.legacy_input_policy ? 0 : 1)))
      || (data.max_output_tokens !== null && (!Number.isSafeInteger(data.max_output_tokens) || data.max_output_tokens < 1))
      || (declaredOutput !== null && (!Number.isSafeInteger(declaredOutput) || declaredOutput < 1))
      || (data.declared_model_input_limit !== null && (!Number.isSafeInteger(data.declared_model_input_limit) || data.declared_model_input_limit < 0))) {
      throw new Error('Could not read the effective policy.');
    }
    const budget = data.input_budget === null ? 'No input limit' : `${data.input_budget.toLocaleString()} tokens`;
    const sources = { app: 'App setting', profile: 'Profile setting', app_and_profile: 'App and profile settings' };
    const budgetSource = data.legacy_input_policy
      ? (data.input_budget_inherited ? 'Previous app policy' : 'Previous profile policy')
      : (sources[data.input_budget_source] || (data.input_budget_inherited ? sources.app : null));
    const inputCheck = data.mode.charAt(0).toUpperCase() + data.mode.slice(1);
    const values = [
      ['Input budget', withSource(budget, budgetSource)],
      ['Input check', withSource(inputCheck, sources[data.input_check_source])],
      ['Output limit', data.max_output_tokens === null ? 'Provider default' : withSource(`${data.max_output_tokens.toLocaleString()} tokens`, sources[data.output_limit_source])],
      ['Declared input ceiling', data.declared_model_input_limit === null ? 'Unknown' : `${data.declared_model_input_limit.toLocaleString()} tokens`],
      ['Declared maximum output', declaredOutput === null ? 'Unknown' : `${declaredOutput.toLocaleString()} tokens`],
      ['Memory', describeMemoryState(memory)],
      ['Summary trigger', generationReady && Number.isSafeInteger(memory.trigger_tokens) ? `${memory.trigger_tokens.toLocaleString()} tokens (${memory.mode === 'auto' ? 'Automatic' : 'Custom'})` : 'Inactive'],
    ];
    if (generationReady) values.push(
      ['Summary input per call', `${memory.summary_input_limit.toLocaleString()} tokens`],
      ['Output planning headroom', memory.output_headroom === null ? 'Unverified' : `${memory.output_headroom.toLocaleString()} tokens (${headroomSources[memory.headroom_source] || 'Unverified'})`]
    );
    if (generationReady) {
      for (const [label, value] of [
        ['Recent history target', memory.effective_keep_recent_tokens],
        ['Summary text target', memory.summary_target_tokens],
        ['Post-summary input target', memory.compaction_target_tokens],
      ]) {
        if (Number.isSafeInteger(value)) values.push([label, `${value.toLocaleString()} tokens`]);
      }
    }
    const list = document.createElement('dl');
    list.className = 'settings-policy-list';
    for (const [label, value] of values) {
      const row = document.createElement('div');
      const term = document.createElement('dt');
      const description = document.createElement('dd');
      term.textContent = label;
      description.textContent = value;
      row.append(term, description);
      list.append(row);
    }
    const notes = [
      memory?.state === 'needs_info' ? planningHelp[memory.reason] || 'Check declared deployment capacity before creating new summaries.' : '',
      generationReady && memory.input_only ? 'Shared context capacity is unverified; planning uses declared maximum input only.' : '',
      generationReady && memory.trigger_adjusted ? `The configured summary trigger is ${memory.configured_trigger_tokens.toLocaleString()} tokens. Its effective value is limited to this deployment's input allowance; the app preference is unchanged.` : '',
      generationReady && memory.effective_keep_recent_tokens < memory.keep_recent_tokens ? 'Recent-history and summary targets share 20% of the effective trigger. Complete turns and essential memory may exceed these targets.' : '',
      generationReady && Number.isSafeInteger(memory.summary_target_tokens) ? 'Missing a target does not request another summary pass. Long prefixes can still need multiple calls; API output caps are unchanged.' : '',
      describeEstimation(data) || (generationReady ? describeEstimation(memory) : '')
    ].filter(Boolean);
    const content = [list];
    if (notes.length) {
      const noteGroup = document.createElement('div');
      noteGroup.className = 'settings-policy-notes';
      for (const note of notes) {
        const paragraph = document.createElement('p');
        paragraph.textContent = note;
        noteGroup.append(paragraph);
      }
      content.push(noteGroup);
    }
    policyValues.replaceChildren(...content);
    state.usesLegacyAppBudget = data.app_input_budget_mode === 'legacy';
    updateBudgetReview();
  }

  function withSource(value, source) {
    return source ? `${value} · ${source}` : value;
  }

  function changes(editing) {
    const result = {};
    for (const [name, input] of fields) {
      const inactiveBudget = name === 'context_input_budget' && ['inherit', 'unlimited'].includes(fields.get('context_input_budget_mode').value);
      const value = inactiveBudget || input.value === '' ? null : name === 'memory_enabled' ? input.value === 'true' : input.tagName === 'SELECT' ? input.value : input.valueAsNumber;
      if (!editing || value !== savedValue(name)) result[name] = value;
    }
    return result;
  }

  function fieldError(input) {
    if (input.disabled || input.tagName === 'SELECT') return '';
    const required = input.dataset.profileContext === 'context_input_budget';
    if (!required && input.value === '' && !input.validity.badInput) return '';

    const value = input.valueAsNumber;
    const blankHint = required ? '' : ', or leave blank';
    if (value > Number.MAX_SAFE_INTEGER) return `Enter a whole number no greater than ${Number.MAX_SAFE_INTEGER.toLocaleString()}${blankHint}.`;
    if (input.validity.badInput || !Number.isSafeInteger(value) || value < 1) {
      return `Enter a whole number of at least 1${blankHint}.`;
    }
    return '';
  }

  function validate() {
    for (const input of fields.values()) input.setCustomValidity(fieldError(input));
  }

  function init() {
    for (const input of fields.values()) {
      input.addEventListener('input', () => {
        input.setCustomValidity('');
        updateControls();
        schedulePreview(250);
      });
    }
    resets.forEach(button => {
      button.onclick = () => {
        const input = fields.get(button.dataset.clearProfileContext);
        input.value = button.dataset.clearProfileContext === 'context_input_budget_mode' ? 'inherit' : '';
        if (button.dataset.clearProfileContext === 'memory_enabled') input.value = 'false';
        if (['context_input_budget', 'context_input_budget_mode'].includes(button.dataset.clearProfileContext)) {
          fields.get('context_input_budget_mode').value = 'inherit';
          fields.get('context_input_budget').value = '';
        }
        input.dispatchEvent(new Event('input', { bubbles: true }));
        const target = button.dataset.clearProfileContext === 'context_input_budget' ? fields.get('context_input_budget_mode') : input;
        target.focus({ preventScroll: true });
      };
    });
    disclosure.addEventListener('toggle', () => schedulePreview());
    retry.onclick = () => schedulePreview(0, { retrying: true });
    dialog.addEventListener('settings-panel-change', () => schedulePreview());
    dialog.addEventListener('tokenizer-changed', () => {
      invalidate();
      schedulePreview();
    });
    dialog.addEventListener('app-settings-saved', () => {
      invalidate();
      updateBudgetReview();
      schedulePreview();
    });
    updateControls();
  }

  return { init, select, changes, validate, updateControls };
}
