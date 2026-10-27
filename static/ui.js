/* Provide shared DOM helpers, notifications, sidebar behavior, dialog interactions, and composer popover placement. */

import { bindDialogBackdrop } from './dialogs.js';

export const $ = (selector) => document.querySelector(selector);

export function escapeHtml(value) {
  const div = document.createElement('div');
  div.textContent = value;
  return div.innerHTML;
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

let toastTimer = null;

export function toast(message, type = 'info') {
  const el = $('#toast');
  if (!el) return;
  const icons = {
    success: 'check-circle-2',
    error: 'alert-circle',
    danger: 'alert-circle',
    warning: 'alert-triangle',
    info: 'info'
  };
  const iconName = icons[type] || 'info';
  el.className = `toast toast-${type}`;
  el.innerHTML = `<i data-lucide="${iconName}"></i><span>${escapeHtml(String(message))}</span>`;
  const openDialog = document.querySelector('dialog[open]');
  if (openDialog) {
    if (el.parentElement !== openDialog) {
      openDialog.appendChild(el);
    }
  } else {
    if (el.parentElement !== document.body) {
      document.body.appendChild(el);
    }
  }
  refreshIcons();
  el.classList.remove('hidden');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 4500);
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

  document.querySelectorAll('dialog').forEach((dlg) => {
    dlg.addEventListener('cancel', (event) => {
      event.preventDefault();
      closeDialog(dlg);
    });

    bindDialogBackdrop(dlg, () => closeDialog(dlg));

    dlg.addEventListener('close', () => {
      const el = $('#toast');
      if (el && el.parentElement === dlg) {
        document.body.appendChild(el);
      }
    });
  });

  document.body.classList.toggle('mobile-layout', window.innerWidth <= 720);
  setSidebarCollapsed(localStorage.getItem('helios.sidebarCollapsed') === 'true');
  refreshIcons();
}
