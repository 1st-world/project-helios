/* Navigate settings categories and connect app policy editing and conversation usage visibility. */

import { $ } from './ui.js';
import { createAppSettings } from './app-settings.js';

export function selectSettingsPanel(name, focus = false) {
  const tabs = [...document.querySelectorAll('[data-settings-tab]')];
  const selectedTab = tabs.find((tab) => tab.dataset.settingsTab === name);
  if (!selectedTab) return;

  tabs.forEach((tab) => {
    const selected = tab === selectedTab;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    const panel = $(`#${tab.getAttribute('aria-controls')}`);
    panel.hidden = !selected;
    if (selected) panel.scrollTop = 0;
  });

  if (focus) selectedTab.focus();
  $('#settings-dialog').dispatchEvent(new CustomEvent('settings-panel-change', { detail: { name } }));
}

export function initializeSettings({ onConversationUsageVisibilityChange, hasUnsavedProfileChanges, isProfileSaving }) {
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
  return { beforeDialogClose: appSettings.beforeDialogClose };
}
