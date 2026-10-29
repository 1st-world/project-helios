/* Render shared response metadata and usage disclosures for model answers, model calls, and app work results. */

import { refreshIcons } from './ui.js';
import { formatMessageTimestamp } from './timezone.js';
import { createDisclosure } from './disclosure.js';
import { usageView } from './response-usage.js';

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

export function createResponseFooter(footer, { timeLabel = 'Response saved', onResize = () => {} } = {}) {
  let main = footer.querySelector('.message-footer-main');
  if (!main) {
    main = element('div', 'message-footer-main');
    footer.append(main);
  }
  const metadata = element('div', 'message-meta');
  const profile = element('span', 'message-meta-tag message-profile');
  profile.hidden = true;
  const icon = element('i');
  icon.setAttribute('data-lucide', 'bot');
  icon.setAttribute('aria-hidden', 'true');
  const profileName = element('span');
  profile.append(icon, profileName);
  const timestamp = element('span', 'message-timestamp');
  timestamp.hidden = true;
  metadata.append(profile, timestamp);
  main.prepend(metadata);
  let current = {};
  let signature = null;
  let usage = null;
  let disclosure = null;
  let visible = true;
  let destroyed = false;
  const observer = new ResizeObserver(() => {
    if (footer.isConnected && !destroyed) onResize();
  });
  observer.observe(footer);

  function refreshTimestamp() {
    if (destroyed) return;
    const formatted = formatMessageTimestamp(current.created_at);
    timestamp.hidden = !current.created_at;
    if (!formatted) {
      timestamp.textContent = 'Time unknown';
      timestamp.removeAttribute('title');
      timestamp.removeAttribute('aria-label');
      return;
    }
    const time = element('time', null, formatted.label);
    time.dateTime = current.created_at;
    timestamp.replaceChildren(time);
    timestamp.title = `${timeLabel}: ${formatted.detail}`;
    timestamp.setAttribute('aria-label', timestamp.title);
  }

  function update(meta) {
    if (destroyed) return;
    current = meta;
    const name = meta.profile_name || meta.deployment;
    profile.hidden = !name;
    profileName.textContent = name || '';
    refreshTimestamp();
    const view = usageView(meta);
    const nextSignature = JSON.stringify(view);
    if (signature === nextSignature) return;
    signature = nextSignature;
    disclosure?.destroy();
    const expanded = usage?.open === true;
    disclosure = null;
    if (!view.summary.length) {
      usage?.remove();
      usage = null;
      refreshIcons(footer);
      return;
    }
    usage ||= element('details', 'message-usage');
    const summary = element('summary', 'message-usage-summary');
    const metrics = element('span', 'message-usage-metrics');
    for (const item of view.summary) metrics.append(element('span', `message-meta-item${item.cost ? ' message-meta-cost' : ''}`, item.text));
    const toggle = element('span', 'message-usage-toggle', 'Details');
    const chevron = element('i');
    chevron.setAttribute('data-lucide', 'chevron-down');
    chevron.setAttribute('aria-hidden', 'true');
    toggle.append(chevron);
    summary.append(metrics, toggle);
    const body = element('div', 'message-usage-body');
    const fields = element('dl', 'message-usage-fields');
    const rows = [...view.rows, ...(view.longTier ? [{ label: 'Pricing tier', value: 'Long Tier' }] : [])];
    for (const row of rows) {
      const pair = element('div');
      const value = element('dd');
      if (row.label === 'Pricing tier') value.append(element('span', 'message-meta-tag message-meta-tier', row.value));
      else value.textContent = row.value;
      pair.append(element('dt', null, row.label), value);
      fields.append(pair);
    }
    body.append(fields);
    for (const hint of view.hints) body.append(element('p', 'message-usage-hint', hint));
    usage.replaceChildren(summary, body);
    usage.open = expanded;
    usage.hidden = !visible;
    if (usage.parentElement !== footer) footer.append(usage);
    disclosure = createDisclosure(usage, body);
    refreshIcons(footer);
  }

  return { update, refreshTimestamp,
    setUsageVisibility(value) { visible = value; if (!value) disclosure?.finish(); if (usage) usage.hidden = !value; },
    finish() { disclosure?.finish(); },
    destroy() { destroyed = true; observer.disconnect(); disclosure?.destroy(); }
  };
}
