/* Describe approximate context warnings and structured request failures for the chat interface. */

function cleanText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function tokenLimit(value, label) {
  return Number.isSafeInteger(value) && value >= 0 ? `${label}: ${value.toLocaleString()} tokens` : '';
}

export function describeRequestError(payload, fallback = 'Request failed.', status = '') {
  const detail = payload?.detail ?? payload;
  let message = cleanText(detail);
  if (Array.isArray(detail)) {
    message = detail.map((item) => {
      const text = cleanText(item?.msg);
      const location = Array.isArray(item?.loc)
        ? item.loc.filter((part) => typeof part === 'string' || typeof part === 'number').join('.') : '';
      return text && location ? `${location}: ${text}` : text;
    }).filter(Boolean).join('; ');
  } else if (detail && typeof detail === 'object') {
    message = cleanText(detail.message);
  }

  const code = cleanText(detail?.code);
  const blocked = code === 'context_preflight_blocked';
  const title = blocked ? 'Context preflight blocked'
    : code === 'attachment_model_unsupported' ? 'Attachment unsupported by model'
    : code.startsWith('attachment_') ? 'Attachment request failed' : 'Request failed';
  const details = [];
  const path = cleanText(detail?.path);
  if (path) details.push(`Affected file: ${path}`);
  if (Array.isArray(detail?.paths)) {
    const paths = [...new Set(detail.paths.map(cleanText).filter(Boolean))];
    if (paths.length) details.push(`Selected attachments: ${paths.join(', ')}`);
  }
  if (blocked) {
    details.push('The local preflight uses approximate estimates. This is not a confirmed rejection by the model.');
    details.push(tokenLimit(detail.input_budget, 'Selected input budget'));
    details.push(tokenLimit(detail.declared_model_input_limit, 'Declared model input limit'));
  }
  if (status) details.push(status);
  return { title, message: message || fallback, details: details.filter(Boolean) };
}

export function describeContextWarning(event, policy = null) {
  const details = [
    tokenLimit(policy?.estimated_input_tokens, 'Approximate input'),
    tokenLimit(policy?.input_budget, 'Selected input budget'),
    tokenLimit(policy?.declared_model_input_limit, 'Declared model input limit'),
    'These are local estimates, not billed usage. Actual usage is shown with the response when available.',
  ].filter(Boolean);
  return {
    title: 'Context estimate warning',
    message: cleanText(event?.message) || 'The approximate input exceeds the selected budget or declared model limit. Warning mode allows this request to continue.',
    details,
  };
}
