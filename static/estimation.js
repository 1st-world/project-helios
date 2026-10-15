/* Describe general-purpose token estimation methods in saved connection policies. */

export function describeEstimation(policy) {
  if (!policy?.estimation_method || policy.estimation_method === 'disabled') return '';
  const methods = {
    o200k_base_with_approximate_framing: 'o200k_base',
    utf8_byte_heuristic_with_approximate_framing: 'UTF-8 byte heuristic',
  };
  const method = Object.hasOwn(methods, policy.estimation_method) ? methods[policy.estimation_method] : 'local heuristic';
  return `Estimation: ${method} (not model-specific).`;
}
