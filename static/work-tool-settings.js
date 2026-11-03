/* Own applied work-tool limits, modal drafts, guarded dismissal, and read-scope guidance. */

import { $, setText, toast } from './ui.js';
import { showActionDialog } from './dialogs.js';
import { initializeHelpPopovers } from './help-popovers.js';
import { createFieldValidation } from './field-validation.js';

const fields = [
  ['work-tools-call-limit', 'max_model_calls'],
  ['work-tools-time-target', 'elapsed_time_target_seconds'],
  ['work-tools-cost-target', 'cost_goal_usd']
];

export function createWorkToolSettings({ getWorkspace, isGenerating }) {
  const dialog = $('#work-tools-settings-dialog');
  const form = $('#work-tools-settings-form');
  const validation = createFieldValidation(form);
  initializeHelpPopovers(dialog);
  let applied = {};
  let baseline = [];

  function updateState() {
    const busy = isGenerating();
    $('#work-tools-run-fields').disabled = busy;
    $('#apply-work-tools-settings').disabled = busy;
    const status = $('#work-tools-settings-status');
    const message = busy ? 'Settings are read-only while a response is being generated.' : '';
    setText(status, message);
    $('#work-tools-workspace').textContent = getWorkspace().root || 'No workspace connected. Choose one from the sidebar to use Explore project.';
  }

  function restoreDraft() {
    validation.reset();
    baseline = fields.map(([id, key]) => {
      const input = $(`#${id}`);
      input.value = applied[key] == null ? '' : String(applied[key]);
      return input.value;
    });
  }

  function readDraft(focus = true) {
    const limits = {};
    for (const [id, key] of fields) {
      const input = $(`#${id}`);
      input.setCustomValidity('');
      if (input.validity.badInput || (input.value !== '' && (!Number.isFinite(input.valueAsNumber) || input.valueAsNumber <= 0
          || (key === 'max_model_calls' && !Number.isSafeInteger(input.valueAsNumber))))) {
        input.setCustomValidity(key === 'max_model_calls'
          ? input.valueAsNumber > Number.MAX_SAFE_INTEGER ? `Enter a whole number no greater than ${Number.MAX_SAFE_INTEGER.toLocaleString()}.` : 'Enter a positive whole number.'
          : 'Enter a number greater than zero.');
      }
      if (input.value !== '') limits[key] = input.valueAsNumber;
    }
    return validation.showErrors({ focus }) ? limits : null;
  }

  function beforeDialogClose() {
    const changed = fields.some(([id], index) => {
      const input = $(`#${id}`);
      return input.validity.badInput || input.value !== baseline[index];
    });
    if (!changed) return true;
    return showActionDialog({ title: 'Discard Work tools changes?',
      description: 'Your unapplied limits and targets will be lost. Applied settings and tool selections will stay unchanged.',
      confirmLabel: 'Discard changes', cancelLabel: 'Keep editing', danger: true });
  }

  function open({ focusLimits = false } = {}) {
    $('#work-tools').open = false;
    restoreDraft();
    updateState();
    dialog.showModal();
    if (focusLimits && !isGenerating()) $('#work-tools-call-limit').focus();
  }

  $('#work-tools-settings').onclick = open;
  form.onsubmit = event => {
    event.preventDefault();
    if (isGenerating()) return;
    const draft = readDraft();
    if (draft == null) return;
    applied = draft;
    dialog.close();
    toast('Work tools settings applied.', 'success');
  };
  fields.forEach(([id]) => $(`#${id}`).addEventListener('input', () => {
    if (validation.isReviewing()) readDraft(false);
  }));
  document.addEventListener('workspace-change', updateState);
  dialog.addEventListener('close', () => {
    if (dialog.open) return;
    restoreDraft();
    $('#work-tools-trigger').focus({ preventScroll: true });
  });
  restoreDraft();
  updateState();
  return { limits: () => ({ ...applied }), beforeDialogClose, updateState, open };
}
