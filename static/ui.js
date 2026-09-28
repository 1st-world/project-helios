/* Provide shared DOM helpers, notifications, sidebar behavior, and dialog interactions. */

export const $ = (selector) => document.querySelector(selector);

export function escapeHtml(value) {
  const div = document.createElement('div');
  div.textContent = value;
  return div.innerHTML;
}

export function refreshIcons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { 'stroke-width': 1.8 } });
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
  function closeDialog(dialog) {
    if (beforeDialogClose(dialog)) dialog.close();
  }

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
  });

  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
      setMobileSidebarOpen(false);
    }
  });

  document.querySelectorAll('[data-close-dialog]').forEach((button) => {
    button.onclick = () => {
      closeDialog($(`#${button.dataset.closeDialog}`));
    };
  });

  document.querySelectorAll('dialog').forEach((dlg) => {
    let isMouseDownOnBackdrop = false;

    dlg.addEventListener('cancel', (event) => {
      if (!beforeDialogClose(dlg)) event.preventDefault();
    });

    dlg.addEventListener('mousedown', (event) => {
      if (event.target !== dlg) {
        isMouseDownOnBackdrop = false;
        return;
      }
      const rect = dlg.getBoundingClientRect();
      const isInside =
        event.clientX >= rect.left &&
        event.clientX <= rect.right &&
        event.clientY >= rect.top &&
        event.clientY <= rect.bottom;
      isMouseDownOnBackdrop = !isInside;
    });

    dlg.addEventListener('mouseup', (event) => {
      if (isMouseDownOnBackdrop && event.target === dlg) {
        const rect = dlg.getBoundingClientRect();
        const isInside =
          event.clientX >= rect.left &&
          event.clientX <= rect.right &&
          event.clientY >= rect.top &&
          event.clientY <= rect.bottom;
        if (!isInside) {
          closeDialog(dlg);
        }
      }
      isMouseDownOnBackdrop = false;
    });

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
