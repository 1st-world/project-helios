/* Describe saved and live work outcomes without treating goals, cancellation, or interruption as provider failures. */

import { describeRequestError } from './chat-feedback.js';

const activeStates = new Set(['preparing', 'model', 'tools', 'ready']);
const goalOutcomes = {
  exploration_call_limit: ['Model call limit reached', 'The model call limit was reached before a final answer was saved. The limit includes the final answer call.'],
  exploration_time_target: ['Time target reached', 'Work stopped between steps after reaching the elapsed time target. An in-flight call was allowed to finish. No final answer was saved.'],
  exploration_cost_goal: ['Cost target reached', 'Work stopped between steps after the recorded estimated cost reached the target. This target is not a billing cap. No final answer was saved.'],
  exploration_cost_unknown: ['Cost estimate incomplete', 'Further work stopped because cost records were incomplete and a cost target was set. Missing cost is not treated as zero. No final answer was saved.']
};
const compatibilityReasons = new Set(['exploration_protocol_error', 'exploration_reasoning_unavailable', 'exploration_empty_response', 'exploration_unknown_tool']);

export const WORK_ACTION_LABELS = {
  'work-settings': 'Review limits & targets', 'prepare-request': 'Prepare new request',
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
    : state('Completed · answer outside current branch', 'neutral', 'This completed attempt is not the saved answer in the current conversation branch. Its recorded work is retained; no answer is adopted automatically.', ['prepare-request']);
  if (run.status === 'unavailable' || !['failed', 'stopped', 'cancelled', 'interrupted'].includes(run.status)) {
    return state('Saved state unavailable', 'warning', 'The saved outcome could not be confirmed. Review recorded usage before starting another request.', ['usage']);
  }
  if (run.status === 'cancelled') return state('Request cancelled', 'neutral',
    'The request was cancelled or disconnected before a final answer was saved. Recorded work and usage are retained. This attempt is not resumed automatically.', ['prepare-request']);
  if (run.status === 'interrupted') return state('Run interrupted', 'warning',
    'The app stopped before this attempt finished. Its recorded work is retained, but the last call may have incomplete usage. Review usage before preparing a new request.', ['usage', 'prepare-request']);
  if (Object.hasOwn(goalOutcomes, code)) {
    const [label, message] = goalOutcomes[code];
    const goal = {
      exploration_call_limit: ['max_model_calls', 'Model call limit', ''],
      exploration_time_target: ['elapsed_time_target_seconds', 'Time target', 's'],
      exploration_cost_goal: ['cost_goal_usd', 'Cost target', ' USD'],
      exploration_cost_unknown: ['cost_goal_usd', 'Cost target', ' USD']
    }[code];
    const value = run.goals?.[goal[0]];
    const details = typeof value === 'number' && Number.isFinite(value) && value > 0
      ? [`${goal[1]}: ${value}${goal[2]}`] : [];
    return state(label, 'warning', message, ['work-settings', 'prepare-request'], details);
  }
  if (['input_policy_blocked', 'context_preflight_blocked'].includes(code)) return state('Input check blocked', 'warning',
    'The estimated input exceeded the selected budget before the next model call. Review the input budget or prepare a smaller request. This is a local check, not a confirmed provider rejection.', ['limits', 'prepare-request']);
  if (['authority_changed', 'conversation_changed', 'context_recovery_unavailable'].includes(code)) return state('Context changed', 'neutral',
    'The conversation or tool context changed. Further work stopped; this attempt is not resumed automatically. Review the current input and tool settings before preparing a new request.', ['workspace', 'prepare-request']);
  if (code.startsWith('provider_') || code === 'request_failed' || code === 'connection_not_configured') {
    const feedback = describeRequestError(run.failure || { code }, 'The model provider did not complete this request. Recorded work and usage are retained.');
    const actions = feedback.actions.filter(action => Object.hasOwn(WORK_ACTION_LABELS, action));
    return state(feedback.title, 'error', feedback.message, [...new Set([...actions, 'prepare-request'])], feedback.details);
  }
  if (compatibilityReasons.has(code)) return state('Model response incompatible', 'error',
    'The model response could not be used for the selected tools. Review deployment compatibility before trying again. Recorded work and usage are retained; no automatic retry was made.', ['connections', 'usage', 'prepare-request']);
  if (code === 'project_tool_unavailable') return state('Explore project unavailable', 'error',
    'Explore project could not complete its local session. Review Workspace availability before preparing a new request. Recorded work and usage are retained.', ['workspace', 'usage', 'prepare-request']);
  return run.status === 'stopped'
    ? state('Run stopped', 'neutral', 'This attempt stopped before a final answer was saved. Review its work process before preparing a new request.', ['work-settings', 'prepare-request'])
    : state('Run failed', 'error', 'This attempt could not finish. Recorded work and usage are retained; no automatic retry was made.', ['usage', 'connections', 'prepare-request']);
}

export function workStepStatus(status) {
  const labels = { completed: 'Completed', failed: 'Failed', stopped: 'Stopped', cancelled: 'Cancelled', interrupted: 'Interrupted',
    in_progress: 'In progress', pending: 'Pending' };
  return Object.hasOwn(labels, status) ? labels[status] : 'Status unavailable';
}
