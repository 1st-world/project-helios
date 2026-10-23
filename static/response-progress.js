/* Present observable request phases and elapsed time without estimating completion or model reasoning. */

const phaseLabels = {
  preparing: 'Preparing request', waiting: 'Waiting for model', receiving: 'Receiving response',
  finalizing: 'Finalizing response', stopping: 'Stopping'
};
const toolProgress = {
  helios_project_list_files: 'Listing files', helios_project_search_text: 'Searching project',
  helios_project_read_file: 'Reading file'
};

export function workProgressLabel(event = {}) {
  if (event.type === 'cancel_requested') return phaseLabels.stopping;
  if (event.type === 'work_usage') return 'Processing model response';
  if (event.type === 'delta') return phaseLabels.finalizing;
  if (event.phase === 'model') return phaseLabels.waiting;
  if (event.phase === 'tool_completed') return event.failed ? 'Tool reported a problem' : 'Preparing next step';
  if (event.phase === 'tool') return Object.hasOwn(toolProgress, event.name) ? toolProgress[event.name] : 'Running tool';
  return 'Preparing exploration';
}

export function createResponseProgress({ startedAt = Date.now(), label = phaseLabels.preparing } = {}) {
  const el = document.createElement('div');
  el.className = 'response-progress';
  const indicator = document.createElement('span');
  indicator.className = 'response-progress-indicator';
  indicator.setAttribute('aria-hidden', 'true');
  const status = document.createElement('span');
  status.className = 'response-progress-label';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  const clock = document.createElement('span');
  clock.className = 'response-progress-clock';
  clock.setAttribute('role', 'timer');
  clock.setAttribute('aria-live', 'off');
  clock.title = 'Elapsed time for this request';
  el.append(indicator, status, clock);
  const source = typeof startedAt === 'number' ? startedAt : Date.parse(startedAt);
  const initialElapsed = Number.isFinite(source) ? Math.max(0, Date.now() - source) : 0;
  const baseline = performance.now();
  let timer = null;
  let destroyed = false;

  function tick() {
    if (destroyed) return;
    const seconds = Math.floor((initialElapsed + Math.max(0, performance.now() - baseline)) / 1000);
    const minutes = Math.floor(seconds / 60);
    clock.textContent = `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
    clock.setAttribute('aria-label', `Elapsed time: ${seconds} ${seconds === 1 ? 'second' : 'seconds'}`);
  }

  function pause() {
    if (timer != null) clearInterval(timer);
    timer = null;
  }

  function resume() {
    pause();
    if (destroyed || document.hidden) return;
    tick();
    timer = setInterval(tick, 1000);
  }

  function update(phase) {
    if (destroyed) return;
    const text = Object.hasOwn(phaseLabels, phase) ? phaseLabels[phase] : phase;
    if (status.textContent !== text) status.textContent = text;
    el.dataset.stopping = String(text === phaseLabels.stopping);
  }

  document.addEventListener('visibilitychange', resume);
  update(label);
  tick();
  resume();
  return { el, update,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      pause();
      document.removeEventListener('visibilitychange', resume);
      el.remove();
    }
  };
}
