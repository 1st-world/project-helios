/* Edit independent profile context limits and model capacity declarations, and fetch their saved policy without provider calls. */

import { $ } from './ui.js';
import { PREFLIGHT_HELP, describeMemoryState, describeRequestError } from './chat-feedback.js';
import { describeEstimation } from './estimation.js';

export function createProfileContext({ isSaving }) {
  const disclosure = $('#profile-context-settings');
  const fields = new Map([...disclosure.querySelectorAll('[data-profile-context]')].map(input => [input.dataset.profileContext, input]));
  const resets = [...disclosure.querySelectorAll('[data-clear-profile-context]')];
  const state = { profile: null, controller: null, loaded: false };

  function savedValue(name) {
    if (name === 'memory_enabled') return state.profile ? state.profile[name] ?? null : false;
    if (name === 'context_input_budget_mode') return state.profile ? state.profile[name] ?? 'legacy' : 'inherit';
    if (name === 'output_token_limit' && state.profile && !Object.hasOwn(state.profile, name)) {
      return state.profile.model_max_output_tokens ?? null;
    }
    return state.profile?.[name] ?? null;
  }

  function updateControls() {
    const budgetMode = fields.get('context_input_budget_mode').value;
    fields.get('context_input_budget').disabled = isSaving() || budgetMode !== 'custom';
    fields.get('context_input_budget').closest('.settings-preference-row').hidden = budgetMode !== 'custom';
    fields.get('context_input_budget_mode').querySelector('[value="legacy"]').hidden = savedValue('context_input_budget_mode') !== 'legacy';
    fields.get('memory_enabled').querySelector('[value=""]').hidden = savedValue('memory_enabled') !== null;
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

  function invalidate() {
    state.controller?.abort();
    state.controller = null;
    state.loaded = false;
    $('#profile-context-retry').hidden = true;
    $('#profile-context-effective').textContent = state.profile
      ? 'Saved policy is not loaded.'
      : 'No saved policy for this new profile.';
  }

  function select(profile) {
    state.profile = profile;
    invalidate();
    for (const [name, input] of fields) {
      input.value = savedValue(name) ?? '';
      input.setCustomValidity('');
      input.removeAttribute('aria-invalid');
    }
    updateControls();
    load();
  }

  async function load() {
    if (!state.profile || state.loaded || state.controller || !disclosure.open
      || !$('#settings-dialog').open || $('#settings-connections').hidden) return;
    const controller = new AbortController();
    state.controller = controller;
    $('#profile-context-retry').hidden = true;
    $('#profile-context-effective').textContent = 'Loading saved effective policy...';
    try {
      const response = await fetch(`/api/context-policy?profile_id=${encodeURIComponent(state.profile.id)}`, { signal: controller.signal });
      const data = await response.json();
      if (state.controller !== controller) return;
      if (!response.ok) {
        const feedback = describeRequestError(data, 'Could not load the saved policy.', `HTTP ${response.status}`);
        throw new Error([feedback.message, ...feedback.details].join('\n'));
      }
      const declaredOutput = data.declared_model_max_output_tokens ?? null;
      const memory = data.summary_policy;
      const planningHelp = {
        missing_input_capacity: 'Add a declared maximum input or context window.',
        missing_output_headroom: 'Add summary output headroom, an explicit output limit, or declared maximum output.',
        invalid_capacity: 'The context window must exceed summary output headroom.',
        capacity_too_small: 'Declared capacity is too small for a positive automatic threshold.'
      };
      const headroomSources = { planning: 'Planning value', output_limit: 'Selected output limit', declared_output: 'Declared maximum output', unknown: 'Unverified' };
      if (!['warn', 'block', 'off'].includes(data.mode) || (data.input_budget !== null && (!Number.isSafeInteger(data.input_budget) || data.input_budget < (data.legacy_input_policy ? 0 : 1)))
        || (data.max_output_tokens !== null && (!Number.isSafeInteger(data.max_output_tokens) || data.max_output_tokens < 1))
        || (declaredOutput !== null && (!Number.isSafeInteger(declaredOutput) || declaredOutput < 1))
        || (data.declared_model_input_limit !== null && (!Number.isSafeInteger(data.declared_model_input_limit) || data.declared_model_input_limit < 0))) {
        throw new Error('Could not read the saved effective policy.');
      }
      const values = [
        ['Input budget', data.input_budget === null ? 'No input limit' : `${data.input_budget.toLocaleString()} tokens`],
        ['Input budget source', { none: 'No budget', app: 'App setting', profile: 'Profile setting', request: 'Request setting', legacy: 'Previous policy' }[data.input_budget_source] || 'Saved policy'],
        ['Input check', data.mode.charAt(0).toUpperCase() + data.mode.slice(1)],
        ['Output limit', data.max_output_tokens === null ? 'Provider default' : `${data.max_output_tokens.toLocaleString()} tokens`],
        ['Declared input ceiling', data.declared_model_input_limit === null ? 'Unknown' : `${data.declared_model_input_limit.toLocaleString()} tokens`],
        ['Declared maximum output', declaredOutput === null ? 'Unknown' : `${declaredOutput.toLocaleString()} tokens`],
        ['Memory', describeMemoryState(memory)],
        ['Summary trigger', memory?.state === 'ready' && Number.isSafeInteger(memory.trigger_tokens) ? `${memory.trigger_tokens.toLocaleString()} tokens (${memory.mode === 'auto' ? 'Automatic' : 'Custom'})` : 'Inactive'],
      ];
      if (memory?.state === 'ready') values.push(
        ['Summary input per call', `${memory.summary_input_limit.toLocaleString()} tokens`],
        ['Output planning headroom', memory.output_headroom === null ? 'Unverified' : `${memory.output_headroom.toLocaleString()} tokens (${headroomSources[memory.headroom_source] || 'Unverified'})`]
      );
      const container = $('#profile-context-effective');
      const list = document.createElement('dl');
      list.className = 'settings-policy-list';
      for (const [label, value] of values) {
        const row = document.createElement('div');
        const term = document.createElement('dt');
        const description = document.createElement('dd');
        term.textContent = `${label}: `;
        description.textContent = value;
        row.append(term, description);
        list.append(row);
      }
      container.replaceChildren(list);
      const notes = [
        data.legacy_input_policy ? 'Review needed: previous input and model capacity limits are preserved. Choose an input budget policy to finish review.' : '',
        memory?.state === 'needs_info' ? planningHelp[memory.reason] || 'Check declared deployment capacity before using memory.' : '',
        memory?.input_only ? 'Shared context capacity is unverified; planning uses declared maximum input only.' : '',
        describeEstimation(data) || (memory?.state === 'ready' ? describeEstimation(memory) : '')
      ];
      for (const note of notes.filter(Boolean)) {
        const paragraph = document.createElement('p');
        paragraph.textContent = note;
        container.append(paragraph);
      }
      state.loaded = true;
    } catch (error) {
      if (error.name !== 'AbortError' && state.controller === controller) {
        $('#profile-context-effective').textContent = error.message || 'Could not load the saved effective policy.';
        $('#profile-context-retry').hidden = false;
      }
    } finally {
      if (state.controller === controller) state.controller = null;
    }
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

  function validate() {
    for (const input of fields.values()) {
      input.setCustomValidity('');
      input.removeAttribute('aria-invalid');
      if (input.disabled) continue;
      if (input.dataset.profileContext === 'context_input_budget' && fields.get('context_input_budget_mode').value === 'custom' && input.value === '') {
        input.setCustomValidity('Enter a positive token limit for the custom budget.');
        input.setAttribute('aria-invalid', 'true');
      }
      if (input.tagName !== 'SELECT' && (input.validity.badInput || (input.value !== '' && (!Number.isSafeInteger(input.valueAsNumber) || input.valueAsNumber < 1)))) {
        input.setCustomValidity('Enter a positive whole number the browser can represent exactly, or leave blank.');
        input.setAttribute('aria-invalid', 'true');
      }
    }
  }

  function init() {
    for (const input of fields.values()) {
      input.addEventListener('input', () => {
        input.setCustomValidity('');
        input.removeAttribute('aria-invalid');
        updateControls();
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
    disclosure.addEventListener('toggle', load);
    $('#profile-context-retry').onclick = load;
    $('#settings-dialog').addEventListener('settings-panel-change', event => { if (event.detail.name === 'connections') load(); });
    $('#settings-dialog').addEventListener('app-settings-saved', () => { invalidate(); load(); });
    updateControls();
  }

  return { init, select, changes, validate, updateControls };
}
