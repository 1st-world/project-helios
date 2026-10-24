/* Animate native disclosures, settle interrupted transitions, and release observers when their content is removed. */

export function createDisclosure(details, body, reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)')) {
  const summary = details.querySelector(':scope > summary');
  let targetOpen = details.open;
  let heightAnimation = null;
  let opacityAnimation = null;
  let bodyHeight = body.getBoundingClientRect().height;

  function cancelAnimations() {
    if (heightAnimation) heightAnimation.onfinish = null;
    heightAnimation?.cancel();
    opacityAnimation?.cancel();
    heightAnimation = null;
    opacityAnimation = null;
  }

  function setOpen(open) {
    if (details.open !== open) details.open = open;
    // Do not treat our transition's native state mutation as an external open request.
    observer.takeRecords();
  }

  function finish(open = targetOpen) {
    cancelAnimations();
    targetOpen = open;
    setOpen(open);
    details.style.overflow = '';
    delete details.dataset.expanded;
    bodyHeight = body.getBoundingClientRect().height;
  }

  function toggle(open) {
    const startHeight = details.getBoundingClientRect().height;
    const startOpacity = details.open ? Number(getComputedStyle(body).opacity) : 0;
    cancelAnimations();
    targetOpen = open;
    if (reducedMotion.matches) return finish();

    if (open) setOpen(true);
    const style = getComputedStyle(details);
    const borderHeight = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
    const endHeight = open ? details.getBoundingClientRect().height : summary.getBoundingClientRect().height + borderHeight;
    bodyHeight = body.getBoundingClientRect().height;
    details.style.overflow = 'hidden';
    details.dataset.expanded = String(open);
    const duration = parseFloat(style.getPropertyValue('--motion-duration'));
    const easing = style.getPropertyValue('--motion-easing').trim();
    heightAnimation = details.animate({ height: [`${startHeight}px`, `${endHeight}px`] }, { duration, easing, fill: 'forwards' });
    opacityAnimation = body.animate({ opacity: [startOpacity, open ? 1 : 0] }, { duration, easing, fill: 'forwards' });
    heightAnimation.onfinish = () => finish();
  }

  function onClick(event) {
    event.preventDefault();
    toggle(!targetOpen);
  }

  function onReducedMotion() {
    if (reducedMotion.matches) finish();
  }

  const observer = new MutationObserver(() => {
    if (details.open !== targetOpen) finish(details.open);
  });
  observer.observe(details, { attributes: true, attributeFilter: ['open'] });
  const resizeObserver = new ResizeObserver(() => {
    const height = body.getBoundingClientRect().height;
    if (heightAnimation && Math.abs(height - bodyHeight) > 0.5) toggle(targetOpen);
    bodyHeight = height;
  });
  resizeObserver.observe(body);
  summary.addEventListener('click', onClick);
  reducedMotion.addEventListener('change', onReducedMotion);

  function destroy() {
    finish();
    observer.disconnect();
    resizeObserver.disconnect();
    summary.removeEventListener('click', onClick);
    reducedMotion.removeEventListener('change', onReducedMotion);
  }

  return { finish, destroy, setOpen: toggle };
}
