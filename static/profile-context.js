/* Edit independent profile context limits and model capacity declarations, and fetch their saved policy without provider calls. */

import { $ } from './ui.js';
import { describeRequestError } from './chat-feedback.js';

export function createProfileContext({ isSaving }) {
  const disclosure = $('#profile-context-settings');
  const fields = new Map([...disclosure.querySelectorAll('[data-profile-context]')].map(input => [input.dataset.profileContext, input]));
  const resets = [...disclosure.querySelectorAll('[data-clear-profile-context]')];
  const state = { profile: null, controller: null, loaded: false };
  const modeHelp = {
    warn: 'Allows requests with estimated input-limit warnings. Warnings alone do not force history summaries.',
    block: 'Rejects requests when input estimates exceed selected limits. Summaries follow the separate memory threshold.',
    off: 'Skips admission estimates. Explicit output limits, attachment checks, and token-based memory summaries still apply.'
  };

  function savedValue(name) {
    if (name === 'context_input_budget_mode') return state.profile ? state.profile[name] ?? 'legacy' : 'inherit';
    if (name === 'output_token_limit' && state.profile && !Object.hasOwn(state.profile, name)) {
      return state.profile.model_max_output_tokens ?? null;
    }
    return state.profile?.[name] ?? null;
  }

  function updateControls() {
    const budgetMode = fields.get('context_input_budget_mode').value;
    fields.get('context_input_budget').disabled = isSaving() || budgetMode !== 'custom';
    fields.get('context_input_budget_mode').querySelector('[value="legacy"]').hidden = savedValue('context_input_budget_mode') !== 'legacy';
    resets.forEach(button => {
      const input = fields.get(button.dataset.clearProfileContext);
      button.hidden = button.dataset.clearProfileContext === 'context_input_budget_mode' ? input.value === 'inherit' : !input.value && !input.validity.badInput;
      if (button.dataset.clearProfileContext === 'context_input_budget' && budgetMode !== 'custom') button.hidden = true;
      button.disabled = isSaving() || button.hidden;
    });
    $('#profile-context-preflight-help').textContent = modeHelp[fields.get('context_preflight_mode').value]
      || 'Uses the app preflight mode. Any explicit output limit still applies.';
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
      if (!['warn', 'block', 'off'].includes(data.mode) || (data.input_budget !== null && (!Number.isSafeInteger(data.input_budget) || data.input_budget < (data.legacy_input_policy ? 0 : 1)))
        || (data.max_output_tokens !== null && (!Number.isSafeInteger(data.max_output_tokens) || data.max_output_tokens < 1))
        || (declaredOutput !== null && (!Number.isSafeInteger(declaredOutput) || declaredOutput < 1))
        || (data.declared_model_input_limit !== null && (!Number.isSafeInteger(data.declared_model_input_limit) || data.declared_model_input_limit < 0))) {
        throw new Error('Could not read the saved effective policy.');
      }
      $('#profile-context-effective').textContent = [
        `Preflight: ${data.mode.charAt(0).toUpperCase() + data.mode.slice(1)}.`,
        `Input estimate budget: ${data.input_budget === null ? 'No input limit' : `${data.input_budget.toLocaleString()} tokens`}.`,
        `Input budget source: ${data.input_budget_source || 'Saved policy'}.`,
        data.legacy_input_policy ? 'Review needed: previous input and model capacity limits are preserved. Choose an input budget policy to finish review.' : 'Model capacity declarations do not impose an input budget.',
        `Declared input ceiling: ${data.declared_model_input_limit === null ? 'Unknown' : `${data.declared_model_input_limit.toLocaleString()} tokens`}.`,
        `Declared maximum output: ${declaredOutput === null ? 'Unknown' : `${declaredOutput.toLocaleString()} tokens`}.`,
        `Output limit: ${data.max_output_tokens === null ? 'Provider default' : `${data.max_output_tokens.toLocaleString()} tokens`}.`,
        `Summary trigger: ${data.summary_policy?.mode === 'disabled' ? 'Disabled' : Number.isSafeInteger(data.summary_policy?.trigger_tokens) ? `${data.summary_policy.trigger_tokens.toLocaleString()} tokens (${data.summary_policy.mode})` : 'Automatic; model capacity unknown'}.`
      ].join('\n');
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
      const value = inactiveBudget || input.value === '' ? null : input.tagName === 'SELECT' ? input.value : input.valueAsNumber;
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
        if (['context_input_budget', 'context_input_budget_mode'].includes(button.dataset.clearProfileContext)) {
          fields.get('context_input_budget_mode').value = 'inherit';
          fields.get('context_input_budget').value = '';
        }
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.focus({ preventScroll: true });
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
