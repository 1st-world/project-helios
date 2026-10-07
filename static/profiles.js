/* Own connection profiles, model selection, and credential, context, and pricing editing. */

import { $, closeMobileSidebar, escapeHtml, refreshIcons, toast } from './ui.js';
import { selectSettingsPanel } from './settings.js';
import { createProfileContext } from './profile-context.js';
import { describeRequestError } from './chat-feedback.js';

const CACHE_PRICE_FIELDS = [
  ['azure-cache-read-price', 'cache_read_price_per_million', 'clear_cache_read_price'],
  ['azure-cache-write-price', 'cache_write_price_per_million', 'clear_cache_write_price'],
  ['azure-long-cache-read-price', 'long_cache_read_price_per_million', 'clear_long_cache_read_price'],
  ['azure-long-cache-write-price', 'long_cache_write_price_per_million', 'clear_long_cache_write_price'],
];

const PRICE_CATEGORIES = [
  ['input', 'azure-input-price', 'azure-long-input-price'],
  ['output', 'azure-output-price', 'azure-long-output-price'],
  ['cache read', 'azure-cache-read-price', 'azure-long-cache-read-price'],
  ['cache write', 'azure-cache-write-price', 'azure-long-cache-write-price'],
];

function readPrice(id) {
  const input = $(`#${id}`);
  const invalid = input.validity.badInput || (input.value !== '' && (!Number.isFinite(input.valueAsNumber) || input.valueAsNumber < 0));
  return { value: input.value === '' || invalid ? null : input.valueAsNumber, invalid };
}

