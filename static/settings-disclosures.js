/* Animate Settings disclosures while preserving their intended state across tab changes and dialog reopening. */

import { $ } from './ui.js';

function createDisclosure(details, reducedMotion) {
  const summary = details.querySelector(':scope > summary');
  const body = details.querySelector(':scope > .settings-disclosure-body');
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

  const observer = new MutationObserver(() => {
    if (details.open !== targetOpen) finish(details.open);
  });
  observer.observe(details, { attributes: true, attributeFilter: ['open'] });
  new ResizeObserver(() => {
    const height = body.getBoundingClientRect().height;
    if (heightAnimation && Math.abs(height - bodyHeight) > 0.5) toggle(targetOpen);
    bodyHeight = height;
  }).observe(body);
  summary.addEventListener('click', event => {
    event.preventDefault();
    toggle(!targetOpen);
  });
  return { finish };
}

export function initializeSettingsDisclosures() {
  const dialog = $('#settings-dialog');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const disclosures = [...dialog.querySelectorAll('.settings-disclosure')].map(details => ({ details, controller: createDisclosure(details, reducedMotion) }));
  reducedMotion.addEventListener('change', () => {
    if (reducedMotion.matches) disclosures.forEach(({ controller }) => controller.finish());
  });
  dialog.addEventListener('settings-panel-change', () => {
    disclosures.filter(({ details }) => details.closest('.settings-panel').hidden).forEach(({ controller }) => controller.finish());
  });
  dialog.addEventListener('close', () => {
    if (!dialog.open) disclosures.forEach(({ controller }) => controller.finish());
  });
}
