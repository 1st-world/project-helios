/* Provide shared display and DOM helpers, notifications, sidebar behavior, dialog interactions, and composer popover placement. */

import { bindDialogBackdrop } from './dialogs.js';

export const $ = (selector) => document.querySelector(selector);

const sizeUnits = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
const sizeNumber = new Intl.NumberFormat('en', { maximumFractionDigits: 1 });

export function formatSize(bytes) {
  let unit = 0;
  while (bytes >= 1024 && unit < sizeUnits.length - 1) {
    bytes /= 1024;
    unit++;
  }
  return `${sizeNumber.format(bytes)} ${sizeUnits[unit]}`;
}

export function formatPageRange(offset, count, total) {
  const first = (offset + 1).toLocaleString();
  const last = (offset + count).toLocaleString();
  return `${first === last ? first : `${first}–${last}`} of ${total.toLocaleString()}`;
}

export function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}

export function setTextWithCode(element, text, codePattern) {
  // A single capturing group marks the literals to render as code.
  const parts = text.split(codePattern);
  const literals = parts.filter((_, index) => index % 2);
  const existing = element.querySelectorAll('code');
  if (element.textContent === text && existing.length === literals.length
      && literals.every((literal, index) => existing[index].textContent === literal)) return;
  element.replaceChildren(...parts.flatMap((part, index) => {
    if (!part) return [];
    if (index % 2 === 0) return [part];
    const code = document.createElement('code');
    code.textContent = part;
    return [code];
  }));
}

const statusIcons = {
  info: 'info', running: 'loader-circle', paused: 'pause', stopped: 'square',
  success: 'check', warning: 'triangle-alert', error: 'circle-alert'
};

export function setStatusIcon(element, tone) {
  const icon = statusIcons[tone];
  const marker = element.querySelector(':scope > .inline-status-icon');
  const content = element.querySelector(':scope > .inline-status-message');
  if (!icon) {
    if (marker) element.replaceChildren(...content.childNodes);
    element.classList.remove('inline-status');
    delete element.dataset.tone;
    return;
  }
  if (marker && element.dataset.tone === tone) return;
  const nextMarker = document.createElement('span');
  nextMarker.className = 'inline-status-icon';
  nextMarker.setAttribute('aria-hidden', 'true');
  const glyph = document.createElement('i');
  glyph.dataset.lucide = icon;
  nextMarker.append(glyph);
  if (marker) marker.replaceWith(nextMarker);
  else {
    const copy = document.createElement('span');
    copy.className = 'inline-status-message';
    copy.append(...element.childNodes);
    element.append(nextMarker, copy);
  }
  element.classList.add('inline-status');
  element.dataset.tone = tone;
  refreshIcons(element);
}

export function setStatusMessage(element, message, tone, codePattern) {
  const content = element.querySelector(':scope > .inline-status-message') || element;
  if (codePattern) setTextWithCode(content, message, codePattern);
  else setText(content, message);
  setStatusIcon(element, message ? tone : null);
}

export function setRefreshBusy(button, busy, disabled = busy) {
  button.setAttribute('aria-busy', String(busy));
  button.disabled = disabled;
}

export function escapeHtml(value) {
  const div = document.createElement('div');
  div.textContent = value;
  return div.innerHTML;
}

export function nameInText(value, fallback = '') {
  return value ? `“${value}”` : fallback;
}

export function refreshIcons(root = document) {
  if (!window.lucide) return;
  const pending = root.querySelectorAll('i[data-lucide]');
  if (!pending.length) return;
  // Lucide retains data-lucide on SVGs; mark only new placeholders for conversion.
  pending.forEach((icon) => icon.setAttribute('data-helios-icon', icon.getAttribute('data-lucide')));
  window.lucide.createIcons({ root, nameAttr: 'data-helios-icon', attrs: { 'stroke-width': 1.8 } });
  root.querySelectorAll('svg[data-helios-icon]').forEach((icon) => icon.removeAttribute('data-helios-icon'));
}

