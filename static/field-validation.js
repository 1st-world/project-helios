/* Present field feedback while preserving control descriptions and disclosure structure. */

export function setFieldFeedback(input, feedback, visible) {
  feedback.hidden = !visible;
  const descriptions = new Set((input.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
  if (visible) descriptions.add(feedback.id);
  else descriptions.delete(feedback.id);
  if (descriptions.size) input.setAttribute('aria-describedby', [...descriptions].join(' '));
  else input.removeAttribute('aria-describedby');
}

export function createFieldValidation(form) {
  const fields = [...form.querySelectorAll('input, select')];
  const messages = new Map();
  let reviewing = false;

  function render(input, message) {
    let error = messages.get(input);
    if (message && !error) {
      error = document.createElement('p');
      error.id = `${input.id}-error`;
      error.className = 'field-error';
      const control = input.closest('[data-field-control]');
      (control.closest('[data-field-row]') || control).append(error);
      messages.set(input, error);
    }
    if (!error) return;
    if (error.textContent !== message) error.textContent = message;
    setFieldFeedback(input, error, Boolean(message));
  }

  function reset() {
    reviewing = false;
    for (const input of fields) {
      input.setCustomValidity('');
      input.removeAttribute('aria-invalid');
      render(input, '');
    }
  }

  function showErrors({ focus = true } = {}) {
    reviewing = true;
    let firstInvalid = null;
    for (const input of fields) {
      const invalid = input.willValidate && !input.validity.valid;
      if (invalid) {
        firstInvalid ||= input;
        input.setAttribute('aria-invalid', 'true');
      } else input.removeAttribute('aria-invalid');
      render(input, invalid ? input.validationMessage : '');
    }
    if (firstInvalid && focus) {
      let disclosure = firstInvalid.closest('details');
      while (disclosure && form.contains(disclosure)) {
        disclosure.open = true;
        disclosure = disclosure.parentElement.closest('details');
      }
      firstInvalid.focus();
      messages.get(firstInvalid).scrollIntoView({ block: 'nearest' });
    }
    return !firstInvalid;
  }

  return { reset, showErrors, isReviewing: () => reviewing };
}
