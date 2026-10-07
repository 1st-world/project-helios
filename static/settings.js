/* Navigate settings categories and connect font preferences, app policy editing, and conversation usage visibility. */

import { $ } from './ui.js';
import { createAppSettings } from './app-settings.js';
import { initializeFontSettings, initializeFontSizeSettings } from './font-settings.js';
import { initializeSettingsDisclosures } from './settings-disclosures.js';

const panelScrollPositions = new Map();

function panelScroller(panel) {
  return panel.querySelector('.settings-editor-body') || panel;
}

export function selectSettingsPanel(name, focus = false) {
  const tabs = [...document.querySelectorAll('[data-settings-tab]')];
  const selectedTab = tabs.find((tab) => tab.dataset.settingsTab === name);
  if (!selectedTab) return;

  tabs.forEach((tab) => {
    const selected = tab === selectedTab;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    const panel = $(`#${tab.getAttribute('aria-controls')}`);
    if (!panel.hidden) panelScrollPositions.set(panel.id, panelScroller(panel).scrollTop);
    panel.hidden = !selected;
    if (selected) panelScroller(panel).scrollTop = panelScrollPositions.get(panel.id) || 0;
  });

  if (focus) selectedTab.focus();
  $('#settings-dialog').dispatchEvent(new CustomEvent('settings-panel-change', { detail: { name } }));
}

export function initializeSettings({ onConversationUsageVisibilityChange, hasUnsavedProfileChanges, isProfileSaving }) {
  initializeFontSettings();
  initializeFontSizeSettings();
  initializeSettingsDisclosures();
  const appSettings = createAppSettings({ hasUnsavedProfileChanges, isProfileSaving });
  appSettings.init();
  const tabs = [...document.querySelectorAll('[data-settings-tab]')];
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectSettingsPanel(tab.dataset.settingsTab));
    tab.addEventListener('keydown', (event) => {
      let nextIndex;
      if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === 'Home') nextIndex = 0;
      else if (event.key === 'End') nextIndex = tabs.length - 1;
      else if (['ArrowUp', 'ArrowDown'].includes(event.key)) {
        const columns = getComputedStyle(tab.parentElement).gridTemplateColumns.split(' ').length;
        if (columns >= tabs.length) return;
        nextIndex = (index + (event.key === 'ArrowDown' ? columns : -columns) + tabs.length) % tabs.length;
      }
      else return;

      event.preventDefault();
      selectSettingsPanel(tabs[nextIndex].dataset.settingsTab, true);
    });
  });

  const usageToggle = $('#show-conversation-usage');
  usageToggle.checked = localStorage.getItem('helios.showConversationUsage') !== 'false';
  onConversationUsageVisibilityChange(usageToggle.checked);
  usageToggle.addEventListener('change', () => {
    localStorage.setItem('helios.showConversationUsage', String(usageToggle.checked));
    onConversationUsageVisibilityChange(usageToggle.checked);
  });
  $('#settings-dialog').addEventListener('close', () => {
    if ($('#settings-dialog').open) return;
    panelScrollPositions.clear();
    document.querySelectorAll('.settings-panel').forEach(panel => { panelScroller(panel).scrollTop = 0; });
  });
  return { beforeDialogClose: appSettings.beforeDialogClose };
}