const toastDuration = 4500;
let toastTimer = null;
let toastFrame = null;
let toastAnnouncementTimer = null;
let toastFocusReturn = null;
let toastObserver;
let toastElement;
let toastAnnouncement;
let modalOrder = [];

function placeToast(records) {
  // Opening mutations follow the modal stack even when dialogs appear in a different DOM order.
  for (const { target, oldValue } of records) {
    if (!(target instanceof HTMLDialogElement) || oldValue !== null || !target.matches(':modal')) continue;
    modalOrder = modalOrder.filter(dialog => dialog !== target);
    modalOrder.push(target);
  }
  modalOrder = modalOrder.filter(dialog => dialog.isConnected && dialog.matches(':modal'));
  const host = modalOrder.at(-1) || document.body;
  if (toastElement.parentElement !== host) {
    toastElement.classList.remove('toast-entering');
    setText(toastAnnouncement, '');
    host.append(toastElement, toastAnnouncement);
  }
}

function stopToastTimer() {
  clearTimeout(toastTimer);
  cancelAnimationFrame(toastFrame);
  toastTimer = toastFrame = null;
  toastElement.removeAttribute('data-countdown');
}

function dismissToast() {
  const focusWasInside = toastElement.contains(document.activeElement);
  stopToastTimer();
  clearTimeout(toastAnnouncementTimer);
  setText(toastAnnouncement, '');
  toastElement.classList.add('hidden');
  if (focusWasInside && toastFocusReturn?.isConnected) toastFocusReturn.focus({ preventScroll: true });
  toastFocusReturn = null;
}

function startToastTimer() {
  toastTimer = setTimeout(dismissToast, toastDuration);
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const started = performance.now();
  toastElement.style.setProperty('--toast-remaining', '100%');
  toastElement.setAttribute('data-countdown', '');

  function updateRing(now) {
    const remaining = Math.max(0, 1 - (now - started) / toastDuration);
    toastElement.style.setProperty('--toast-remaining', `${remaining * 100}%`);
    if (remaining) toastFrame = requestAnimationFrame(updateRing);
  }

  toastFrame = requestAnimationFrame(updateRing);
}

function initializeToast() {
  if (toastObserver) return;
  toastElement = $('#toast');
  toastAnnouncement = $('#toast-announcement');
  if (!toastElement || !toastAnnouncement) return;
  modalOrder = [...document.querySelectorAll('dialog:modal')];
  toastObserver = new MutationObserver(records => {
    if (records.some(({ target }) => target instanceof HTMLDialogElement)) placeToast(records);
  });
  toastObserver.observe(document.body, { subtree: true, attributeFilter: ['open'], attributeOldValue: true });
  placeToast([]);
  toastElement.querySelector('.toast-dismiss').onclick = dismissToast;
  toastElement.addEventListener('pointerenter', event => {
    if (event.pointerType !== 'touch') stopToastTimer();
  });
  toastElement.addEventListener('focusin', event => {
    toastFocusReturn = event.relatedTarget;
    stopToastTimer();
  });
}

export function toast(message, type = 'info') {
  initializeToast();
  if (!toastObserver) return;
  const icons = {
    success: 'check-circle-2',
    error: 'alert-circle',
    danger: 'alert-circle',
    warning: 'alert-triangle',
    info: 'info'
  };
  const text = String(message);
  const icon = document.createElement('i');
  icon.dataset.lucide = icons[type] || 'info';
  stopToastTimer();
  // A caller can open a modal and display a notification in the same turn.
  placeToast(toastObserver.takeRecords());
  toastElement.querySelector('.toast-icon').replaceChildren(icon);
  setText(toastElement.querySelector('.toast-message'), text);
  toastElement.className = `toast toast-${type} toast-entering`;
  refreshIcons(toastElement);
  setText(toastAnnouncement, '');
  clearTimeout(toastAnnouncementTimer);
  toastAnnouncementTimer = setTimeout(() => setText(toastAnnouncement, text), 0);
  const hovered = window.matchMedia('(any-hover: hover)').matches && toastElement.matches(':hover');
  if (!hovered && !toastElement.contains(document.activeElement)) startToastTimer();
}

