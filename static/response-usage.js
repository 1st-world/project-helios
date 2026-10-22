/* Describe response and model-call usage with explicit aggregate scope and incomplete-record qualifiers. */

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
    summary.push({ text: `${(meta.response_time_ms / 1000).toFixed(1)}s` });
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
  if (aggregate) hints.push('Totals cover all model calls in this response. Elapsed time includes model and tool work.');
  if (meta.coverage_incomplete || partial.size || meta.usage_status === 'partial' || meta.cost_status === 'partial') {
    hints.push('Some records are incomplete. Only known usage and cost are shown.');
  }
  if (aggregate && meta.usage_status === 'unavailable') hints.push('Usage for this saved response is unavailable.');
  if (['cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens'].some(name => count(meta[name]) > 0)) {
    hints.push('Cache tokens are included in input; reasoning tokens are included in output.');
  }
  return { summary, rows, hints, longTier: meta.is_long_context === true };
}
