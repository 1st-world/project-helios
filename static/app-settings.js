/* Edit app-wide context and attachment policies while keeping saved enforcement limits separate from drafts. */

import { $, toast } from './ui.js';
import { PREFLIGHT_HELP, describeMemoryState, describeRequestError } from './chat-feedback.js';
import { showActionDialog } from './dialogs.js';

export function createAppSettings({ hasUnsavedProfileChanges, isProfileSaving }) {
  const form = $('#app-settings-form');
  const fields = new Map([...form.querySelectorAll('[data-app-setting]')].map(input => [input.dataset.appSetting, input]));
  const resetButtons = [...form.querySelectorAll('[data-reset-setting]')];
  const resetLabels = new Map(resetButtons.map(button => [button, button.getAttribute('aria-label')]));
  const effectiveLabels = [...form.querySelectorAll('[data-effective-setting]')];
  const state = { saved: null, defaults: null, localLimits: null, summaryPolicy: null, resets: new Set(), loading: false, saving: false, controller: null, policyController: null, feedback: null };

  function setStatus(message, type = 'info') {
    state.feedback = message ? { message, type } : null;
    renderStatus();
  }

  function renderStatus() {
    const status = $('#app-settings-status');
    const dirty = hasUnsavedChanges();
    status.textContent = state.feedback?.message || (dirty ? 'Unsaved changes' : '');
    status.dataset.state = state.feedback?.type || (dirty ? 'dirty' : 'idle');
  }

  function valueOf(input) {
    if (input.dataset.appSetting === 'context_input_budget' && fields.get('context_input_budget_mode').value !== 'custom') return null;
    if (['context_output_reserve', 'summary_trigger_tokens', 'context_input_budget'].includes(input.dataset.appSetting) && input.value === '' && !input.validity.badInput) return null;
    return input.tagName === 'SELECT' ? input.value : input.valueAsNumber;
  }

  function hasUnsavedChanges() {
    return Boolean(state.saved && [...fields].some(([name, input]) => valueOf(input) !== state.saved[name]));
  }

  function showAttachmentLimits() {
    for (const label of effectiveLabels) {
      const value = state.localLimits?.[label.dataset.limitKey];
      const configured = state.saved?.[label.dataset.effectiveSetting];
      const available = Number.isSafeInteger(value) && value >= 1;
      const input = fields.get(label.dataset.effectiveSetting);
      const clamped = available && value < configured && value < valueOf(input);
      label.textContent = clamped ? `Server ceiling: ${value.toLocaleString()} ${label.dataset.unit}.`
        : state.saved && !available ? 'Effective limit unavailable.' : '';
      label.hidden = !label.textContent;
    }
    $('#attachment-provider-limits').textContent = 'Limits for local reading and extraction; the provider checks model capacity. Byte values are exact, and server ceilings may lower them. These limits do not guarantee memory use or processing time.';
  }

  function updateControls() {
    const busy = state.loading || state.saving || isProfileSaving();
    let defaultsDiffer = false;
    for (const input of fields.values()) input.disabled = busy || !state.saved;
    const inputMode = fields.get('context_input_budget_mode').value;
    fields.get('context_input_budget').disabled ||= inputMode !== 'custom';
    fields.get('context_input_budget').closest('.settings-preference-row').hidden = inputMode !== 'custom';
    fields.get('context_input_budget_mode').querySelector('[value="legacy"]').hidden = state.saved?.context_input_budget_mode !== 'legacy';
    resetButtons.forEach(button => {
      const name = button.dataset.resetSetting;
      const differs = Boolean(state.defaults && valueOf(fields.get(name)) !== state.defaults[name]);
      defaultsDiffer ||= differs;
      button.hidden = !differs;
      button.disabled = busy || !state.saved || !differs;
    });
    $('#restore-app-defaults').disabled = busy || !state.saved || !defaultsDiffer;
    $('#discard-app-settings').disabled = busy || !hasUnsavedChanges();
    $('#save-app-settings').disabled = busy || !hasUnsavedChanges();
    $('#save-app-settings-label').textContent = state.saving ? 'Saving...' : 'Save changes';
    renderStatus();
    $('#context-preflight-help').textContent = PREFLIGHT_HELP[fields.get('context_preflight_mode').value] || 'How to handle estimates above the selected input limits.';
    const output = valueOf(fields.get('context_output_reserve'));
    const budget = inputMode === 'legacy' ? fields.get('context_token_budget').valueAsNumber - (output ?? 0) : valueOf(fields.get('context_input_budget'));
    const inputText = inputMode === 'unlimited' ? 'No input limit' : Number.isSafeInteger(budget) && budget >= 0 ? `${budget.toLocaleString()} tokens` : 'Enter a custom budget';
    $('#app-input-budget').textContent = `Draft input budget: ${inputText}.\nOutput limit: ${output === null ? 'Provider default' : `${output.toLocaleString()} tokens`}.`;
    $('#app-input-policy-review').hidden = inputMode !== 'legacy';
    const trigger = valueOf(fields.get('summary_trigger_tokens'));
    const policy = state.summaryPolicy;
    $('#app-memory-status').textContent = policy?.state
      ? `Active profile (saved): ${describeMemoryState(policy)}.`
      : 'Active profile memory is unavailable. Reopen Settings to retry.';
    $('#summary-trigger-tokens-help').textContent = trigger === null
      ? 'Blank uses 90% of the declared input allowance. Enter 0 to pause summaries.'
      : trigger === 0 ? 'Summaries are disabled. Requests use original conversation history.'
      : 'Summarizes above this input estimate when memory is ready. Enter 0 to pause summaries.';
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
    for (const [name, input] of fields) input.value = state.saved?.[name] ?? '';
    resetButtons.forEach(button => {
      const value = state.defaults?.[button.dataset.resetSetting];
      const label = resetLabels.get(button);
      const formatted = value === undefined ? '' : value === null ? button.dataset.resetSetting === 'summary_trigger_tokens' ? 'Automatic' : button.dataset.resetSetting === 'context_input_budget' ? 'No input limit' : 'Provider default' : typeof value === 'number' ? value.toLocaleString() : value.charAt(0).toUpperCase() + value.slice(1);
      button.title = value === undefined ? label : `${label} (default: ${formatted})`;
      button.setAttribute('aria-label', button.title);
    });
    updateControls();
  }

  function acceptResponse(data) {
    state.policyController?.abort();
    state.policyController = null;
    for (const name of fields.keys()) {
      for (const values of [data?.settings, data?.defaults]) {
        const value = values?.[name];
        if (['context_output_reserve', 'summary_trigger_tokens', 'context_input_budget'].includes(name) && value === null) continue;
        if (name === 'context_preflight_mode' ? !Object.hasOwn(PREFLIGHT_HELP, value) : name === 'context_input_budget_mode' ? !['unlimited', 'custom', 'legacy'].includes(value) : !Number.isSafeInteger(value) || value < (name === 'summary_trigger_tokens' ? 0 : 1)) {
          throw new Error('Could not read the saved settings. Reload Settings before editing.');
        }
      }
    }
    state.saved = { ...data.settings };
    state.defaults = { ...data.defaults };
    state.localLimits = { ...data.local_attachment_limits };
    state.summaryPolicy = data.summary_policy || null;
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
      setStatus('');
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

  async function refreshSummaryPolicy() {
    if (!state.saved || state.loading || state.saving || !$('#settings-dialog').open) return;
    state.policyController?.abort();
    const controller = new AbortController();
    state.policyController = controller;
    try {
      const response = await fetch('/api/settings', { signal: controller.signal });
      const data = await readResponse(response);
      if (state.policyController !== controller) return;
      state.summaryPolicy = data.summary_policy || null;
    } catch (error) {
      if (error.name !== 'AbortError' && state.policyController === controller) state.summaryPolicy = null;
    } finally {
      if (state.policyController === controller) {
        state.policyController = null;
        updateControls();
      }
    }
  }

  function restoreDefault(name) {
    if (!state.saved || state.loading || state.saving || isProfileSaving()) return;
    const input = fields.get(name);
    const restoreFocus = document.activeElement?.dataset.resetSetting === name;
    input.value = state.defaults[name] ?? '';
    if (['context_input_budget_mode', 'context_input_budget'].includes(name)) {
      fields.get('context_input_budget_mode').value = 'unlimited';
      fields.get('context_input_budget').value = '';
      state.resets.add('context_input_budget_mode');
      state.resets.add('context_input_budget');
    }
    if (valueOf(input) !== state.saved[name]) state.resets.add(name);
    else state.resets.delete(name);
    clearValidation();
    setStatus('');
    updateControls();
    if (restoreFocus) {
      const target = name === 'context_input_budget' ? fields.get('context_input_budget_mode') : input;
      target.focus({ preventScroll: true });
    }
  }

  function validate() {
    clearValidation();
    for (const input of fields.values()) {
      if (input.disabled) continue;
      if (['context_output_reserve', 'summary_trigger_tokens'].includes(input.dataset.appSetting) && valueOf(input) === null) continue;
      if (input.tagName !== 'SELECT' && (!Number.isSafeInteger(input.valueAsNumber) || input.valueAsNumber < Number(input.min))) {
        input.setCustomValidity(`Enter a whole number of at least ${input.min} that the browser can represent exactly.`);
      }
    }
    const budget = fields.get('context_token_budget');
    const reserve = fields.get('context_output_reserve');
    if (fields.get('context_input_budget_mode').value === 'legacy' && budget.valueAsNumber - (valueOf(reserve) ?? 0) < 1024) budget.setCustomValidity('The previous policy must leave at least 1,024 input tokens after output reservation. Review the input budget to use independent limits.');
    const trigger = fields.get('summary_trigger_tokens').valueAsNumber;
    const recent = fields.get('keep_recent_tokens');
    if (trigger > 0 && recent.valueAsNumber >= trigger) recent.setCustomValidity('Keep recent tokens must be smaller than the enabled summary trigger.');
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
    if (!state.saved || state.loading || state.saving || isProfileSaving() || !hasUnsavedChanges() || !validate()) return;
    const changes = {};
    for (const [name, input] of fields) {
      const value = valueOf(input);
      if (value !== state.saved[name]) changes[name] = state.resets.has(name) ? null : value;
    }
    state.saving = true;
    setStatus('Saving changes...');
    updateControls();
    try {
      const response = await fetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes)
      });
      acceptResponse(await readResponse(response));
      $('#settings-dialog').dispatchEvent(new Event('app-settings-saved'));
      setStatus('');
      toast('Changes saved.', 'success');
    } catch (error) {
      setStatus(`${error.message || 'Could not confirm the save.'}\nYour edits are kept. Reopen Settings to check saved values if the connection was interrupted.`, 'error');
    } finally {
      state.saving = false;
      updateControls();
    }
  }

  function beforeDialogClose() {
    if (state.saving || isProfileSaving()) {
      toast('Wait for the Settings save to finish before closing.', 'info');
      return false;
    }
    if (!hasUnsavedChanges() && !hasUnsavedProfileChanges()) return true;
    return showActionDialog({ title: 'Discard Settings changes?', description: 'Your unsaved settings and profile edits will be lost.',
      confirmLabel: 'Discard changes', cancelLabel: 'Keep editing', danger: true });
  }

  function init() {
    form.onsubmit = save;
    for (const [name, input] of fields) {
      input.addEventListener('input', () => {
        state.resets.delete(name);
        clearValidation();
        setStatus('');
        updateControls();
      });
    }
    resetButtons.forEach(button => { button.onclick = () => restoreDefault(button.dataset.resetSetting); });
    $('#restore-app-defaults').onclick = () => {
      const restoreFocus = document.activeElement === $('#restore-app-defaults');
      for (const name of fields.keys()) restoreDefault(name);
      if (restoreFocus) $('#save-app-settings').focus({ preventScroll: true });
    };
    $('#discard-app-settings').onclick = () => {
      if (!state.saved || state.loading || state.saving || isProfileSaving()) return;
      fillSaved();
      setStatus('');
      $('#settings-context-tab').focus({ preventScroll: true });
    };
    $('#settings-dialog').addEventListener('profile-save-state-change', updateControls);
    $('#settings-dialog').addEventListener('settings-panel-change', event => {
      if (event.detail.name === 'context') {
        if (state.saved) refreshSummaryPolicy();
        else load();
      }
    });
    $('#settings-dialog').addEventListener('close', () => {
      if ($('#settings-dialog').open) return;
      state.controller?.abort();
      state.policyController?.abort();
      state.controller = null;
      state.policyController = null;
      state.loading = false;
      state.saved = null;
      state.defaults = null;
      state.localLimits = null;
      state.summaryPolicy = null;
      fillSaved();
      setStatus('');
    });
    fillSaved();
  }

  return { init, beforeDialogClose };
}