function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle('sidebar-collapsed', collapsed);
  const toggle = $('#sidebar-toggle');
  const showExpand = collapsed && window.innerWidth > 720;
  const iconName = showExpand ? 'panel-left-open' : 'panel-left-close';
  if (toggle.dataset.icon !== iconName) {
    toggle.innerHTML = `<i data-lucide="${iconName}"></i>`;
    toggle.dataset.icon = iconName;
  }
  toggle.setAttribute('aria-expanded', String(!showExpand));
  $('#sidebar-navigation').inert = showExpand;
  const sidebar = $('#sidebar');
  if (sidebar) {
    sidebar.inert = window.innerWidth <= 720 && !document.body.classList.contains('sidebar-open');
    if (showExpand) {
      sidebar.title = 'Click to expand sidebar';
    } else {
      sidebar.removeAttribute('title');
    }
  }
  toggle.title = window.innerWidth <= 720 ? 'Close sidebar' : (collapsed ? 'Expand sidebar' : 'Collapse sidebar');
  toggle.setAttribute('aria-label', toggle.title);
  localStorage.setItem('helios.sidebarCollapsed', String(collapsed));
  refreshIcons();
}

function setMobileSidebarOpen(open) {
  document.body.classList.toggle('sidebar-open', open);
  $('#sidebar').inert = window.innerWidth <= 720 && !open;
  const mobileToggle = $('#mobile-sidebar-toggle');
  if (mobileToggle) {
    mobileToggle.innerHTML = `<i data-lucide="${open ? 'panel-left-close' : 'panel-left-open'}"></i>`;
    mobileToggle.title = open ? 'Close sidebar' : 'Open sidebar';
    mobileToggle.setAttribute('aria-label', mobileToggle.title);
  }
  refreshIcons();
}

export function closeMobileSidebar() {
  if (window.innerWidth <= 720 && document.body.classList.contains('sidebar-open')) {
    setMobileSidebarOpen(false);
  }
}

