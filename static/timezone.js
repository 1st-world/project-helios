/* Manage the browser-local app timezone and provide shared timestamp and calendar-date formatting. */

import { $, toast } from './ui.js';

const STORAGE_KEY = 'helios.uiTimezone';

function normalizeTimezone(value) {
  if (typeof value !== 'string' || !/^[A-Za-z][A-Za-z0-9_+\-/]*$/.test(value)) return 'browser';
  try { return Intl.DateTimeFormat(undefined, { timeZone: value }).resolvedOptions().timeZone; }
  catch { return 'browser'; }
}

function readPreference() {
  try { return normalizeTimezone(localStorage.getItem(STORAGE_KEY)); }
  catch { return 'browser'; }
}

let preference = readPreference();

export function getTimezonePreference() {
  return preference;
}

export function getTimezone() {
  if (preference !== 'browser') return preference;
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; }
  catch { return 'UTC'; }
}

export function calendarDate(date = new Date()) {
  const parts = Object.fromEntries(Intl.DateTimeFormat('en-US', {
    timeZone: getTimezone(), year: 'numeric', month: '2-digit', day: '2-digit'
  }).formatToParts(date).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

export function formatTimestamp(value) {
  if (typeof value !== 'string' || !/(Z|[+-]\d{2}:\d{2})$/.test(value)) return 'Unknown';
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString(undefined, { timeZone: getTimezone() }) : 'Unknown';
}

export function initializeTimezoneSettings() {
  const select = $('#ui-timezone');
  let zones;
  try { zones = Intl.supportedValuesOf('timeZone'); }
  catch { zones = []; }
  const names = new Set(['UTC', getTimezone(), ...(preference === 'browser' ? [] : [preference]), ...zones]);
  for (const name of [...names].sort()) select.add(new Option(name, name));

  function fillPreference() {
    if (![...select.options].some(option => option.value === preference)) select.add(new Option(preference, preference));
    select.value = preference;
    select.title = getTimezone();
  }

  function applyPreference(value) {
    preference = value === 'browser' ? 'browser' : normalizeTimezone(value);
    fillPreference();
    document.dispatchEvent(new Event('timezone-change'));
  }

  select.addEventListener('change', () => {
    applyPreference(select.value);
    try {
      if (preference === 'browser') localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, preference);
    } catch {
      toast('Your browser could not save the timezone preference. It applies for this session only.', 'error');
    }
  });

  window.addEventListener('storage', event => {
    if (event.key === STORAGE_KEY || event.key === null) applyPreference(readPreference());
  });

  fillPreference();
}
