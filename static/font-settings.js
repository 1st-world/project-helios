/* Manage browser-local font family and text size preferences, with optional installed font discovery. */

import { $ } from './ui.js';

const STORAGE_KEY = 'helios.uiFont';
const SCALE_STORAGE_KEY = 'helios.uiFontScale';
const DEFAULT_FONT = { source: 'default', family: '' };

function normalizePreference(value) {
  if (!value || !['default', 'system', 'installed'].includes(value.source)) return { ...DEFAULT_FONT };
  if (value.source !== 'installed') return { source: value.source, family: '' };
  if (typeof value.family !== 'string') return { ...DEFAULT_FONT };
  const family = value.family.trim();
  if (!family || family.length > 200 || /[\u0000-\u001f\u007f]/.test(family)) return { ...DEFAULT_FONT };
  return { source: 'installed', family };
}

function readPreference() {
  try { return normalizePreference(JSON.parse(localStorage.getItem(STORAGE_KEY))); }
  catch { return { ...DEFAULT_FONT }; }
}

function quoteFamily(family) {
  return `"${family.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
}

export function initializeFontSizeSettings() {
  const root = document.documentElement;
  const size = $('#ui-font-scale');
  const status = $('#font-size-settings-status');
  const allowedScales = new Set([...size.options].map(option => Number(option.value)));

  function readScale() {
    try {
      const scale = Number(localStorage.getItem(SCALE_STORAGE_KEY));
      return allowedScales.has(scale) ? scale : 1;
    } catch { return 1; }
  }

  function applyScale(scale) {
    if (scale === 1) root.style.removeProperty('--ui-font-scale');
    else root.style.setProperty('--ui-font-scale', String(scale));
    size.value = String(scale);
    status.textContent = '';
    document.dispatchEvent(new Event('text-size-change'));
  }

  size.addEventListener('change', () => {
    const value = Number(size.value);
    const scale = allowedScales.has(value) ? value : 1;
    applyScale(scale);
    try {
      if (scale === 1) localStorage.removeItem(SCALE_STORAGE_KEY);
      else localStorage.setItem(SCALE_STORAGE_KEY, String(scale));
    } catch {
      status.textContent = 'Your browser could not save the text size preference. It applies for this session only.';
    }
  });
  window.addEventListener('storage', event => {
    if (event.key === SCALE_STORAGE_KEY || event.key === null) applyScale(readScale());
  });
  $('#settings-dialog').addEventListener('close', () => {
    if (!$('#settings-dialog').open) status.textContent = '';
  });

  applyScale(readScale());
}

export function initializeFontSettings() {
  const root = document.documentElement;
  const source = $("#ui-font-source");
  const family = $("#ui-font-family");
  const localFields = $("#font-settings-local");
  const list = $("#installed-font-families");
  const listStatus = $("#installed-fonts-status");
  const status = $("#font-settings-status");
  const defaultStack = getComputedStyle(root).getPropertyValue("--font-sans").trim();
  const canListFonts = window.isSecureContext && typeof window.queryLocalFonts === "function";
  let saved = readPreference();
  let listing = false;
  let listMessage = canListFonts
    ? ""
    : "Automatic font listing is unavailable here. You can still enter an installed font family name.";

  function applyPreference(preference) {
    if (preference.source === "default") root.style.removeProperty("--font-sans");
    else root.style.setProperty("--font-sans", preference.source === "system"
      ? "var(--font-system)"
      : `${quoteFamily(preference.family)}, ${defaultStack}`);
  }

  function updateControls() {
    localFields.hidden = source.value !== "installed";
    listStatus.textContent = listMessage;
  }

  function fillSaved() {
    source.value = saved.source;
    family.value = saved.family;
    family.setCustomValidity("");
    family.removeAttribute("aria-invalid");
    status.textContent = "";
    updateControls();
  }

  function persist(preference) {
    saved = preference;
    applyPreference(saved);
    status.textContent = "";
    try {
      if (saved.source === "default") localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
    } catch {
      status.textContent = "Your browser could not save the font preference. It applies for this session only.";
    }
  }

  function applySelection() {
    family.setCustomValidity("");
    family.removeAttribute("aria-invalid");
    const preference = normalizePreference({ source: source.value, family: family.value });
    if (source.value === "installed" && family.value.trim() && preference.source !== "installed") {
      const message = "Enter a font family name of up to 200 characters without control characters.";
      family.setCustomValidity(message);
      family.setAttribute("aria-invalid", "true");
      status.textContent = message;
      return;
    }
    persist(preference);
  }

  async function listInstalledFonts() {
    if (!canListFonts || listing) return;
    listing = true;
    listMessage = "Waiting for access to installed fonts...";
    updateControls();
    try {
      // Call directly from the selection event to preserve browser user activation.
      const fonts = await window.queryLocalFonts();
      const families = [...new Set(fonts.map(font => normalizePreference({ source: "installed", family: font.family }))
        .filter(preference => preference.source === "installed").map(preference => preference.family))]
        .sort((left, right) => left.localeCompare(right));
      const options = families.map(name => {
        const option = document.createElement("option");
        option.value = name;
        return option;
      });
      list.replaceChildren(...options);
      listMessage = families.length
        ? ""
        : "No font families were returned. You can still enter a family name.";
    } catch (error) {
      listMessage = error.name === "NotAllowedError"
        ? "Font access was denied. You can still enter a family name directly."
        : "Could not list installed fonts. You can still enter a family name directly.";
    } finally {
      listing = false;
      updateControls();
    }
  }

  source.addEventListener("change", () => {
    updateControls();
    applySelection();
    if (source.value === "installed") listInstalledFonts();
  });
  family.addEventListener("input", applySelection);
  family.addEventListener("change", applySelection);
  $("#settings-dialog").addEventListener("close", () => {
    if ($("#settings-dialog").open) return;
    fillSaved();
  });
  window.addEventListener("storage", event => {
    if (event.key !== STORAGE_KEY && event.key !== null) return;
    saved = readPreference();
    applyPreference(saved);
    fillSaved();
  });

  // Restore the preference without requesting font access during page startup.
  applyPreference(saved);
  fillSaved();
}
