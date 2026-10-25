/* Render public summaries and host ordered work records independently of final answer text and input history. */

import { createDisclosure } from './disclosure.js';
import { escapeHtml, refreshIcons } from './ui.js';

export function renderReasoningText(text) {
  return window.DOMPurify ? DOMPurify.sanitize(marked.parse(text)) : escapeHtml(text).replace(/\n/g, '<br>');
}

export function createReasoningSummary({ open = false, work = false } = {}) {
  const el = document.createElement('details');
  el.className = 'reasoning-summary';
  el.open = open;
  el.hidden = !work;
  const header = document.createElement('summary');
  let title = work ? 'Work process' : 'Reasoning summary';
  header.setAttribute('aria-label', title);
  header.title = `Show or hide ${title.toLowerCase()}`;
  const label = document.createElement('span');
  label.className = 'reasoning-summary-label';
  label.textContent = title;
  const toggle = document.createElement('span');
  toggle.className = 'reasoning-summary-toggle';
  const chevron = document.createElement('i');
  chevron.setAttribute('data-lucide', 'chevron-down');
  chevron.setAttribute('aria-hidden', 'true');
  toggle.append(chevron);
  header.append(label, toggle);
  const body = document.createElement('div');
  body.className = 'reasoning-summary-body';
  const list = document.createElement('div');
  list.className = 'reasoning-summary-parts';
  list.tabIndex = 0;
  list.setAttribute('role', 'region');
  list.setAttribute('aria-label', work ? 'Work process content' : 'Reasoning summary content');
  body.append(list);
  el.append(header, body);
  const disclosure = createDisclosure(el, body);
  const calls = new Map();
  let progress = null;
  let destroyed = false;
  let started = false;
  let answering = false;

  function setWork(value) {
    if (destroyed || work === value) return;
    work = value;
    title = work ? 'Work process' : 'Reasoning summary';
    header.setAttribute('aria-label', title);
    header.title = `Show or hide ${title.toLowerCase()}`;
    label.textContent = title;
    list.setAttribute('aria-label', work ? 'Work process content' : 'Reasoning summary content');
    el.hidden = !work && !calls.size;
  }

  function start() {
    if (destroyed || started || answering) return;
    started = true;
    disclosure.finish(true);
  }

  function answerStarted() {
    if (destroyed || answering) return;
    answering = true;
    disclosure.setOpen(false);
  }

  function update(parts = []) {
    if (destroyed) return;
    const followBottom = el.open && list.scrollHeight - list.scrollTop - list.clientHeight <= 32;
    const visible = parts.filter(part => typeof part.text === 'string' && part.text.trim());
    const callIds = [...new Set(visible.map(part => part.call_id))];
    for (const [index, callId] of callIds.entries()) {
      let call = calls.get(callId);
      if (!call) {
        const section = document.createElement('section');
        section.className = 'reasoning-summary-call';
        section.tabIndex = -1;
        const heading = document.createElement('p');
        heading.className = 'reasoning-summary-call-label';
        const content = document.createElement('div');
        content.className = 'reasoning-summary-call-parts';
        section.append(heading, content);
        call = { section, heading, content, views: new Map() };
        calls.set(callId, call);
      }
      const parts = visible.filter(part => part.call_id === callId);
      const labels = [];
      if (callIds.length > 1) labels.push(`Summary ${index + 1}`);
      if (parts.some(part => part.status === 'incomplete')) labels.push('Incomplete summary');
      call.heading.textContent = labels.join(' · ');
      call.heading.hidden = !labels.length;
      call.section.setAttribute('aria-label', `Reasoning summary ${index + 1}`);
      call.section.setAttribute('aria-busy', String(parts.some(part => part.status === 'in_progress')));
      const retained = new Set();
      for (const [position, part] of parts.entries()) {
        const key = JSON.stringify([part.call_id, part.item_id, part.summary_index]);
        retained.add(key);
        let view = call.views.get(key);
        if (!view) {
          const content = document.createElement('div');
          content.className = 'reasoning-summary-content';
          view = { content };
          call.views.set(key, view);
        }
        if (view.text !== part.text) {
          view.content.innerHTML = renderReasoningText(part.text);
          view.text = part.text;
        }
        if (call.content.children[position] !== view.content) call.content.insertBefore(view.content, call.content.children[position] || null);
      }
      for (const [key, view] of call.views) {
        if (retained.has(key)) continue;
        view.content.remove();
        call.views.delete(key);
      }
      if (list.children[index] !== call.section) list.insertBefore(call.section, list.children[index] || null);
    }
    for (const [callId, call] of calls) {
      if (callIds.includes(callId)) continue;
      call.section.remove();
      calls.delete(callId);
    }
    el.hidden = !work && !visible.length;
    if (followBottom) list.scrollTop = list.scrollHeight;
    refreshIcons(el);
  }

  function setProgress(view = null) {
    if (destroyed || progress === view && (!view || view.el.parentElement === header)) return;
    if (progress?.el.parentElement === header) progress.el.remove();
    progress = view;
    if (progress) {
      progress.el.insertBefore(toggle, progress.el.querySelector('.response-progress-clock'));
      header.insertBefore(progress.el, label);
    } else header.append(toggle);
    label.hidden = Boolean(progress);
    el.dataset.progress = String(Boolean(progress));
    if (progress && !el.hidden) start();
  }

  return { el, content: list, update, setProgress, setWork, start, answerStarted, finish: disclosure.finish,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      disclosure.destroy();
      calls.clear();
      el.remove();
    }
  };
}
