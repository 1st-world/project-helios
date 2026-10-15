/* Describe approximate context warnings and structured request failures for the chat interface. */

function cleanText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function tokenLimit(value, label) {
  return Number.isSafeInteger(value) && value >= 0 ? `${label}: ${value.toLocaleString()} tokens` : '';
}

const PROVIDER_TITLES = {
  connection_not_configured: 'Connection needs setup',
  provider_authentication_failed: 'Provider authentication failed',
  provider_permission_denied: 'Provider access denied',
  provider_resource_not_found: 'Provider resource or API route not found',
  provider_deployment_not_found: 'Deployment not found',
  provider_throttled: 'Provider request throttled',
  provider_quota_exceeded: 'Provider quota limit reported',
  provider_timeout: 'Provider request timed out',
  provider_connection_failed: 'Provider connection failed',
  provider_unavailable: 'Provider unavailable',
  provider_invalid_request: 'Provider rejected the request',
  provider_output_limit_reached: 'Response reached the output limit',
  provider_content_filtered: 'Request or response filtered',
  provider_response_incomplete: 'Response incomplete',
  provider_response_empty: 'Response contains no text',
  provider_stream_interrupted: 'Response stream interrupted',
  provider_error: 'Provider request failed',
};

function recoveryActions(code) {
  if (['connection_not_configured', 'provider_authentication_failed', 'provider_permission_denied', 'provider_resource_not_found', 'provider_deployment_not_found'].includes(code)) return ['connections'];
  if (['provider_throttled', 'provider_quota_exceeded', 'provider_timeout', 'provider_connection_failed', 'provider_unavailable', 'provider_stream_interrupted', 'provider_error', 'request_failed'].includes(code)) return ['usage', 'connections'];
  if (code === 'provider_context_length_exceeded') return ['question', 'attachments', 'connections'];
  if (code === 'provider_invalid_request') return ['question', 'attachments', 'connections'];
  if (code === 'provider_output_limit_reached') return ['question', 'output', 'connections'];
  if (code === 'provider_content_filtered') return ['question', 'usage', 'connections'];
  if (['provider_response_incomplete', 'provider_response_empty'].includes(code)) return ['question', 'usage'];
  if (code === 'context_preflight_blocked') return ['question', 'limits'];
  if (code.startsWith('attachment_')) return ['attachments', 'connections'];
  if (code === 'context_recovery_unavailable') return ['question', 'usage'];
  return [];
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
  let title = (Object.hasOwn(PROVIDER_TITLES, code) ? PROVIDER_TITLES[code] : '') || (blocked ? 'Context preflight blocked'
    : code === 'provider_context_length_exceeded' ? 'Provider context limit exceeded'
    : code === 'context_recovery_unavailable' ? 'One-time recovery unavailable'
    : code === 'conversation_request_in_progress' ? 'Conversation request in progress'
    : code === 'attachment_model_unsupported' ? 'Attachment unsupported by model'
    : code.startsWith('attachment_') ? 'Attachment request failed' : 'Request failed');
  if (detail?.phase === 'summary') title = `Memory summary: ${title}`;
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
  if (code === 'provider_context_length_exceeded') {
    details.push('The API provider confirmed that this request exceeds its context limit.');
    if (detail.recovery?.available === false && cleanText(detail.recovery.reason)) details.push(cleanText(detail.recovery.reason));
  }
  if (code.startsWith('provider_') || detail?.category === 'attachment') {
    const httpStatus = Number.isInteger(detail.http_status) && detail.http_status >= 100 && detail.http_status <= 599 ? detail.http_status : 'Not provided';
    const requestId = /^[A-Za-z0-9._-]{1,256}$/.test(cleanText(detail.request_id)) ? cleanText(detail.request_id) : 'Not provided';
    const retry = typeof detail.retry_after_seconds === 'number' && Number.isFinite(detail.retry_after_seconds) && detail.retry_after_seconds >= 0
      ? `${detail.retry_after_seconds.toLocaleString(undefined, { maximumFractionDigits: 3 })} seconds` : 'Not provided';
    details.push(`Provider HTTP status: ${httpStatus}`, `Request ID: ${requestId}`, `Retry-after: ${retry}`);
    if (['failed', 'incomplete', 'cancelled', 'completed'].includes(detail.response_status)) details.push(`Provider response: ${detail.response_status}`);
    if (detail.phase === 'summary') details.push('The summary did not complete. No reply retry was started. Any recorded summary usage is retained.');
    details.push('No automatic retry was made.');
  }
  if (status) details.push(status);
  return { title, message: message || fallback, details: details.filter(Boolean),
    actions: recoveryActions(code),
    ...(code === 'provider_context_length_exceeded' && detail.recovery?.available === true ? { recovery: detail.recovery } : {}) };
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
    message: cleanText(event?.message) || 'The approximate input exceeds the selected input budget. Warning mode allows this request to continue.',
    details,
  };
}
