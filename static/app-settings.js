/* Edit app-wide context and attachment policies while keeping saved enforcement limits separate from drafts. */

import { $, toast } from './ui.js';
import { describeRequestError } from './chat-feedback.js';

export function createAppSettings({ hasUnsavedProfileChanges }) {
  const form = $('#app-settings-form');
  const fields = new Map([...form.querySelectorAll('[data-app-setting]')].map(input => [input.dataset.appSetting, input]));
  const resetButtons = [...form.querySelectorAll('[data-reset-setting]')];
  const resetLabels = new Map(resetButtons.map(button => [button, button.getAttribute('aria-label')]));
  const effectiveLabels = [...form.querySelectorAll('[data-effective-setting]')];
  const state = { saved: null, defaults: null, localLimits: null, providerLimits: null, resets: new Set(), loading: false, saving: false, controller: null };
  const modeHelp = {
    warn: 'Allows the request and reports approximate input overruns. Preflight estimates alone do not force history compaction.',
    block: 'Uses approximate estimates to reject oversized input or summarize history to fit. Estimates can differ from actual model usage.',
    off: 'Skips input preflight estimates. Local file checks, provider limits, and background memory summaries still apply.'
  };

  function setStatus(message, type = 'info') {
    const status = $('#app-settings-status');
    status.textContent = message;
    status.dataset.state = type;
  }

  function valueOf(input) {
    return input.tagName === 'SELECT' ? input.value : input.valueAsNumber;
  }

  function hasUnsavedChanges() {
    return Boolean(state.saved && (state.resets.size || [...fields].some(([name, input]) => valueOf(input) !== state.saved[name])));
  }

  function showAttachmentLimits() {
    for (const label of effectiveLabels) {
      const value = state.localLimits?.[label.dataset.limitKey];
      const configured = state.saved?.[label.dataset.effectiveSetting];
      const clamped = Number.isSafeInteger(value) && value < configured;
      label.textContent = Number.isSafeInteger(value) && value >= 1
        ? `Saved effective local limit: ${value.toLocaleString()} ${label.dataset.unit}.${clamped ? ' The server applies a lower ceiling than the saved preference.' : ''}`
        : 'Saved effective local limit: unavailable.';
    }
    const limits = state.providerLimits;
    const values = [limits?.max_images, limits?.native_file_bytes_exclusive, limits?.native_category_total_bytes_exclusive];
    $('#attachment-provider-limits').textContent = values.every(value => Number.isSafeInteger(value) && value >= 1)
      ? `Server-enforced provider limits: at most ${values[0].toLocaleString()} images; each native image or PDF must be under ${values[1].toLocaleString()} bytes. Combined image bytes and combined PDF bytes must each stay under ${values[2].toLocaleString()} bytes. Local preferences cannot raise these limits.`
      : 'Saved provider limits are unavailable. Local preferences cannot override provider restrictions.';
  }

  function updateControls() {
    const busy = state.loading || state.saving;
    let defaultsDiffer = false;
    for (const input of fields.values()) input.disabled = busy || !state.saved;
    resetButtons.forEach(button => {
      const name = button.dataset.resetSetting;
      const differs = Boolean(state.defaults && valueOf(fields.get(name)) !== state.defaults[name]);
      defaultsDiffer ||= differs;
      button.hidden = !differs;
      button.disabled = busy || !state.saved || !differs;
    });
    $('#restore-app-defaults').disabled = busy || !state.saved || !defaultsDiffer;
    $('#cancel-app-settings').disabled = state.saving;
    $('#save-app-settings').disabled = busy || !hasUnsavedChanges();
    $('#save-app-settings-label').textContent = state.saving ? 'Saving...' : 'Save settings';
    $('#retry-app-settings').hidden = Boolean(state.saved) || state.loading;
    $('#app-settings-dirty').textContent = hasUnsavedChanges() ? 'Unsaved changes' : '';
    $('#context-preflight-help').textContent = modeHelp[fields.get('context_preflight_mode').value] || 'Choose how local input estimates are used.';
    const budget = fields.get('context_token_budget').valueAsNumber - fields.get('context_output_reserve').valueAsNumber;
    $('#app-input-budget').textContent = `App input allowance before profile or request overrides: ${Number.isSafeInteger(budget) && budget >= 0 ? `${budget.toLocaleString()} tokens` : '—'}.`;
    showAttachmentLimits();
  }

  function clearValidation() {
    for (const input of fields.values()) {
      input.setCustomValidity('');
      input.removeAttribute('aria-invalid');
    }
  }

  function fillSaved() {
    state.resets.clear();
    clearValidation();
    for (const [name, input] of fields) input.value = state.saved ? state.saved[name] : '';
    resetButtons.forEach(button => {
      const value = state.defaults?.[button.dataset.resetSetting];
      const label = resetLabels.get(button);
      const formatted = value === undefined ? '' : typeof value === 'number' ? value.toLocaleString() : value.charAt(0).toUpperCase() + value.slice(1);
      button.title = value === undefined ? label : `${label} (default: ${formatted})`;
      button.setAttribute('aria-label', button.title);
    });
    updateControls();
  }

  function acceptResponse(data) {
    for (const name of fields.keys()) {
      for (const values of [data?.settings, data?.defaults]) {
        const value = values?.[name];
        if (name === 'context_preflight_mode' ? !Object.hasOwn(modeHelp, value) : !Number.isSafeInteger(value) || value < 1) {
          throw new Error('Could not read the saved settings. Reload Settings before editing.');
        }
      }
    }
    state.saved = { ...data.settings };
    state.defaults = { ...data.defaults };
    state.localLimits = { ...data.local_attachment_limits };
    state.providerLimits = { ...data.provider_limits };
    fillSaved();
  }

  async function readResponse(response) {
    let data;
    try { data = await response.json(); }
    catch { throw new Error(`Could not read the Settings response. (HTTP ${response.status})`); }
    if (!response.ok) {
      const feedback = describeRequestError(data, 'Could not update app settings.', `HTTP ${response.status}`);
      throw new Error([feedback.message, ...feedback.details].join('\n'));
    }
    return data;
  }

  async function load() {
    if (state.loading || state.saving || state.saved || !$('#settings-dialog').open) return;
    const controller = new AbortController();
    state.controller = controller;
    state.loading = true;
    setStatus('Loading saved app settings...');
    updateControls();
    try {
      const response = await fetch('/api/settings', { signal: controller.signal });
      const data = await readResponse(response);
      if (state.controller !== controller || !$('#settings-dialog').open) return;
      acceptResponse(data);
      setStatus('Saved app settings loaded.');
    } catch (error) {
      if (error.name !== 'AbortError' && state.controller === controller) setStatus(error.message || 'Could not load app settings.', 'error');
    } finally {
      if (state.controller === controller) {
        state.controller = null;
        state.loading = false;
        updateControls();
      }
    }
  }

  function restoreDefault(name) {
    if (!state.saved || state.loading || state.saving) return;
    const input = fields.get(name);
    const restoreFocus = document.activeElement?.dataset.resetSetting === name;
    input.value = state.defaults[name];
    state.resets.add(name);
    clearValidation();
    setStatus('Default values are staged. Save settings to apply them.');
    updateControls();
    if (restoreFocus) input.focus({ preventScroll: true });
  }

  function validate() {
    clearValidation();
    for (const input of fields.values()) {
      if (input.tagName !== 'SELECT' && (!Number.isSafeInteger(input.valueAsNumber) || input.valueAsNumber < Number(input.min))) {
        input.setCustomValidity(`Enter a whole number of at least ${input.min} that the browser can represent exactly.`);
      }
    }
    const budget = fields.get('context_token_budget');
    const reserve = fields.get('context_output_reserve');
    if (budget.valueAsNumber - reserve.valueAsNumber < 1024) budget.setCustomValidity('The context budget must leave at least 1,024 input tokens after reserving output.');
    const recent = fields.get('keep_recent_messages');
    if (recent.valueAsNumber >= fields.get('max_context_messages').valueAsNumber) recent.setCustomValidity('Keep recent messages must be smaller than the summary trigger.');
    for (const input of fields.values()) {
      if (!input.checkValidity()) input.setAttribute('aria-invalid', 'true');
    }
    const invalid = [...fields.values()].find(input => !input.checkValidity());
    if (invalid) {
      const disclosure = invalid.closest('details');
      if (disclosure) disclosure.open = true;
      invalid.reportValidity();
      setStatus('Review the highlighted settings before saving.', 'error');
      return false;
    }
    return true;
  }

  async function save(event) {
    event.preventDefault();
    if (!state.saved || state.loading || state.saving || !hasUnsavedChanges() || !validate()) return;
    const changes = {};
    for (const [name, input] of fields) {
      if (state.resets.has(name)) changes[name] = null;
      else if (valueOf(input) !== state.saved[name]) changes[name] = valueOf(input);
    }
    state.saving = true;
    setStatus('Saving app settings...');
    updateControls();
    try {
      const response = await fetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes)
      });
      acceptResponse(await readResponse(response));
      setStatus('Settings saved. Changes apply to new requests; running requests keep their settings.', 'success');
    } catch (error) {
      setStatus(`${error.message || 'Could not confirm the save.'}\nYour edits are kept. Reopen Settings to check saved values if the connection was interrupted.`, 'error');
    } finally {
      state.saving = false;
      updateControls();
    }
  }

  function beforeDialogClose({ discardProfile = false, discardContext = false } = {}) {
    if (state.saving) {
      toast('Wait for the Settings save to finish before closing.', 'info');
      return false;
    }
    if ((discardContext || !hasUnsavedChanges()) && (discardProfile || !hasUnsavedProfileChanges())) return true;
    return window.confirm('Discard unsaved Settings changes and close?');
  }

  function init() {
    form.onsubmit = save;
    for (const [name, input] of fields) {
      input.addEventListener('input', () => {
        state.resets.delete(name);
        clearValidation();
        setStatus(hasUnsavedChanges() ? 'Changes are not saved yet.' : 'Saved values restored.');
        updateControls();
      });
    }
    resetButtons.forEach(button => { button.onclick = () => restoreDefault(button.dataset.resetSetting); });
    $('#restore-app-defaults').onclick = () => {
      const restoreFocus = document.activeElement === $('#restore-app-defaults');
      for (const name of fields.keys()) restoreDefault(name);
      if (restoreFocus) $('#save-app-settings').focus({ preventScroll: true });
    };
    $('#cancel-app-settings').onclick = () => {
      if (beforeDialogClose({ discardContext: true })) $('#settings-dialog').close();
    };
    $('#retry-app-settings').onclick = load;
    $('#settings-dialog').addEventListener('settings-panel-change', event => {
      if (event.detail.name === 'context') load();
    });
    $('#settings-dialog').addEventListener('close', () => {
      if ($('#settings-dialog').open) return;
      state.controller?.abort();
      state.controller = null;
      state.loading = false;
      state.saved = null;
      state.defaults = null;
      state.localLimits = null;
      state.providerLimits = null;
      $('#attachment-resource-settings').open = false;
      fillSaved();
      setStatus('');
    });
    fillSaved();
  }

  return { init, beforeDialogClose };
}