export function initializeUI({ beforeDialogClose }) {
  initializeToast();
  const closingDialogs = new WeakSet();

  async function closeDialog(dialog) {
    if (closingDialogs.has(dialog)) return;
    closingDialogs.add(dialog);
    try {
      if (await beforeDialogClose(dialog)) dialog.close();
    } finally {
      closingDialogs.delete(dialog);
    }
  }

  const composerActions = $('.composer-actions');
  const composerFooter = composerActions.closest('footer');
  const composerPopovers = [...composerActions.querySelectorAll('.tools-popover, .model-popover')];

  function positionComposerPopovers() {
    const bounds = composerFooter.getBoundingClientRect();
    const style = getComputedStyle(composerFooter);
    const left = bounds.left + parseFloat(style.paddingLeft);
    const right = bounds.right - parseFloat(style.paddingRight);
    const viewportTop = window.visualViewport?.offsetTop || 0;
    const navigation = $('#mobile-sidebar-toggle')?.getBoundingClientRect();
    const topLimit = Math.max(viewportTop + 16, navigation?.height ? navigation.bottom + 8 : 0);

    composerPopovers.forEach((popover) => {
      // Keep closed menus sized so the browser does not clamp their saved scroll range.
      if (!popover.closest('details').open) return;

      popover.style.setProperty('--popover-max-width', `${Math.max(0, right - left)}px`);
      const popoverStyle = getComputedStyle(popover);
      const anchorBottom = popover.closest('details').getBoundingClientRect().bottom;
      // Keep expanded menus inside the visible viewport and below the mobile navigation control.
      const availableHeight = Math.max(0, anchorBottom - parseFloat(popoverStyle.bottom) - topLimit);
      popover.style.setProperty('--popover-max-height', `${availableHeight}px`);
      popover.style.setProperty('--popover-offset-x', '0px');
      const rect = popover.getBoundingClientRect();
      const adjustedLeft = Math.max(left, Math.min(rect.left, right - rect.width));
      popover.style.setProperty('--popover-offset-x', `${adjustedLeft - rect.left}px`);
    });
  }

  const popoverResizeObserver = new ResizeObserver(positionComposerPopovers);
  popoverResizeObserver.observe(composerFooter);
  composerPopovers.forEach((popover) => {
    const details = popover.closest('details');
    const summary = details.querySelector('summary');
    summary.addEventListener('click', (event) => {
      if (summary.getAttribute('aria-disabled') === 'true') event.preventDefault();
    });
    summary.addEventListener('keydown', (event) => {
      if (summary.getAttribute('aria-disabled') === 'true' && ['Enter', ' '].includes(event.key)) event.preventDefault();
    });
    details.addEventListener('toggle', positionComposerPopovers);
    popoverResizeObserver.observe(details);
  });
  window.visualViewport?.addEventListener('resize', positionComposerPopovers);
  window.visualViewport?.addEventListener('scroll', positionComposerPopovers);

  const sidebarEl = $('#sidebar');
  if (sidebarEl) {
    sidebarEl.onclick = (event) => {
      if (!document.body.classList.contains('sidebar-collapsed')) return;
      if (event.target.closest('button, .workspace-card, .sidebar-dock-btn')) return;
      setSidebarCollapsed(false);
    };
  }

  $('#sidebar-toggle').onclick = (event) => {
    event.stopPropagation();
    if (window.innerWidth <= 720) {
      setMobileSidebarOpen(false);
    } else {
      setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed'));
    }
  };

  const mobileSidebarToggle = $('#mobile-sidebar-toggle');
  if (mobileSidebarToggle) {
    mobileSidebarToggle.onclick = (event) => {
      event.stopPropagation();
      setMobileSidebarOpen(!document.body.classList.contains('sidebar-open'));
    };
  }

  const sidebarBackdrop = $('#sidebar-backdrop');
  if (sidebarBackdrop) {
    sidebarBackdrop.onclick = () => setMobileSidebarOpen(false);
  }

  const workTools = $('#work-tools');
  document.addEventListener('click', (event) => {
    if (!workTools.contains(event.target)) workTools.open = false;
  });
  workTools.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      workTools.open = false;
      workTools.querySelector('summary').focus();
      event.stopPropagation();
    }
  });

  window.addEventListener('resize', () => {
    document.body.classList.toggle('mobile-layout', window.innerWidth <= 720);
    setSidebarCollapsed(document.body.classList.contains('sidebar-collapsed'));
    if (window.innerWidth > 720 && document.body.classList.contains('sidebar-open')) {
      document.body.classList.remove('sidebar-open');
    }
    positionComposerPopovers();
  });

  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !document.querySelector('dialog[open]') && document.body.classList.contains('sidebar-open')) {
      setMobileSidebarOpen(false);
    }
  });

  document.querySelectorAll('[data-close-dialog]').forEach((button) => {
    button.onclick = () => {
      closeDialog($(`#${button.dataset.closeDialog}`));
    };
  });

  for (const button of document.querySelectorAll('[data-date-picker]')) {
    const input = document.getElementById(button.dataset.datePicker);
    if (typeof input?.showPicker !== 'function') continue;
    button.classList.remove('hidden');
    input.classList.add('date-picker-input');
    button.onclick = () => {
      if (input.disabled) return;
      input.focus({ preventScroll: true });
      input.showPicker();
    };
  }

  document.querySelectorAll('dialog').forEach((dlg) => {
    dlg.addEventListener('cancel', (event) => {
      event.preventDefault();
      closeDialog(dlg);
    });

    bindDialogBackdrop(dlg, () => closeDialog(dlg));
  });

  document.body.classList.toggle('mobile-layout', window.innerWidth <= 720);
  setSidebarCollapsed(localStorage.getItem('helios.sidebarCollapsed') === 'true');
  refreshIcons();
}
