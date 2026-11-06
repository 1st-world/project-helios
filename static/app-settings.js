/* Edit app-wide context and attachment policies while keeping saved enforcement limits separate from drafts. */

import { $, setText, toast } from './ui.js';
import { PREFLIGHT_HELP, describeRequestError } from './chat-feedback.js';
import { showActionDialog } from './dialogs.js';
import { createFieldValidation, setFieldFeedback } from './field-validation.js';

export function createAppSettings({ hasUnsavedProfileChanges, isProfileSaving }) {
  const form = $('#app-settings-form');
  const validation = createFieldValidation(form);
  const fields = new Map([...form.querySelectorAll('[data-app-setting]')].map(input => [input.dataset.appSetting, input]));
  const resetButtons = [...form.querySelectorAll('[data-reset-setting]')];
  const resetLabels = new Map(resetButtons.map(button => [button, button.getAttribute('aria-label')]));
  const effectiveLabels = [...form.querySelectorAll('[data-effective-setting]')];
  const memorySwitches = ['memory_use_saved_summaries', 'memory_generation_enabled', 'memory_background_enabled'];
  const state = { saved: null, defaults: null, localLimits: null, resets: new Set(), loading: false, saving: false, controller: null, feedback: null, legacyTriggerRetained: false };

  function setStatus(message, type = 'info') {
    state.feedback = message ? { message, type } : null;
    renderStatus();
  }

  function renderStatus() {
    const status = $('#app-settings-status');
    setText(status, state.feedback?.message || '');
    status.dataset.state = state.feedback?.type || 'idle';
  }

  function valueOf(input) {
    if (input.type === 'checkbox') return input.checked;
    if (input.dataset.appSetting === 'context_input_budget' && fields.get('context_input_budget_mode').value !== 'custom') return null;
    if (['context_output_reserve', 'summary_trigger_tokens', 'context_input_budget'].includes(input.dataset.appSetting) && input.value === '' && !input.validity.badInput) return null;
    return input.tagName === 'SELECT' ? input.value : input.valueAsNumber;
  }

  function setValue(input, value) {
    if (input.type === 'checkbox') input.checked = value === true;
    else input.value = value ?? '';
  }

  function resumeLegacyTrigger() {
    const trigger = fields.get('summary_trigger_tokens');
    if (trigger.valueAsNumber !== 0) return;
    state.legacyTriggerRetained = false;
    trigger.value = '';
    state.resets.delete('summary_trigger_tokens');
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
    const budgetMode = fields.get('context_input_budget_mode');
    const inputMode = budgetMode.value;
    fields.get('context_input_budget').disabled ||= inputMode !== 'custom';
    fields.get('context_input_budget').closest('.settings-preference-row').hidden = inputMode !== 'custom';
    budgetMode.querySelector('[value="legacy"]').hidden = state.saved?.context_input_budget_mode !== 'legacy';
    const useMemory = fields.get('memory_use_saved_summaries').checked;
    const generateMemory = fields.get('memory_generation_enabled').checked;
    fields.get('memory_generation_enabled').disabled ||= !useMemory;
    fields.get('memory_background_enabled').disabled ||= !useMemory || !generateMemory;
    setText($('#memory-generation-enabled-help'), useMemory
      ? 'Make additional paid calls to summarize older messages. Off keeps saved summaries available.'
      : 'Available when Use saved summaries is On.');
    setText($('#memory-background-enabled-help'), useMemory && generateMemory
      ? 'Check after completed replies. Off still allows summaries before a reply.'
      : 'Available when saved-summary use and generation are On.');
    const trigger = fields.get('summary_trigger_tokens');
    const retainedZero = state.legacyTriggerRetained && trigger.valueAsNumber === 0;
    trigger.min = retainedZero ? '0' : '1';
    setFieldFeedback(trigger, $('#summary-trigger-review'), retainedZero);
    resetButtons.forEach(button => {
      const name = button.dataset.resetSetting;
      const input = fields.get(name);
      const differs = Boolean(state.defaults && valueOf(input) !== state.defaults[name]);
      defaultsDiffer ||= differs;
      button.hidden = !differs;
      button.disabled = busy || !state.saved || !differs || input.type === 'checkbox' && input.disabled;
    });
    $('#restore-app-defaults').disabled = busy || !state.saved || !defaultsDiffer;
    $('#discard-app-settings').disabled = busy || !hasUnsavedChanges();
    $('#save-app-settings').disabled = busy || !hasUnsavedChanges();
    $('#save-app-settings-label').textContent = state.saving ? 'Saving...' : 'Save changes';
    $('#save-app-settings').setAttribute('aria-busy', String(state.saving));
    $('#save-app-settings').setAttribute('aria-label', state.saving ? 'Saving changes' : 'Save changes');
    renderStatus();
    $('#context-preflight-help').textContent = PREFLIGHT_HELP[fields.get('context_preflight_mode').value] || 'How to handle estimates above the selected input limits.';
    setFieldFeedback(budgetMode, $('#app-input-policy-review'), inputMode === 'legacy');
    if (inputMode === 'legacy') {
      const output = valueOf(fields.get('context_output_reserve'));
      const budget = fields.get('context_token_budget').valueAsNumber - (output ?? 0);
      $('#app-input-budget').textContent = Number.isSafeInteger(budget) && budget >= 0
        ? `Draft budget before profile limits is ${budget.toLocaleString()} tokens.`
        : 'Review the summary work budget and output limit to calculate the legacy input budget.';
    }
    showAttachmentLimits();
  }

  function describeDefault(button) {
    const name = button.dataset.resetSetting;
    const value = state.defaults?.[name];
    if (value === undefined) return '';
    const input = fields.get(name);
    if (input.type === 'checkbox') return value ? 'On' : 'Off';
    if (input.tagName === 'SELECT') return [...input.options].find(option => option.value === value)?.textContent.trim() || value;
    if (value === null) return input.placeholder || fields.get('context_input_budget_mode').querySelector('[value="unlimited"]').textContent.trim();
    return `${value.toLocaleString()}${button.dataset.resetUnit ? ` ${button.dataset.resetUnit}` : ''}`;
  }

  function fillSaved() {
    state.resets.clear();
    validation.reset();
    state.legacyTriggerRetained = state.saved?.summary_trigger_tokens === 0;
    for (const [name, input] of fields) setValue(input, state.saved?.[name]);
    resetButtons.forEach(button => {
      const label = resetLabels.get(button);
      const formatted = describeDefault(button);
      button.title = formatted ? `${label}: ${formatted}` : label;
      button.setAttribute('aria-label', button.title);
    });
    updateControls();
  }

  function acceptResponse(data) {
    for (const [name, input] of fields) {
      for (const values of [data?.settings, data?.defaults]) {
        const value = values?.[name];
        if (['context_output_reserve', 'summary_trigger_tokens', 'context_input_budget'].includes(name) && value === null) continue;
        if (input.type === 'checkbox' ? typeof value !== 'boolean' : name === 'context_preflight_mode' ? !Object.hasOwn(PREFLIGHT_HELP, value) : name === 'context_input_budget_mode' ? !['unlimited', 'custom', 'legacy'].includes(value) : !Number.isSafeInteger(value) || value < (name === 'summary_trigger_tokens' ? 0 : 1)) {
          throw new Error('Could not read the saved settings. Reload Settings before editing.');
        }
      }
    }
    state.saved = { ...data.settings };
    state.defaults = { ...data.defaults };
    state.localLimits = { ...data.local_attachment_limits };
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

  function restoreDefaultValue(name) {
    const input = fields.get(name);
    setValue(input, state.defaults[name]);
    if (name === 'summary_trigger_tokens') state.legacyTriggerRetained = false;
    if (name === 'memory_generation_enabled' && input.checked) resumeLegacyTrigger();
    if (['context_input_budget_mode', 'context_input_budget'].includes(name)) {
      fields.get('context_input_budget_mode').value = 'unlimited';
      fields.get('context_input_budget').value = '';
      state.resets.add('context_input_budget_mode');
      state.resets.add('context_input_budget');
    }
    if (valueOf(input) !== state.saved[name]) state.resets.add(name);
    else state.resets.delete(name);
  }

  function restoreDefaults(names) {
    if (!state.saved || state.loading || state.saving || isProfileSaving()) return;
    const restoreAllFocus = document.activeElement === $('#restore-app-defaults');
    const focusedSetting = document.activeElement?.dataset.resetSetting;
    const reviewing = validation.isReviewing();
    for (const name of names) restoreDefaultValue(name);
    state.feedback = null;
    updateControls();
    if (reviewing) validate(false);
    if (restoreAllFocus) $('#save-app-settings').focus({ preventScroll: true });
    else if (focusedSetting) {
      const target = fields.get(focusedSetting === 'context_input_budget' ? 'context_input_budget_mode' : focusedSetting);
      target.focus({ preventScroll: true });
    }
  }

  function validate(focus = true) {
    for (const input of fields.values()) {
      input.setCustomValidity('');
      if (input.disabled) continue;
      if (input.type === 'checkbox') continue;
      if (['context_output_reserve', 'summary_trigger_tokens'].includes(input.dataset.appSetting) && valueOf(input) === null) continue;
      if (input.tagName !== 'SELECT' && (!Number.isSafeInteger(input.valueAsNumber) || input.valueAsNumber < Number(input.min))) {
        input.setCustomValidity(input.dataset.appSetting === 'summary_trigger_tokens' && input.valueAsNumber === 0
          ? 'Use Generate summaries to pause new summaries. Enter a positive trigger or leave it blank for Automatic.'
          : input.valueAsNumber > Number.MAX_SAFE_INTEGER
          ? `Enter a whole number no greater than ${Number.MAX_SAFE_INTEGER.toLocaleString()}.`
          : `Enter a whole number of at least ${Number(input.min).toLocaleString()}.`);
      }
    }
    const budget = fields.get('context_token_budget');
    const reserve = fields.get('context_output_reserve');
    if (fields.get('context_input_budget_mode').value === 'legacy' && budget.valueAsNumber - (valueOf(reserve) ?? 0) < 1024) budget.setCustomValidity('The previous policy must leave at least 1,024 input tokens after output reservation. Review the input budget to use independent limits.');
    const trigger = fields.get('summary_trigger_tokens').valueAsNumber;
    const recent = fields.get('keep_recent_tokens');
    if (trigger > 0 && recent.valueAsNumber >= trigger) recent.setCustomValidity('Enter fewer tokens than the summary trigger.');
    return validation.showErrors({ focus });
  }

  async function save(event) {
    event.preventDefault();
    if (!state.saved || state.loading || state.saving || isProfileSaving() || !hasUnsavedChanges() || !validate()) return;
    const changes = {};
    for (const [name, input] of fields) {
      const value = valueOf(input);
      if (value !== state.saved[name]) changes[name] = state.resets.has(name) ? null : value;
    }
    // Explicit choices prevent numeric edits from invoking the legacy zero-trigger shortcut.
    for (const name of memorySwitches) changes[name] = state.resets.has(name) ? null : valueOf(fields.get(name));
    if (state.legacyTriggerRetained && fields.get('summary_trigger_tokens').valueAsNumber === 0) changes.summary_trigger_tokens = 0;
    state.saving = true;
    setStatus('');
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
        if (name === 'summary_trigger_tokens') state.legacyTriggerRetained = false;
        if (name === 'memory_generation_enabled' && input.checked) resumeLegacyTrigger();
        setStatus('');
        updateControls();
        if (validation.isReviewing()) validate(false);
      });
    }
    resetButtons.forEach(button => { button.onclick = () => restoreDefaults([button.dataset.resetSetting]); });
    $('#restore-app-defaults').onclick = () => restoreDefaults(fields.keys());
    $('#discard-app-settings').onclick = () => {
      if (!state.saved || state.loading || state.saving || isProfileSaving()) return;
      fillSaved();
      setStatus('');
      $('#settings-context-tab').focus({ preventScroll: true });
    };
    $('#settings-dialog').addEventListener('profile-save-state-change', updateControls);
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
      fillSaved();
      setStatus('');
    });
    fillSaved();
  }

  return { init, beforeDialogClose };
}
