/* Own applied work-tool limits, modal drafts, guarded dismissal, and read-scope guidance. */

import { $, toast } from './ui.js';
import { showActionDialog } from './dialogs.js';
import { createDisclosure } from './disclosure.js';

const fields = [
  ['work-tools-call-limit', 'max_model_calls'],
  ['work-tools-time-target', 'elapsed_time_target_seconds'],
  ['work-tools-cost-target', 'cost_goal_usd']
];

export function createWorkToolSettings({ getWorkspace, isGenerating }) {
  const dialog = $('#work-tools-settings-dialog');
  const form = $('#work-tools-settings-form');
  const scope = $('#work-tools-scope-details');
  const disclosure = createDisclosure(scope, scope.querySelector('.work-tools-scope-body'));
  let applied = {};
  let baseline = [];

  function updateState() {
    const busy = isGenerating();
    $('#work-tools-run-fields').disabled = busy;
    $('#apply-work-tools-settings').disabled = busy;
    $('#work-tools-settings-busy').hidden = !busy;
    $('#work-tools-workspace').textContent = getWorkspace().root || 'No workspace connected. Choose one from the sidebar to use Explore project.';
  }

  function restoreDraft() {
    baseline = fields.map(([id, key]) => {
      const input = $(`#${id}`);
      input.value = applied[key] == null ? '' : String(applied[key]);
      input.setCustomValidity('');
      return input.value;
    });
  }

  function readDraft() {
    const limits = {};
    for (const [id, key] of fields) {
      const input = $(`#${id}`);
      input.setCustomValidity('');
      if (input.validity.badInput || (input.value !== '' && (!Number.isFinite(input.valueAsNumber) || input.valueAsNumber <= 0
          || (key === 'max_model_calls' && !Number.isSafeInteger(input.valueAsNumber))))) {
        input.setCustomValidity(key === 'max_model_calls' ? 'Enter a positive whole number.' : 'Enter a number greater than zero.');
      }
      if (!input.reportValidity()) return null;
      if (input.value !== '') limits[key] = input.valueAsNumber;
    }
    return limits;
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

  $('#work-tools-settings').onclick = () => {
    $('#work-tools').open = false;
    restoreDraft();
    updateState();
    dialog.showModal();
  };
  form.onsubmit = event => {
    event.preventDefault();
    if (isGenerating()) return;
    const draft = readDraft();
    if (draft == null) return;
    applied = draft;
    dialog.close();
    toast('Work tools settings applied.', 'success');
  };
  fields.forEach(([id]) => $(`#${id}`).addEventListener('input', event => event.target.setCustomValidity('')));
  document.addEventListener('workspace-change', updateState);
  dialog.addEventListener('close', () => {
    if (dialog.open) return;
    disclosure.finish();
    restoreDraft();
    $('#work-tools-trigger').focus({ preventScroll: true });
  });
  restoreDraft();
  updateState();
  return { limits: () => ({ ...applied }), beforeDialogClose, updateState };
}
