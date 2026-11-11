/* Manage overflow, chip-by-chip navigation, and mouse dragging in the composer's attachment rail. */

export function createAttachmentRail(rail) {
  const viewport = rail.querySelector('.attachment-rail-viewport');
  const track = rail.querySelector('.attachment-rail-track');
  const previous = rail.querySelector('[data-attachment-direction="-1"]');
  const next = rail.querySelector('[data-attachment-direction="1"]');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const edgeTolerance = 1;
  let pendingLeft = null;
  let drag = null;

  function updateEdges() {
    const left = viewport.scrollLeft;
    if (pendingLeft !== null && Math.abs(left - pendingLeft) <= edgeTolerance) pendingLeft = null;
    const hidePrevious = left <= edgeTolerance;
    const hideNext = left >= viewport.scrollWidth - viewport.clientWidth - edgeTolerance;
    if ((hidePrevious && document.activeElement === previous) || (hideNext && document.activeElement === next)) {
      viewport.focus({ preventScroll: true });
    }
    previous.hidden = hidePrevious;
    next.hidden = hideNext;
  }

  function refresh() {
    const wasHidden = rail.hidden;
    rail.hidden = !track.childElementCount;
    const overflow = !rail.hidden && viewport.scrollWidth > viewport.clientWidth;
    rail.toggleAttribute('data-overflow', overflow);
    viewport.tabIndex = overflow ? 0 : -1;
    if (!rail.hidden && (wasHidden || !overflow)) viewport.scrollLeft = 0;
    pendingLeft = null;
    updateEdges();
  }

  function move(direction) {
    const left = pendingLeft ?? viewport.scrollLeft;
    const end = viewport.scrollWidth - viewport.clientWidth;
    const inset = Number.parseFloat(getComputedStyle(viewport).scrollPaddingInlineStart);
    const trackLeft = track.getBoundingClientRect().left;
    // Align the whole chip beyond the fade, using its fractional position.
    const starts = Array.from(track.children, chip => Math.max(0, chip.getBoundingClientRect().left - trackLeft - inset));
    const destination = direction < 0
      ? starts.findLast(start => start < left - edgeTolerance) ?? 0
      : starts.find(start => start > left + edgeTolerance) ?? end;
    pendingLeft = Math.min(destination, end);
    viewport.scrollTo({ left: pendingLeft, behavior: reducedMotion.matches ? 'auto' : 'smooth' });
  }

  previous.addEventListener('click', () => move(-1));
  next.addEventListener('click', () => move(1));
  viewport.addEventListener('scroll', updateEdges, { passive: true });
  viewport.addEventListener('wheel', () => { pendingLeft = null; }, { passive: true });
  viewport.addEventListener('keydown', event => {
    if (event.target !== viewport || event.altKey || event.ctrlKey || event.metaKey || !rail.hasAttribute('data-overflow')) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      move(event.key === 'ArrowLeft' ? -1 : 1);
    } else {
      pendingLeft = null;
    }
  });
  viewport.addEventListener('pointerdown', event => {
    pendingLeft = null;
    if (event.pointerType !== 'mouse' || event.button !== 0 || event.target.closest('button') || !rail.hasAttribute('data-overflow')) return;
    drag = { id: event.pointerId, x: event.clientX, left: viewport.scrollLeft };
  });
  viewport.addEventListener('pointermove', event => {
    if (drag?.id !== event.pointerId) return;
    const distance = event.clientX - drag.x;
    if (!viewport.hasPointerCapture(drag.id)) {
      if (Math.abs(distance) < 5) return;
      viewport.setPointerCapture(drag.id);
      viewport.setAttribute('data-dragging', '');
    }
    viewport.scrollLeft = drag.left - distance;
  });

  function stopDrag(event) {
    if (drag?.id !== event.pointerId) return;
    drag = null;
    viewport.removeAttribute('data-dragging');
  }

  viewport.addEventListener('pointerup', stopDrag);
  viewport.addEventListener('pointercancel', stopDrag);
  viewport.addEventListener('lostpointercapture', stopDrag);
  viewport.addEventListener('pointerleave', event => {
    if (!viewport.hasPointerCapture(event.pointerId)) stopDrag(event);
  });

  const observer = new ResizeObserver(refresh);
  observer.observe(rail);
  observer.observe(track);

  return { track, refresh };
}
