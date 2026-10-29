/* Describe an explicitly selected model continuation and pin the boundary approved by the user. */

import { nameInText } from './ui.js';

export function resumeBoundary(run) {
  return JSON.stringify([run.id, run.status, run.execution_segment_id, run.execution_revision, run.checkpoint_revision]);
}

export function resumeSelection(selection) {
  return JSON.stringify([selection?.epoch, selection?.profileId, selection?.revision, selection?.signature]);
}

export function describeResume(run, selection) {
  const current = nameInText(run.profile?.name || run.profile?.deployment, 'the saved model');
  const target = selection?.profile;
  const name = nameInText(target?.name || target?.deployment, 'the selected model');
  const switching = Boolean(target && target.id !== run.profile?.id);
  let host = '';
  try { host = new URL(target?.endpoint).host; } catch { /* An invalid endpoint is rejected by the server. */ }
  const label = `Resume with ${name}`;
  return {
    switching, label, confirmLabel: switching ? 'Change model & resume' : 'Resume',
    title: switching ? 'Continue with a different model?' : 'Resume this run?',
    description: (switching
      ? `Change from ${current} to ${name}${target?.deployment ? ` (${target.deployment})` : ''}${host ? ` at ${nameInText(host)}` : ''}. The new model receives the original request, completed model messages and saved tool results. Previous reasoning is excluded. Unexecuted tool requests are reconsidered. The selected connection’s memory permissions, input budget, output limit and pricing apply. If the initial switch fails, the previous paused checkpoint stays available; attempted calls may still incur costs.`
      : `Continue with ${current} using its saved connection, tools, attachments and limits. The app checks the question, Workspace and saved file results before continuing. Further model calls may incur costs.`)
      + ' Calls, cost and active time remain cumulative across models and resumes; paused time is excluded. Your message draft stays unchanged.'
      + (run.kind === 'regeneration' ? ' Once completed, the regenerated answer replaces the previous answer and all following messages in this branch.' : '')
  };
}
