/* Describe saved and live work outcomes without treating goals, cancellation, or interruption as provider failures. */

import { describeRequestError } from './chat-feedback.js';

const activeStates = new Set(['preparing', 'model', 'tools', 'ready']);
const goalOutcomes = {
  exploration_call_limit: ['Model call limit reached', 'The model call limit was reached before a final answer was saved. The limit includes the final answer call.'],
  exploration_time_target: ['Time target reached', 'Exploration stopped between steps after reaching the elapsed time target. An in-flight call was allowed to finish. No final answer was saved.'],
  exploration_cost_goal: ['Cost target reached', 'Exploration stopped between steps after the recorded estimated cost reached the target. This target is not a billing cap. No final answer was saved.'],
  exploration_cost_unknown: ['Cost estimate incomplete', 'Further work stopped because cost records were incomplete and a cost target was set. Missing cost is not treated as zero. No final answer was saved.']
};
const compatibilityReasons = new Set(['exploration_protocol_error', 'exploration_reasoning_unavailable', 'exploration_empty_response', 'exploration_unknown_tool']);

export const WORK_ACTION_LABELS = {
  'work-settings': 'Review limits & targets', 'prepare-exploration': 'Prepare new exploration',
  connections: 'Connection settings', usage: 'Review usage', limits: 'Review input budget',
  attachments: 'Review attachments', workspace: 'Review workspace'
};

export function describeWorkRun(run = {}) {
  const reason = run.failure?.code || run.reason;
  const code = typeof reason === 'string' ? reason : '';
  const state = (label, tone, message = '', actions = [], details = []) => ({ label, tone, message, actions, details });
  if (activeStates.has(run.status)) return run.cancellationRequested
    ? state('Stopping', 'neutral', 'Stop was requested. Waiting for in-flight work to close; recorded usage is retained.')
    : state('In progress', 'neutral');
  if (run.status === 'completed') return run.adopted
    ? state('Completed', 'neutral')
    : state('Completed · answer outside current branch', 'neutral', 'This completed attempt is not the saved answer in the current conversation branch. Its activity is retained; no answer is adopted automatically.', ['prepare-exploration']);
  if (run.status === 'unavailable' || !['failed', 'stopped', 'cancelled', 'interrupted'].includes(run.status)) {
    return state('Saved state unavailable', 'warning', 'The saved outcome could not be confirmed. Review recorded usage before starting another request.', ['usage']);
  }
  if (run.status === 'cancelled') return state('Request cancelled', 'neutral',
    'The request was cancelled or disconnected before a final answer was saved. Recorded activity and usage are retained. This attempt is not resumed automatically.', ['prepare-exploration']);
  if (run.status === 'interrupted') return state('Run interrupted', 'warning',
    'The app stopped before this attempt finished. Its saved activity is retained, but the last call may have incomplete usage. Review usage before preparing another exploration.', ['usage', 'prepare-exploration']);
  if (Object.hasOwn(goalOutcomes, code)) {
    const [label, message] = goalOutcomes[code];
    return state(label, 'warning', message, ['work-settings', 'prepare-exploration']);
  }
  if (['input_policy_blocked', 'context_preflight_blocked'].includes(code)) return state('Input check blocked', 'warning',
    'The estimated input exceeded the selected budget before the next model call. Review the input budget or prepare a smaller request. This is a local check, not a confirmed provider rejection.', ['limits', 'prepare-exploration']);
  if (['authority_changed', 'conversation_changed', 'context_recovery_unavailable'].includes(code)) return state('Context changed', 'neutral',
    'The conversation, Workspace, or context permission changed. Further work stopped; this attempt is not resumed automatically. Review the current Workspace before preparing another exploration.', ['workspace', 'prepare-exploration']);
  if (code.startsWith('provider_') || code === 'request_failed' || code === 'connection_not_configured') {
    const feedback = describeRequestError(run.failure || { code }, 'The model provider did not complete this exploration. Recorded activity and usage are retained.');
    const actions = feedback.actions.filter(action => Object.hasOwn(WORK_ACTION_LABELS, action));
    return state(feedback.title, 'error', feedback.message, [...new Set([...actions, 'prepare-exploration'])], feedback.details);
  }
  if (compatibilityReasons.has(code)) return state('Exploration response incompatible', 'error',
    'The model response could not be used for exploration. Review deployment compatibility before trying again. Recorded activity and usage are retained; no automatic retry was made.', ['connections', 'usage', 'prepare-exploration']);
  if (code === 'project_tool_unavailable') return state('Project tool unavailable', 'error',
    'The local project tool session could not finish. Review Workspace availability before preparing another exploration. Recorded work and usage are retained.', ['workspace', 'usage', 'prepare-exploration']);
  return run.status === 'stopped'
    ? state('Exploration stopped', 'neutral', 'This attempt stopped before a final answer was saved. Review its activity before preparing another exploration.', ['work-settings', 'prepare-exploration'])
    : state('Exploration failed', 'error', 'This attempt could not finish. Recorded activity and usage are retained; no automatic retry was made.', ['usage', 'connections', 'prepare-exploration']);
}

export function workStepStatus(status) {
  const labels = { completed: 'Completed', failed: 'Failed', stopped: 'Stopped', cancelled: 'Cancelled', interrupted: 'Interrupted',
    in_progress: 'In progress', pending: 'Pending' };
  return Object.hasOwn(labels, status) ? labels[status] : 'Status unavailable';
}
