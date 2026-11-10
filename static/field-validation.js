/* Present field feedback while preserving control descriptions and disclosure structure. */

export function setFieldFeedback(input, feedback, visible) {
  feedback.hidden = !visible;
  const descriptions = new Set((input.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
  if (visible) descriptions.add(feedback.id);
  else descriptions.delete(feedback.id);
  if (descriptions.size) input.setAttribute('aria-describedby', [...descriptions].join(' '));
  else input.removeAttribute('aria-describedby');
}

export function createFieldValidation(container, updateValidity = () => {}) {
  const fields = new Set(container.querySelectorAll('[data-field-control] :is(input, select, textarea)'));
  const edited = new Set();
  const messages = new Map();

  function render(input, message) {
    let error = messages.get(input);
    if (message) input.setAttribute('aria-invalid', 'true');
    else input.removeAttribute('aria-invalid');
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
    edited.clear();
    for (const input of fields) {
      input.setCustomValidity('');
      render(input, '');
    }
  }

  function focusError(input) {
    let disclosure = input.closest('details');
    while (disclosure && container.contains(disclosure)) {
      disclosure.open = true;
      disclosure = disclosure.parentElement.closest('details');
    }
    input.focus();
    messages.get(input).scrollIntoView({ block: 'nearest' });
  }

  function refresh({ field, revealAll = false } = {}) {
    const related = updateValidity();
    const dependent = related?.get(field);
    let firstInvalid = null;
    for (const input of fields) {
      const invalid = input.willValidate && !input.validity.valid;
      const visible = revealAll || input === field || input === dependent || messages.get(input)?.hidden === false;
      if (invalid) firstInvalid ||= input;
      render(input, invalid && visible ? input.validationMessage : '');
    }
    if (firstInvalid && revealAll) focusError(firstInvalid);
    return !firstInvalid;
  }

  container.addEventListener('input', ({ target }) => {
    if (!fields.has(target)) return;
    edited.add(target);
    refresh();
  });
  container.addEventListener('focusout', ({ target }) => {
    if (edited.delete(target)) refresh({ field: target });
  });

  return {
    reset,
    refresh,
    showErrors: () => refresh({ revealAll: true }),
    showFieldError(input, message, { focus = false } = {}) {
      render(input, message);
      if (message && focus) focusError(input);
    },
  };
}
