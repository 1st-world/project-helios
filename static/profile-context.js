/* Edit optional profile context declarations and fetch their saved effective policy without provider calls. */

import { $ } from './ui.js';
import { describeRequestError } from './chat-feedback.js';

export function createProfileContext({ isSaving }) {
  const disclosure = $('#profile-context-settings');
  const fields = new Map([...disclosure.querySelectorAll('[data-profile-context]')].map(input => [input.dataset.profileContext, input]));
  const resets = [...disclosure.querySelectorAll('[data-clear-profile-context]')];
  const state = { profile: null, controller: null, loaded: false };
  const modeHelp = {
    warn: 'Allows requests with estimated input-limit warnings. Warnings alone do not force history summaries.',
    block: 'May summarize history or reject requests when input estimates exceed limits. Estimates can differ from actual usage.',
    off: 'Skips input estimates. Output caps, attachment checks, provider limits, and background memory summaries still apply.'
  };

  function updateControls() {
    resets.forEach(button => {
      const input = fields.get(button.dataset.clearProfileContext);
      button.hidden = !input.value && !input.validity.badInput;
      button.disabled = isSaving() || button.hidden;
    });
    $('#profile-context-preflight-help').textContent = modeHelp[fields.get('context_preflight_mode').value]
      || 'Uses the app preflight mode. Output caps still apply.';
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
      input.value = profile?.[name] ?? '';
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
      if (!['warn', 'block', 'off'].includes(data.mode) || !Number.isSafeInteger(data.input_budget) || data.input_budget < 1
        || !Number.isSafeInteger(data.max_output_tokens) || data.max_output_tokens < 1
        || (data.declared_model_input_limit !== null && (!Number.isSafeInteger(data.declared_model_input_limit) || data.declared_model_input_limit < 0))) {
        throw new Error('Could not read the saved effective policy.');
      }
      $('#profile-context-effective').textContent = [
        `Preflight: ${data.mode.charAt(0).toUpperCase() + data.mode.slice(1)}.`,
        `Input estimate budget: ${data.input_budget.toLocaleString()} tokens.`,
        `Declared input ceiling: ${data.declared_model_input_limit === null ? 'Unknown' : `${data.declared_model_input_limit.toLocaleString()} tokens`}.`,
        `Output cap: ${data.max_output_tokens.toLocaleString()} tokens.`
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
      const value = input.value === '' ? null : input.tagName === 'SELECT' ? input.value : input.valueAsNumber;
      if (!editing || value !== (state.profile?.[name] ?? null)) result[name] = value;
    }
    return result;
  }

  function validate() {
    for (const input of fields.values()) {
      input.setCustomValidity('');
      input.removeAttribute('aria-invalid');
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
        input.value = '';
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
