/* Present reusable contextual help with keyboard dismissal and placement inside the current dialog or viewport. */

export function initializeHelpPopovers(root = document) {
  const ownerDocument = root.ownerDocument || root;
  let current = null;
  const entries = [...root.querySelectorAll('.help-popover-trigger')].flatMap(trigger => {
    const popover = ownerDocument.getElementById(trigger.getAttribute('popovertarget'));
    if (!popover) return [];
    const native = typeof popover.showPopover === 'function';
    if (native) popover.hidden = false;
    else {
      popover.removeAttribute('popover');
      trigger.removeAttribute('popovertarget');
    }
    return [{ trigger, popover, native }];
  });

  function isOpen(entry) {
    return entry.native ? entry.popover.matches(':popover-open') : !entry.popover.hidden;
  }

  function position() {
    if (!current || !isOpen(current)) return;
    const { trigger, popover } = current;
    const viewport = window.visualViewport;
    const viewportLeft = viewport?.offsetLeft || 0;
    const viewportTop = viewport?.offsetTop || 0;
    const viewportRight = viewportLeft + (viewport?.width || innerWidth);
    const viewportBottom = viewportTop + (viewport?.height || innerHeight);
    const bounds = trigger.closest('dialog')?.getBoundingClientRect();
    const inset = parseFloat(getComputedStyle(popover).paddingLeft) || 0;
    const left = Math.max(bounds?.left ?? viewportLeft, viewportLeft) + inset;
    const right = Math.min(bounds?.right ?? viewportRight, viewportRight) - inset;
    const top = Math.max(bounds?.top ?? viewportTop, viewportTop) + inset;
    const bottom = Math.min(bounds?.bottom ?? viewportBottom, viewportBottom) - inset;
    const anchor = trigger.getBoundingClientRect();
    const below = Math.max(0, bottom - anchor.bottom - 8);
    const above = Math.max(0, anchor.top - top - 8);
    popover.style.maxWidth = `${Math.max(0, right - left)}px`;
    popover.style.maxHeight = `${Math.max(above, below)}px`;
    const size = popover.getBoundingClientRect();
    const y = size.height <= below ? anchor.bottom + 8 : anchor.top - size.height - 8;
    popover.style.left = `${Math.max(left, Math.min(anchor.left, right - size.width))}px`;
    popover.style.top = `${Math.max(top, Math.min(y, bottom - size.height))}px`;
    popover.style.visibility = '';
  }

  function close() {
    if (!current) return;
    const entry = current;
    current = null;
    entry.trigger.setAttribute('aria-expanded', 'false');
    if (entry.native) {
      if (isOpen(entry)) entry.popover.hidePopover();
    } else entry.popover.hidden = true;
  }

  entries.forEach(entry => {
    const { trigger, popover, native } = entry;
    trigger.addEventListener('click', event => {
      event.stopPropagation();
      if (native) return;
      if (current === entry) return close();
      close();
      current = entry;
      popover.hidden = false;
      trigger.setAttribute('aria-expanded', 'true');
      position();
      if (event.detail === 0) popover.focus({ preventScroll: true });
    });
    if (!native) return;
    popover.addEventListener('beforetoggle', event => {
      if (event.newState === 'open') {
        current = entry;
        popover.style.visibility = 'hidden';
      } else {
        trigger.setAttribute('aria-expanded', 'false');
        if (current === entry) current = null;
      }
    });
    popover.addEventListener('toggle', event => {
      const open = event.newState === 'open';
      trigger.setAttribute('aria-expanded', String(open));
      if (open) position();
    });
  });

  ownerDocument.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || !current) return;
    event.preventDefault();
    event.stopPropagation();
    const trigger = current.trigger;
    close();
    trigger.focus({ preventScroll: true });
  });
  ownerDocument.addEventListener('pointerdown', event => {
    if (current && !current.native && !current.trigger.contains(event.target) && !current.popover.contains(event.target)) close();
  });
  ownerDocument.addEventListener('scroll', event => {
    if (current && !current.popover.contains(event.target)) close();
  }, true);
  ownerDocument.addEventListener('close', event => {
    if (current && event.target.contains(current.trigger)) close();
  }, true);
  window.addEventListener('resize', position);
  window.visualViewport?.addEventListener('resize', position);
  window.visualViewport?.addEventListener('scroll', position);
  return { close };
}
