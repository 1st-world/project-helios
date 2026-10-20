/* Provide accessible app dialogs with asynchronous actions, inline errors, and focus restoration. */

let activeDialog = null;

export function bindDialogBackdrop(dialog, onDismiss) {
  let backdropPointer = null;

  function isOnBackdrop(event) {
    const rect = dialog.getBoundingClientRect();
    return event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom);
  }

  dialog.addEventListener('pointerdown', event => {
    backdropPointer = event.isPrimary && event.button === 0 && isOnBackdrop(event) ? event.pointerId : null;
  });
  dialog.addEventListener('pointerup', event => {
    const shouldDismiss = backdropPointer === event.pointerId && isOnBackdrop(event);
    backdropPointer = null;
    if (shouldDismiss) onDismiss();
  });
  dialog.addEventListener('pointercancel', () => { backdropPointer = null; });
}

export function showActionDialog({ title, description, confirmLabel, cancelLabel = 'Cancel', danger = false, field = null, onConfirm = () => {} }) {
  // Ignore duplicate requests while a decision or its action is still pending.
  if (activeDialog) return Promise.resolve(false);
  const opener = document.activeElement;
  const dialog = document.createElement('dialog');
  activeDialog = dialog;
  dialog.className = 'action-dialog';
  dialog.setAttribute('aria-labelledby', 'action-dialog-title');
  dialog.setAttribute('aria-describedby', 'action-dialog-description');
  dialog.innerHTML = `
    <form novalidate>
      <div class="dialog-heading"><h2 id="action-dialog-title"></h2></div>
      <p id="action-dialog-description"></p>
      <div class="action-dialog-field" hidden><input id="action-dialog-input" type="text" autocomplete="off" aria-describedby="action-dialog-error"></div>
      <p id="action-dialog-error" role="alert" hidden></p>
      <div class="dialog-actions"><button class="quiet" type="button" data-cancel></button><button type="submit" data-confirm></button></div>
    </form>
  `;
  const form = dialog.querySelector('form');
  const input = dialog.querySelector('input');
  const error = dialog.querySelector('[role="alert"]');
  const cancel = dialog.querySelector('[data-cancel]');
  const confirm = dialog.querySelector('[data-confirm]');
  dialog.querySelector('h2').textContent = title;
  dialog.querySelector('#action-dialog-description').textContent = description;
  cancel.textContent = cancelLabel;
  confirm.textContent = confirmLabel;
  confirm.className = danger ? 'danger-btn' : 'primary-btn';
  if (field) {
    dialog.querySelector('.action-dialog-field').hidden = false;
    input.setAttribute('aria-label', field.label);
    input.value = field.value;
    if (field.maxLength) input.maxLength = field.maxLength;
  }
  let busy = false;
  let accepted = false;

  function setBusy(value) {
    busy = value;
    form.setAttribute('aria-busy', String(value));
    input.disabled = cancel.disabled = confirm.disabled = value;
    confirm.textContent = value ? 'Working...' : confirmLabel;
  }

  function dismiss() {
    if (!busy) dialog.close();
  }

  cancel.onclick = dismiss;
  bindDialogBackdrop(dialog, dismiss);
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    dismiss();
  });
  input.oninput = () => {
    error.hidden = true;
    input.removeAttribute('aria-invalid');
  };
  dialog.addEventListener('keydown', event => {
    if (event.key === 'Enter' && event.isComposing) event.preventDefault();
    if (event.key !== 'Tab') return;
    const controls = [...form.querySelectorAll('input, button')].filter(control => !control.disabled && control.getClientRects().length);
    const target = event.shiftKey ? controls.at(-1) : controls[0];
    const boundary = event.shiftKey ? controls[0] : controls.at(-1);
    if (!controls.length || document.activeElement === boundary || !controls.includes(document.activeElement)) {
      event.preventDefault();
      target?.focus();
    }
  });
  form.onsubmit = async event => {
    event.preventDefault();
    if (busy) return;
    const value = input.value.trim();
    if (field && (!value || (field.maxLength && [...value].length > field.maxLength))) {
      error.textContent = !value ? `${field.label} is required.` : `Use ${field.maxLength} characters or fewer.`;
      error.hidden = false;
      input.setAttribute('aria-invalid', 'true');
      input.focus();
      return;
    }
    error.hidden = true;
    setBusy(true);
    try {
      await onConfirm(value);
      accepted = true;
      dialog.close();
    } catch (failure) {
      error.textContent = failure.message || 'Could not complete this action. Please try again.';
      error.hidden = false;
      setBusy(false);
      (field ? input : confirm).focus();
    }
  };
  const result = new Promise(resolve => {
    dialog.addEventListener('close', () => {
      dialog.remove();
      activeDialog = null;
      if (opener?.isConnected && !opener.disabled) opener.focus({ preventScroll: true });
      resolve(accepted);
    }, { once: true });
  });
  document.body.append(dialog);
  dialog.showModal();
  (field ? input : cancel).focus();
  if (field) input.select();
  return result;
}
