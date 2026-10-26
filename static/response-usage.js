/* Describe response and model-call usage with explicit aggregate scope and incomplete-record qualifiers. */

export function workRunMetadata(run = {}) {
  const usage = run.usage;
  const totals = usage?.totals;
  const meta = { profile_name: run.profile?.name, deployment: run.profile?.deployment, created_at: run.started_at };
  if (!totals) return meta;
  const recorded = totals.calls ?? usage.expected_calls;
  const empty = usage.expected_calls === 0;
  Object.assign(meta, { usage_scope: 'work_run', response_calls: usage.expected_calls, recorded_response_calls: recorded,
    usage_status: recorded || empty ? totals.usage_status : 'unavailable', cost_status: recorded || empty ? totals.cost_status : 'unavailable',
    coverage_incomplete: totals.coverage_incomplete, partial_token_fields: [] });
  for (const field of ['input_tokens', 'output_tokens', 'total_tokens', 'uncached_input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens']) {
    const unknown = totals[`unknown_${field}_calls`] || 0;
    meta[field] = empty || recorded > unknown ? totals[field] ?? null : null;
    if (meta[field] != null && (unknown || usage.missing_call_ids?.length)) meta.partial_token_fields.push(field);
  }
  meta.estimated_cost = (empty || recorded > (totals.unknown_cost_calls || 0)) && totals.cost_status !== 'unavailable' ? totals.known_estimated_cost : null;
  const activeTime = typeof run.active_elapsed_seconds === 'number' && Number.isFinite(run.active_elapsed_seconds) && run.active_elapsed_seconds >= 0;
  const elapsed = activeTime ? run.active_elapsed_seconds * 1000 : Date.parse(run.finished_at) - Date.parse(run.started_at);
  if (Number.isFinite(elapsed) && elapsed >= 0) meta.response_time_ms = elapsed;
  if (activeTime) meta.active_time = true;
  if (run.active_time_incomplete === true) meta.active_time_incomplete = true;
  return meta;
}

export function usageView(meta = {}) {
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
  const summary = [];
  const aggregate = meta.usage_scope === 'work_run';
  const calls = count(meta.response_calls);
  if (aggregate && calls != null) summary.push({ text: `${calls.toLocaleString()} ${calls === 1 ? 'call' : 'calls'}` });
  const input = count(meta.input_tokens);
  const output = count(meta.output_tokens);
  const total = count(meta.total_tokens) ?? (input != null && output != null ? count(input + output) : null);
  if (total != null) summary.push({ text: `${total.toLocaleString()} tokens${meta.usage_status === 'partial' ? ' (partial)' : ''}` });
  else if (meta.usage_status) summary.push({ text: 'Tokens unknown' });
  if (Number.isFinite(meta.response_time_ms) && meta.response_time_ms >= 0) {
    summary.push({ text: `${(meta.response_time_ms / 1000).toFixed(1)}s${meta.active_time_incomplete ? ' (partial)' : ''}` });
  }
  if (Number.isFinite(meta.estimated_cost) && meta.estimated_cost >= 0) {
    const qualifier = meta.cost_status === 'complete' ? '' : meta.cost_status === 'partial' ? ' (Partial estimate)' : ' (Completeness unknown)';
    summary.push({ text: `$${meta.estimated_cost.toFixed(6)}${qualifier}`, cost: true });
  } else if (meta.cost_status || meta.estimated_cost != null) summary.push({ text: 'Cost unavailable' });
  const fields = [['input_tokens', 'Input tokens'], ['output_tokens', 'Output tokens'], ['uncached_input_tokens', 'Uncached input'],
    ['cache_read_tokens', 'Cache read'], ['cache_write_tokens', 'Cache write'], ['reasoning_tokens', 'Reasoning']];
  const partial = new Set(Array.isArray(meta.partial_token_fields) ? meta.partial_token_fields : []);
  const rows = fields.filter(([name]) => meta[name] != null || ['input_tokens', 'output_tokens'].includes(name))
    .map(([name, label]) => ({ label, value: count(meta[name]) == null ? 'Unknown'
      : `${meta[name].toLocaleString()}${partial.has(name) ? ' (partial)' : ''}` }));
  if (aggregate && calls != null) rows.unshift({ label: 'Model calls', value: calls.toLocaleString() });
  if (aggregate && calls != null && count(meta.recorded_response_calls) != null && meta.recorded_response_calls !== calls) {
    rows.splice(1, 0, { label: 'Recorded calls', value: meta.recorded_response_calls.toLocaleString() });
  }
  const hints = [];
  if (aggregate) hints.push(meta.active_time
    ? 'Totals cover all model calls in this response across resumes. Active time includes model and tool work; time spent paused is excluded.'
    : 'Totals cover all model calls in this response. Elapsed time includes model and tool work.');
  if (meta.active_time_incomplete) hints.push('Active time is incomplete because the app stopped during an unfinished step. App downtime is excluded.');
  if (meta.coverage_incomplete || partial.size || meta.usage_status === 'partial' || meta.cost_status === 'partial') {
    hints.push('Some records are incomplete. Only known usage and cost are shown.');
  }
  if (aggregate && meta.usage_status === 'unavailable') hints.push('Usage for this saved response is unavailable.');
  if (['cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens'].some(name => count(meta[name]) > 0)) {
    hints.push('Cache tokens are included in input; reasoning tokens are included in output.');
  }
  return { summary, rows, hints, longTier: meta.is_long_context === true };
}