export function createProfiles({ isGenerating }) {
  const state = { activeProfileId: null, editingProfileId: null, profiles: [], formBaseline: null, saving: false, feedback: null };
  const profileContext = createProfileContext({ isSaving: () => state.saving });
  let cachePriceBaseline = {};

  function selectCachePrices(profile) {
    cachePriceBaseline = {};
    for (const [id, field] of CACHE_PRICE_FIELDS) {
      const value = profile?.[field] ?? null;
      cachePriceBaseline[field] = value;
      const input = $(`#${id}`);
      input.value = value ?? '';
      input.setCustomValidity('');
    }
  }

  function validateCachePrices() {
    for (const [id] of CACHE_PRICE_FIELDS) {
      const input = $(`#${id}`);
      const invalid = input.validity.badInput || (input.value !== '' && (!Number.isFinite(input.valueAsNumber) || input.valueAsNumber < 0));
      input.setCustomValidity(invalid ? 'Enter a finite price of 0 or greater, or leave this field blank.' : '');
    }
  }

  function cachePriceChanges(editing) {
    const changes = {};
    for (const [id, field, clear] of CACHE_PRICE_FIELDS) {
      const input = $(`#${id}`);
      const value = input.value === '' ? null : input.valueAsNumber;
      if (editing && value === cachePriceBaseline[field]) continue;
      if (value !== null) changes[field] = value;
      else if (editing) changes[clear] = true;
    }
    return changes;
  }

  function snapshotProfileForm() {
    return JSON.stringify([...$('#azure-settings-form').querySelectorAll('input, select')]
      .map((input) => [input.id, input.value, input.validity.badInput]));
  }

  function hasUnsavedChanges() {
    return state.formBaseline !== null && snapshotProfileForm() !== state.formBaseline;
  }

  function canDiscardProfileChanges() {
    return !state.saving && (!hasUnsavedChanges() || window.confirm('Discard unsaved profile changes?'));
  }

  function setStatus(message, type = 'info') {
    state.feedback = message ? { message, type } : null;
    updateEditorControls();
  }

  function updateEditorControls() {
    const dirty = hasUnsavedChanges();
    const status = $('#profile-settings-status');
    status.textContent = state.feedback?.message || (state.saving ? 'Saving changes...' : dirty ? 'Unsaved changes' : '');
    status.dataset.state = state.feedback?.type || (state.saving ? 'info' : dirty ? 'dirty' : 'idle');
    $('#save-profile-btn').disabled = state.saving || !dirty;
    $('#discard-profile-edit').disabled = state.saving || !dirty;
    $('#save-profile-label').textContent = state.saving ? 'Saving...' : 'Save changes';
  }

  function setSaving(saving) {
    state.saving = saving;
    $('#azure-settings-form').querySelectorAll('input, select, button').forEach(control => { control.disabled = saving; });
    profileContext.updateControls();
    updateEditorControls();
    $('#settings-dialog').dispatchEvent(new Event('profile-save-state-change'));
  }

  function updatePricingStatus() {
    const statusEl = $('#pricing-accordion-status');
    if (!statusEl) return;
    const rates = PRICE_CATEGORIES.map(([name, standardId, longId]) => ({ name, standard: readPrice(standardId), long: readPrice(longId) }));
    const invalid = rates.flatMap(rate => [
      ...(rate.standard.invalid ? [`standard ${rate.name}`] : []),
      ...(rate.long.invalid ? [`long-context ${rate.name}`] : []),
    ]);
    const missingStandard = rates.filter(rate => rate.standard.value === null).map(rate => rate.name);
    // A blank long rate inherits independently; an invalid draft must not appear configured.
    const missingLong = rates.filter(rate => (rate.long.invalid ? null : rate.long.value ?? rate.standard.value) === null).map(rate => rate.name);
    const fallback = rates.filter(rate => !rate.long.invalid && rate.long.value === null && rate.standard.value !== null).map(rate => rate.name);
    const hasPricing = rates.some(rate => rate.standard.value !== null || rate.long.value !== null);
    const state = invalid.length ? 'invalid' : !hasPricing ? 'empty' : missingStandard.length || missingLong.length ? 'partial' : 'configured';
    const labels = { empty: 'Not configured', partial: 'Partial pricing', configured: 'Configured', invalid: 'Check rates' };
    const icons = { empty: 'circle-dashed', partial: 'triangle-alert', configured: 'check', invalid: 'triangle-alert' };
    const standardText = missingStandard.length
      ? `${rates.some(rate => rate.standard.invalid) ? 'Unavailable' : 'Missing'} standard rates: ${missingStandard.join(', ')}.`
      : 'All standard rates are configured.';
    const longText = [
      ...(missingLong.length ? [`${invalid.length ? 'Unavailable' : 'Missing'} effective long-context rates: ${missingLong.join(', ')}.`] : []),
      ...(fallback.length ? [`Long-context fallback to standard rates: ${fallback.join(', ')}.`] : []),
    ].join(' ') || 'All long-context rates are configured.';
    const invalidText = invalid.length ? `Enter a finite price of 0 or greater, or leave blank: ${invalid.join(', ')}.` : '';
    $('#pricing-standard-status').textContent = standardText;
    $('#pricing-long-status').textContent = longText;
    $('#pricing-invalid-status').textContent = invalidText;
    $('#pricing-invalid-status').hidden = !invalid.length;
    statusEl.dataset.state = state;
    statusEl.classList.toggle('configured', state === 'configured');
    statusEl.classList.toggle('partial', state === 'partial' || state === 'invalid');
    statusEl.innerHTML = `<i data-lucide="${icons[state]}" aria-hidden="true"></i> ${labels[state]}`;
    statusEl.title = [standardText, longText, invalidText].filter(Boolean).join(' ');
    refreshIcons();
  }

  function resetProfileForm() {
    if (state.saving) return;
    state.editingProfileId = null;
    $('#editing-profile-id').value = '';
    $('#profile-form-title').textContent = 'Add new profile';
    $('#profile-name').value = '';
    $('#azure-endpoint').value = '';
    $('#azure-api-key').value = '';
    $('#azure-api-key').type = 'password';
    $('#azure-api-key').placeholder = 'Enter your Azure OpenAI API key';
    const hint = $('#api-key-hint');
    if (hint) hint.textContent = 'API key is required for new connection profiles.';
    $('#azure-deployment').value = '';
    $('#azure-input-price').value = '';
    $('#azure-output-price').value = '';
    $('#azure-long-threshold').value = '';
    $('#azure-long-input-price').value = '';
    $('#azure-long-output-price').value = '';
    selectCachePrices(null);
    profileContext.select(null);
    updateThresholdLabels();
    updatePricingStatus();
    $('#delete-profile-btn').classList.add('hidden');
    const toggleBtn = $('#toggle-api-key-btn');
    if (toggleBtn) toggleBtn.innerHTML = '<i data-lucide="eye"></i>';
    document.querySelectorAll('.profile-item').forEach((item) => item.classList.remove('editing'));
    refreshIcons();
    state.formBaseline = snapshotProfileForm();
    setStatus('');
  }

  function selectProfileForEditing(profile) {
    if (state.saving) return;
    state.editingProfileId = profile.id;
    $('#editing-profile-id').value = profile.id;
    $('#profile-form-title').textContent = `Edit profile: ${profile.name}`;
    $('#profile-name').value = profile.name;
    $('#azure-endpoint').value = profile.endpoint;
    $('#azure-api-key').value = '';
    $('#azure-api-key').type = 'password';
    $('#azure-api-key').placeholder = 'Leave blank to keep existing key';
    const hint = $('#api-key-hint');
    if (hint) hint.textContent = 'Optional. Leave blank to keep current key, or enter a new key to update.';
    $('#azure-deployment').value = profile.deployment;
    $('#azure-input-price').value = profile.input_price_per_million !== null && profile.input_price_per_million !== undefined ? profile.input_price_per_million : '';
    $('#azure-output-price').value = profile.output_price_per_million !== null && profile.output_price_per_million !== undefined ? profile.output_price_per_million : '';
    $('#azure-long-threshold').value = profile.long_context_threshold !== null && profile.long_context_threshold !== undefined ? profile.long_context_threshold : '';
    $('#azure-long-input-price').value = profile.long_input_price_per_million !== null && profile.long_input_price_per_million !== undefined ? profile.long_input_price_per_million : '';
    $('#azure-long-output-price').value = profile.long_output_price_per_million !== null && profile.long_output_price_per_million !== undefined ? profile.long_output_price_per_million : '';
    selectCachePrices(profile);
    profileContext.select(profile);
    updateThresholdLabels();
    updatePricingStatus();
    $('#delete-profile-btn').classList.remove('hidden');
    const toggleBtn = $('#toggle-api-key-btn');
    if (toggleBtn) toggleBtn.innerHTML = '<i data-lucide="eye"></i>';
    document.querySelectorAll('.profile-item').forEach((item) => {
      item.classList.toggle('editing', item.dataset.profileId === profile.id);
    });
    refreshIcons();
    state.formBaseline = snapshotProfileForm();
    setStatus('');
  }

  function renderProfiles(data) {
    state.activeProfileId = data.active_profile_id;
    state.profiles = data.profiles || [];
    const activeProfile = state.profiles.find((p) => p.id === state.activeProfileId);

    const select = $('#profile-select');
    if (select) {
      select.innerHTML = '';
      if (!state.profiles.length) {
        const opt = document.createElement('option');
        opt.textContent = 'No profiles configured';
        select.append(opt);
      } else {
        state.profiles.forEach((p) => {
          const opt = document.createElement('option');
          opt.value = p.id;
          opt.textContent = `${p.name} (${p.deployment || 'No model'})`;
          opt.selected = p.id === state.activeProfileId;
          select.append(opt);
        });
      }
    }

    const labelEl = $('#model-name-label');
    if (labelEl) {
      if (activeProfile) {
        labelEl.textContent = activeProfile.name;
      } else if (state.profiles.length) {
        labelEl.textContent = state.profiles[0].name;
      } else {
        labelEl.textContent = 'No model';
      }
    }

    const popoverList = $('#model-profile-list');
    if (popoverList) {
      popoverList.innerHTML = '';
      if (!state.profiles.length) {
        const empty = document.createElement('div');
        empty.className = 'model-profile-empty';
        empty.textContent = 'No profiles configured. Open Settings to add one.';
        popoverList.append(empty);
      } else {
        state.profiles.forEach((p) => {
          const item = document.createElement('button');
          item.type = 'button';
          item.className = `model-profile-item ${p.id === state.activeProfileId ? 'active' : ''}`;
          item.setAttribute('role', 'option');
          item.setAttribute('aria-selected', String(p.id === state.activeProfileId));
          item.title = `Switch to ${p.name} (${p.deployment || 'No deployment'})`;
          item.innerHTML = `
            <span class="model-item-check"><i data-lucide="check"></i></span>
            <span class="model-item-info">
              <span class="model-item-name">${escapeHtml(p.name)}</span>
              <span class="model-item-deployment">${escapeHtml(p.deployment || 'No deployment')}</span>
            </span>
          `;
          item.onclick = async () => {
            const picker = $('#model-picker');
            if (picker) picker.open = false;
            if (p.id !== state.activeProfileId) {
              await switchActiveProfile(p.id);
            }
          };
          popoverList.append(item);
        });
      }
    }
    const container = $('#profile-list-container');
    const emptyState = $('#profile-empty-state');
    container.innerHTML = '';
    if (!state.profiles.length) {
      if (emptyState) emptyState.classList.remove('hidden');
    } else {
      if (emptyState) emptyState.classList.add('hidden');
      state.profiles.forEach((p) => {
        const item = document.createElement('div');
        item.className = `profile-item ${p.id === state.activeProfileId ? 'active' : ''} ${p.id === state.editingProfileId ? 'editing' : ''}`;
        item.dataset.profileId = p.id;
        item.title = 'Click to edit profile';
        item.onclick = (e) => {
          if (!e.target.closest('button') && canDiscardProfileChanges()) selectProfileForEditing(p);
        };
        const info = document.createElement('div');
        info.className = 'profile-info';
        info.innerHTML = `<strong>${escapeHtml(p.name)}</strong><span class="profile-meta" title="${escapeHtml(p.endpoint)}">${escapeHtml(p.deployment)} · ${escapeHtml(p.endpoint)}</span>`;
        const actions = document.createElement('div');
        actions.className = 'profile-actions';
        if (p.id !== state.activeProfileId) {
          const useBtn = document.createElement('button');
          useBtn.className = 'quiet-btn';
          useBtn.type = 'button';
          useBtn.textContent = 'Use';
          useBtn.title = 'Set as active profile';
          useBtn.onclick = (e) => {
            e.stopPropagation();
            switchActiveProfile(p.id);
          };
          actions.append(useBtn);
        } else {
          const activeBadge = document.createElement('span');
          activeBadge.className = 'active-badge';
          activeBadge.textContent = 'Active';
          actions.append(activeBadge);
        }
        item.append(info, actions);
        container.append(item);
      });
    }
    refreshIcons();
  }

  async function loadProfiles() {
    try {
      const data = await fetch('/api/profiles').then((res) => res.json());
      renderProfiles(data);
      health();
    } catch {
      toast('Could not load connection profiles.', 'error');
    }
  }

  async function switchActiveProfile(profileId) {
    if (isGenerating()) return;
    try {
      const data = await fetch(`/api/profiles/${profileId}/active`, { method: 'POST' }).then((res) => res.json());
      renderProfiles(data);
      health();
      toast('Active profile updated.', 'success');
    } catch {
      toast('Could not switch active profile.', 'error');
    }
  }

  async function health() {
    const el = $('#connection-status');
    try {
      const response = await fetch('/api/health');
      if (!response.ok) throw new Error('Unavailable');
      const data = await response.json();
      el.className = `status ${data.configured ? 'hidden' : 'error'}`;
      el.textContent = data.configured ? '' : 'Add a model in Settings to start chatting.';
      const modelTitle = data.configured ? 'Model for the next response' : 'No configured model';
      if ($('#profile-select')) $('#profile-select').title = modelTitle;
      if ($('#model-trigger')) $('#model-trigger').title = modelTitle;
    } catch {
      el.className = 'status error';
      el.textContent = 'Helios is offline. Check the local server.';
    }
  }

  async function openSettingsDialog(panelName = 'general') {
    closeMobileSidebar();
    await loadProfiles();
    const accordion = $('#profile-pricing-accordion');
    if (accordion) accordion.open = false;
    if (state.profiles.length) {
      const active = state.profiles.find((p) => p.id === state.activeProfileId) || state.profiles[0];
      selectProfileForEditing(active);
    } else {
      resetProfileForm();
    }
    $('#settings-dialog').showModal();
    selectSettingsPanel(panelName, true);
  }

  function updateThresholdLabels() {
    const inputEl = $('#azure-long-threshold');
    if (!inputEl) return;
    const raw = inputEl.value.trim();
    const val = parseInt(raw, 10);
    let display = '128K';
    if (!isNaN(val) && val > 0) {
      display = (val >= 1000 && val % 1000 === 0) ? `${val / 1000}K` : val.toLocaleString();
    }
    const stdLabel = $('#standard-range-label');
    const longLabel = $('#long-range-label');
    if (stdLabel) stdLabel.textContent = `< ${display} tokens`;
    if (longLabel) longLabel.textContent = `≥ ${display} tokens`;
  }

  function setupPricingAccordionAnimation() {
    const el = $('#profile-pricing-accordion');
    if (!el) return;
    const summary = el.querySelector('summary');
    const body = el.querySelector('.pricing-accordion-body');
    if (!summary || !body) return;
    const thresholdInput = $('#azure-long-threshold');
    if (thresholdInput) {
      thresholdInput.addEventListener('input', updateThresholdLabels);
    }
    let isAnimating = false;
    summary.addEventListener('click', (e) => {
      e.preventDefault();
      if (isAnimating) return;
      if (el.open) {
        isAnimating = true;
        const startHeight = `${el.offsetHeight}px`;
        const endHeight = `${summary.offsetHeight}px`;
        el.style.overflow = 'hidden';
        const anim = el.animate(
          { height: [startHeight, endHeight] },
          { duration: 200, easing: 'cubic-bezier(0.2, 0, 0, 1)' }
        );
        body.animate({ opacity: [1, 0] }, { duration: 150 });
        anim.onfinish = () => {
          el.open = false;
          el.style.height = '';
          el.style.overflow = '';
          isAnimating = false;
        };
      } else {
        el.open = true;
        isAnimating = true;
        const startHeight = `${summary.offsetHeight}px`;
        const endHeight = `${el.offsetHeight}px`;
        el.style.overflow = 'hidden';
        const anim = el.animate(
          { height: [startHeight, endHeight] },
          { duration: 220, easing: 'cubic-bezier(0, 0, 0.2, 1)' }
        );
        body.animate({ opacity: [0, 1] }, { duration: 180, delay: 20 });
        anim.onfinish = () => {
          el.style.height = '';
          el.style.overflow = '';
          isAnimating = false;
        };
      }
    });
  }

  function setGenerating(value) {
    const select = $('#profile-select');
    if (select) select.disabled = value;
    const modelPicker = $('#model-picker');
    if (modelPicker) {
      if (value) modelPicker.open = false;
      modelPicker.inert = value;
    }
  }

  function init() {
    profileContext.init();
    for (const [id] of CACHE_PRICE_FIELDS) {
      $(`#${id}`).addEventListener('input', validateCachePrices);
    }
    for (const [, standardId, longId] of PRICE_CATEGORIES) {
      for (const id of [standardId, longId]) {
        $(`#${id}`).addEventListener('input', updatePricingStatus);
      }
    }
    const modelPicker = $('#model-picker');
    if (modelPicker) {
      document.addEventListener('click', (event) => {
        if (!modelPicker.contains(event.target)) modelPicker.open = false;
      });
      modelPicker.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
          modelPicker.open = false;
          const trigger = modelPicker.querySelector('summary');
          if (trigger) trigger.focus();
          event.stopPropagation();
        }
      });
    }

    $('#open-settings').onclick = () => openSettingsDialog();
    const modelSettingsBtn = $('#model-popover-settings');
    if (modelSettingsBtn) {
      modelSettingsBtn.onclick = () => {
        if (modelPicker) modelPicker.open = false;
        openSettingsDialog('connections');
      };
    }
    $('#add-profile-btn').onclick = () => { if (canDiscardProfileChanges()) resetProfileForm(); };
    $('#discard-profile-edit').onclick = () => {
      if (state.saving) return;
      const profile = state.profiles.find(item => item.id === state.editingProfileId);
      if (profile) selectProfileForEditing(profile);
      else resetProfileForm();
      $('#settings-connections-tab').focus({ preventScroll: true });
    };
    $('#azure-settings-form').addEventListener('input', () => setStatus(''));

    const toggleKeyBtn = $('#toggle-api-key-btn');
    if (toggleKeyBtn) {
      toggleKeyBtn.onclick = () => {
        const input = $('#azure-api-key');
        const isPassword = input.type === 'password';
        input.type = isPassword ? 'text' : 'password';
        toggleKeyBtn.innerHTML = `<i data-lucide="${isPassword ? 'eye-off' : 'eye'}"></i>`;
        refreshIcons();
      };
    }

    $('#azure-settings-form').onsubmit = async (event) => {
      event.preventDefault();
      if (state.saving || !hasUnsavedChanges()) return;
      profileContext.validate();
      validateCachePrices();
      const invalid = [...$('#azure-settings-form').querySelectorAll('input, select')].find(input => !input.checkValidity());
      if (invalid) {
        const disclosure = invalid.closest('details');
        if (disclosure) disclosure.open = true;
        invalid.reportValidity();
        setStatus(invalid.validationMessage || 'Review the highlighted fields before saving.', 'error');
        return;
      }
      const editingId = $('#editing-profile-id').value;
      const name = $('#profile-name').value.trim();
      const endpoint = $('#azure-endpoint').value.trim();
      const api_key = $('#azure-api-key').value.trim();
      const deployment = $('#azure-deployment').value.trim();
      if (!name) return setStatus('Profile name is required.', 'error');
      if (!endpoint) return setStatus('Azure endpoint is required.', 'error');
      if (!editingId && !api_key) {
        $('#azure-api-key').focus();
        return setStatus('API key is required when creating a new profile.', 'error');
      }
      if (!deployment) return setStatus('Deployment name is required.', 'error');
      const payload = { name, endpoint, deployment, ...profileContext.changes(Boolean(editingId)), ...cachePriceChanges(Boolean(editingId)) };
      if (api_key) payload.api_key = api_key;
      const inPriceVal = $('#azure-input-price').value.trim();
      const outPriceVal = $('#azure-output-price').value.trim();
      if (inPriceVal !== '') {
        const parsedIn = parseFloat(inPriceVal);
        if (!isNaN(parsedIn) && parsedIn >= 0) payload.input_price_per_million = parsedIn;
      } else if (editingId) {
        payload.clear_input_price = true;
      }
      if (outPriceVal !== '') {
        const parsedOut = parseFloat(outPriceVal);
        if (!isNaN(parsedOut) && parsedOut >= 0) payload.output_price_per_million = parsedOut;
      } else if (editingId) {
        payload.clear_output_price = true;
      }
      const thresholdVal = $('#azure-long-threshold').value.trim();
      const longInVal = $('#azure-long-input-price').value.trim();
      const longOutVal = $('#azure-long-output-price').value.trim();
      if (thresholdVal !== '') {
        const parsedTh = parseInt(thresholdVal, 10);
        if (!isNaN(parsedTh) && parsedTh > 0) payload.long_context_threshold = parsedTh;
      }
      if (longInVal !== '') {
        const parsedLongIn = parseFloat(longInVal);
        if (!isNaN(parsedLongIn) && parsedLongIn >= 0) payload.long_input_price_per_million = parsedLongIn;
      } else if (editingId) {
        payload.clear_long_input_price = true;
      }
      if (longOutVal !== '') {
        const parsedLongOut = parseFloat(longOutVal);
        if (!isNaN(parsedLongOut) && parsedLongOut >= 0) payload.long_output_price_per_million = parsedLongOut;
      } else if (editingId) {
        payload.clear_long_output_price = true;
      }
      const url = editingId ? `/api/profiles/${editingId}` : '/api/profiles';
      const method = editingId ? 'PUT' : 'POST';
      setStatus('');
      setSaving(true);
      try {
        const response = await fetch(url, {
          method,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          const err = await response.json();
          const feedback = describeRequestError(err, 'Could not save profile.', `HTTP ${response.status}`);
          throw new Error([feedback.message, ...feedback.details].join('\n'));
        }
        const data = await response.json();
        $('#azure-api-key').value = '';
        renderProfiles(data);
        health();
        setSaving(false);
        if (!editingId) {
          resetProfileForm();
          setStatus('Profile created.', 'success');
        } else {
          const updated = (data.profiles || []).find((p) => p.id === editingId);
          if (updated) selectProfileForEditing(updated);
          setStatus('Changes saved.', 'success');
        }
      } catch (err) {
        setStatus(`${err.message || 'Could not confirm the save.'}\nYour edits are kept.`, 'error');
      } finally {
        setSaving(false);
      }
    };

    $('#delete-profile-btn').onclick = async () => {
      const editingId = $('#editing-profile-id').value;
      if (!editingId) return;
      if (!confirm('Are you sure you want to delete this profile?')) return;
      try {
        const response = await fetch(`/api/profiles/${editingId}`, { method: 'DELETE' });
        if (!response.ok) throw new Error('Could not delete profile.');
        const data = await response.json();
        renderProfiles(data);
        health();
        resetProfileForm();
        setStatus('Profile deleted.', 'success');
      } catch (err) {
        setStatus(err.message || 'Could not delete profile.', 'error');
      }
    };

    $('#profile-select').onchange = (event) => switchActiveProfile(event.target.value);

    $('#settings-dialog').addEventListener('close', () => {
      resetProfileForm();
      const accordion = $('#profile-pricing-accordion');
      if (accordion) accordion.open = false;
    });

    setupPricingAccordionAnimation();
  }

  return { init, load: loadProfiles, checkHealth: health, setGenerating, hasUnsavedChanges, isSaving: () => state.saving, getActiveProfileId: () => state.activeProfileId };
}
