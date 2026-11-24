/* Own connection profiles, model selection, and credential, context, and pricing editing. */

import { $, closeMobileSidebar, escapeHtml, nameInText, refreshIcons, setRefreshBusy, setStatusMessage, setText, toast } from './ui.js';
import { selectSettingsPanel } from './settings.js';
import { createProfileContext } from './profile-context.js';
import { describeRequestError } from './chat-feedback.js';
import { showActionDialog } from './dialogs.js';
import { createModelSelection } from './model-selection.js';
import { createFieldValidation } from './field-validation.js';

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
const pricingFieldList = new Intl.ListFormat('en');

function readPrice(input) {
  const invalid = input.validity.badInput || (input.value !== '' && (!Number.isFinite(input.valueAsNumber) || input.valueAsNumber < 0));
  return { value: input.value === '' || invalid ? null : input.valueAsNumber, invalid };
}

function isValidThreshold(value) {
  return Number.isSafeInteger(value) && value > 0;
}

export function createProfiles({ isGenerating, onModelContextChange = () => {}, onSelectionChange = () => {} }) {
  const state = { defaultProfileId: null, editingProfileId: null, profiles: [], formBaseline: null, saving: false, defaultSaving: false, feedback: null, catalogLoaded: false, catalogLoading: false, catalogError: '', online: true };
  const form = $('#azure-settings-form');
  const validation = createFieldValidation(form, updateValidity);
  const profileContext = createProfileContext({ isSaving: () => state.saving });
  const modelSelection = createModelSelection({ onChange: renderModelSelection });
  let catalogPromise = null;
  let reloadingSelection = null;
  let modelContextSignature = '';
  let cachePriceBaseline = {};
  let pausedRun = null;

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

  function validatePrices() {
    for (const id of PRICE_CATEGORIES.flatMap(([, standardId, longId]) => [standardId, longId])) {
      const input = $(`#${id}`);
      input.setCustomValidity(readPrice(input).invalid ? 'Enter a price of 0 or greater, or leave blank.' : '');
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

  async function canDiscardProfileChanges() {
    return !state.saving && (!hasUnsavedChanges() || await showActionDialog({ title: 'Discard profile changes?',
      description: 'Your unsaved edits to this profile will be lost.', confirmLabel: 'Discard changes', cancelLabel: 'Keep editing', danger: true }));
  }

  function setStatus(message, type = 'info') {
    state.feedback = message ? { message, type } : null;
    updateEditorControls();
  }

  function updateEditorControls() {
    const dirty = hasUnsavedChanges();
    const status = $('#profile-settings-status');
    setStatusMessage(status, state.feedback?.message || '', state.feedback?.type);
    status.dataset.state = state.feedback?.type || 'idle';
    $('#save-profile-btn').disabled = state.saving || state.defaultSaving || !dirty;
    $('#delete-profile-btn').disabled = state.saving || state.defaultSaving;
    $('#discard-profile-edit').disabled = state.saving || !dirty;
    $('#discard-profile-edit').title = state.editingProfileId
      ? 'Restore the last saved values for this profile'
      : 'Restore the new profile form';
    $('#save-profile-label').textContent = state.saving ? 'Saving...' : 'Save changes';
    $('#save-profile-btn').setAttribute('aria-busy', String(state.saving));
    $('#save-profile-btn').setAttribute('aria-label', state.saving ? 'Saving changes' : 'Save changes');
    renderCatalogFeedback();
  }

  function setSaving(saving) {
    state.saving = saving;
    $('#azure-settings-form').querySelectorAll('input, select, button').forEach(control => { control.disabled = saving; });
    profileContext.updateControls();
    updateEditorControls();
    $('#profile-list-container').querySelectorAll('[data-set-default]').forEach(button => { button.disabled = saving || state.defaultSaving; });
    $('#settings-dialog').dispatchEvent(new Event('profile-save-state-change'));
  }

  function updatePricingStatus() {
    const statusEl = $('#pricing-accordion-status');
    const rates = PRICE_CATEGORIES.map(([name, standardId, longId]) => ({
      name, standard: readPrice($(`#${standardId}`)), long: readPrice($(`#${longId}`)),
    }));
    const invalid = rates.some(rate => rate.standard.invalid || rate.long.invalid) || !isValidThreshold(readThreshold());
    const missingStandard = rates.filter(rate => rate.standard.value === null).map(rate => rate.name);
    const hasPricing = rates.some(rate => rate.standard.value !== null || rate.long.value !== null);
    // Valid blank long rates use the corresponding standard rates.
    const pricingState = invalid ? 'invalid' : !hasPricing ? 'empty' : missingStandard.length ? 'partial' : 'configured';
    const message = pricingState === 'partial' ? `Missing standard rates for ${pricingFieldList.format(missingStandard)}.` : '';
    const completeness = $('#pricing-completeness-status');
    setText(completeness, message);
    completeness.hidden = !message;
    if (statusEl.dataset.state !== pricingState) {
      const labels = { empty: 'Not configured', partial: 'Partial pricing', configured: 'Configured', invalid: 'Check pricing' };
      const icons = { empty: 'circle-dashed', partial: 'triangle-alert', configured: 'check', invalid: 'circle-alert' };
      statusEl.dataset.state = pricingState;
      statusEl.innerHTML = `<i data-lucide="${icons[pricingState]}" aria-hidden="true"></i> ${labels[pricingState]}`;
      refreshIcons();
    }
  }

  function setKeyVisibility(visible) {
    $('#azure-api-key').type = visible ? 'text' : 'password';
    const button = $('#toggle-api-key-btn');
    const label = visible ? 'Hide API key' : 'Show API key';
    button.title = label;
    button.setAttribute('aria-label', label);
    button.innerHTML = `<i data-lucide="${visible ? 'eye-off' : 'eye'}" aria-hidden="true"></i>`;
  }

  function resetProfileForm() {
    if (state.saving) return;
    state.editingProfileId = null;
    validation.reset();
    $('#editing-profile-id').value = '';
    $('#profile-form-title').textContent = 'Add new profile';
    $('#profile-name').value = '';
    $('#azure-endpoint').value = '';
    $('#azure-api-key').value = '';
    setKeyVisibility(false);
    $('#azure-api-key').placeholder = 'Enter an API key';
    const hint = $('#api-key-hint');
    if (hint) hint.textContent = 'An API key is required for a new profile.';
    $('#azure-deployment').value = '';
    $('#profile-reasoning-summary').value = 'off';
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
    document.querySelectorAll('.profile-item').forEach((item) => {
      item.classList.remove('editing');
      item.querySelector('.profile-info').setAttribute('aria-pressed', 'false');
    });
    refreshIcons();
    state.formBaseline = snapshotProfileForm();
    setStatus('');
  }

  function selectProfileForEditing(profile) {
    if (state.saving) return;
    validation.reset();
    state.editingProfileId = profile.id;
    $('#editing-profile-id').value = profile.id;
    $('#profile-form-title').textContent = `Edit profile: ${profile.name}`;
    $('#profile-name').value = profile.name;
    $('#azure-endpoint').value = profile.endpoint;
    $('#azure-api-key').value = '';
    setKeyVisibility(false);
    $('#azure-api-key').placeholder = 'New key (optional)';
    const hint = $('#api-key-hint');
    if (hint) hint.textContent = 'Blank keeps the saved key; a new value replaces it.';
    $('#azure-deployment').value = profile.deployment;
    $('#profile-reasoning-summary').value = profile.reasoning_summary || 'off';
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
    document.querySelectorAll('.profile-item').forEach((item) => {
      const editing = item.dataset.profileId === profile.id;
      item.classList.toggle('editing', editing);
      item.querySelector('.profile-info').setAttribute('aria-pressed', String(editing));
    });
    refreshIcons();
    state.formBaseline = snapshotProfileForm();
    setStatus('');
  }

  function renderModelSelection() {
    const model = modelSelection.snapshot();
    const activeProfile = model.available === false ? undefined : state.profiles.find(profile => profile.id === model.profileId);
    const select = $('#profile-select');
    if (select) {
      select.innerHTML = '';
      if (!activeProfile) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = model.profileId ? 'Unavailable model' : 'Select model';
        opt.selected = true;
        opt.disabled = true;
        select.append(opt);
      }
      state.profiles.forEach((p) => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = `${p.name} (${p.deployment || 'No model'})`;
        opt.selected = p.id === activeProfile?.id;
        select.append(opt);
      });
    }

    const labelEl = $('#model-name-label');
    if (labelEl) {
      if (model.saving) {
        labelEl.textContent = 'Saving model...';
      } else if (!state.catalogLoaded || !model.ready) {
        labelEl.textContent = model.error || state.catalogError ? 'Check model' : 'Loading model...';
      } else if (activeProfile) {
        labelEl.textContent = activeProfile.name;
      } else {
        labelEl.textContent = model.profileId ? 'Unavailable model' : 'Select model';
      }
      $('#model-trigger').setAttribute('aria-busy', String(model.saving));
      $('#model-trigger').title = model.saving ? 'Saving the model selection for this conversation.' : activeProfile
        ? `${pausedRun ? `Continue paused work with ${nameInText(activeProfile.name)}` : `Next response model: ${activeProfile.name}`} (${activeProfile.deployment || 'No deployment'})`
        : model.profileId ? 'The saved model profile is unavailable. Choose another model for this conversation.' : 'Select a model for this conversation';
      if (select) select.title = $('#model-trigger').title;
    }

    const reload = $('#reload-model-selection');
    const reloading = reloadingSelection?.epoch === model.epoch;
    const selectionLoadFailed = !model.ready && Boolean(model.error);
    reload.hidden = !reloading && !selectionLoadFailed && !state.catalogError;
    setRefreshBusy(reload, reloading, reloading || model.saving);

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
          item.className = `model-profile-item ${p.id === activeProfile?.id ? 'active' : ''}`;
          item.disabled = model.saving || !model.ready;
          item.setAttribute('role', 'option');
          item.setAttribute('aria-selected', String(p.id === activeProfile?.id));
          item.title = `Use ${nameInText(p.name)} (${p.deployment || 'No deployment'}) for this conversation`;
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
            if (isGenerating()) return;
            try {
              if (await modelSelection.select(p.id)) toast('Model selected for this conversation.', 'success');
            } catch (error) { toast(error.message, 'warning'); }
          };
          popoverList.append(item);
        });
      }
    }
    const status = $('#connection-status');
    const message = !state.online ? 'Helios is offline. Check the local server.'
      : model.saving ? '' : state.catalogError || model.error || (!state.catalogLoaded || !model.ready ? 'Loading model selection...'
      : !activeProfile ? model.profileId ? 'The selected profile is unavailable. Choose another model for the next response.'
      : state.profiles.length ? 'Select a model for this conversation.' : 'Add a model in Settings to start chatting.'
      : !activeProfile.is_configured ? 'The selected profile needs connection details in Settings.' : '');
    const loading = state.online && !state.catalogError && !model.error && (!state.catalogLoaded || !model.ready);
    const failed = !state.online || state.catalogError || model.error;
    const needsAttention = activeProfile ? !activeProfile.is_configured : Boolean(model.profileId);
    setStatusMessage(status, message, loading ? 'running' : failed ? 'error' : needsAttention ? 'warning' : null);
    status.hidden = !message;
    const signature = JSON.stringify([model.profileId, activeProfile && Object.fromEntries(Object.entries(activeProfile).filter(([key]) => key !== 'name'))]);
    if (signature !== modelContextSignature) {
      modelContextSignature = signature;
      onModelContextChange();
    }
    refreshIcons();
    onSelectionChange();
  }

  function renderProfiles(data) {
    state.defaultProfileId = data.active_profile_id;
    state.profiles = data.profiles || [];
    state.catalogLoaded = true;
    state.catalogError = '';
    renderCatalogFeedback();
    modelSelection.setDefault(state.defaultProfileId);
    const container = $('#profile-list-container');
    const emptyState = $('#profile-empty-state');
    container.innerHTML = '';
    if (!state.profiles.length) {
      if (emptyState) emptyState.classList.remove('hidden');
    } else {
      if (emptyState) emptyState.classList.add('hidden');
      state.profiles.forEach((p) => {
        const item = document.createElement('div');
        item.className = `profile-item ${p.id === state.defaultProfileId ? 'active' : ''} ${p.id === state.editingProfileId ? 'editing' : ''}`;
        item.dataset.profileId = p.id;
        const info = document.createElement('button');
        info.type = 'button';
        info.className = 'profile-info';
        info.title = `Edit profile: ${p.name}`;
        info.setAttribute('aria-label', `Edit profile: ${p.name}`);
        info.setAttribute('aria-pressed', String(p.id === state.editingProfileId));
        info.onclick = async () => { if (await canDiscardProfileChanges()) selectProfileForEditing(p); };
        info.innerHTML = `<strong>${escapeHtml(p.name)}</strong><span class="profile-meta" title="${escapeHtml(p.endpoint)}">${escapeHtml(p.deployment)} · ${escapeHtml(p.endpoint)}</span>`;
        const actions = document.createElement('div');
        actions.className = 'profile-actions';
        if (p.id !== state.defaultProfileId) {
          const useBtn = document.createElement('button');
          useBtn.className = 'quiet-btn';
          useBtn.type = 'button';
          useBtn.textContent = 'Set as default';
          useBtn.title = 'Use as the initial model for new chats';
          useBtn.dataset.setDefault = '';
          useBtn.disabled = state.saving || state.defaultSaving;
          useBtn.onclick = (e) => {
            e.stopPropagation();
            switchDefaultProfile(p.id);
          };
          actions.append(useBtn);
        } else {
          const defaultBadge = document.createElement('span');
          defaultBadge.className = 'default-profile-badge';
          defaultBadge.textContent = 'Default';
          actions.append(defaultBadge);
        }
        item.append(info, actions);
        container.append(item);
      });
    }
    refreshIcons();
  }

  async function readProfileResponse(response, fallback) {
    let data;
    try { data = await response.json(); }
    catch { throw new Error(`${fallback} (HTTP ${response.status})`); }
    if (!response.ok) {
      const feedback = describeRequestError(data, fallback, `HTTP ${response.status}`);
      throw new Error([feedback.message, ...feedback.details].join('\n'));
    }
    return data;
  }

  function renderCatalogFeedback() {
    $('#profile-catalog-feedback').hidden = !state.catalogError;
    const status = $('#profile-catalog-status');
    const message = state.catalogError ? state.catalogLoaded
      ? 'Could not load connection profiles. Showing the previous list.' : 'Could not load connection profiles.' : '';
    setStatusMessage(status, message, 'error');
    setRefreshBusy($('#reload-profile-catalog'), state.catalogLoading, state.catalogLoading || state.saving || state.defaultSaving);
  }

  async function loadProfiles() {
    if (catalogPromise) return catalogPromise;
    state.catalogLoading = true;
    renderCatalogFeedback();
    catalogPromise = (async () => {
      try {
        const data = await readProfileResponse(await fetch('/api/profiles', { cache: 'no-store', signal: AbortSignal.timeout(12000) }), 'Could not load connection profiles.');
        renderProfiles(data);
        health();
        return true;
      } catch {
        state.catalogError = 'Could not load model profiles.';
        renderCatalogFeedback();
        renderModelSelection();
        return false;
      } finally {
        catalogPromise = null;
        state.catalogLoading = false;
        renderCatalogFeedback();
      }
    })();
    return catalogPromise;
  }

  async function switchDefaultProfile(profileId) {
    if (state.saving || state.defaultSaving) return;
    state.defaultSaving = true;
    updateEditorControls();
    $('#profile-list-container').querySelectorAll('[data-set-default]').forEach(button => { button.disabled = true; });
    try {
      const data = await readProfileResponse(await fetch(`/api/profiles/${profileId}/active`, { method: 'POST', signal: AbortSignal.timeout(12000) }), 'Could not update the default profile.');
      renderProfiles(data);
      health();
      toast('Default profile updated for new chats.', 'success');
    } catch (error) {
      await loadProfiles();
      toast(error.message || 'Could not update the default profile.', 'error');
    } finally {
      state.defaultSaving = false;
      updateEditorControls();
      $('#profile-list-container').querySelectorAll('[data-set-default]').forEach(button => { button.disabled = state.saving; });
    }
  }

  async function health() {
    try {
      const response = await fetch('/api/health');
      if (!response.ok) throw new Error('Unavailable');
      state.online = true;
    } catch { state.online = false; }
    renderModelSelection();
  }

  async function prepareRequestProfile() {
    const context = modelSelection.snapshot().epoch;
    if (catalogPromise) await catalogPromise;
    if (!state.catalogLoaded) await loadProfiles();
    if (state.catalogError) throw new Error('Could not load model profiles. Choose Reload selection in the model picker.');
    if (context !== modelSelection.snapshot().epoch) throw new DOMException('Conversation changed while loading model profiles.', 'AbortError');
    const prepared = await modelSelection.prepare();
    const profile = state.profiles.find(item => item.id === prepared.profile_id);
    if (!profile || modelSelection.snapshot().available === false) throw new Error('The selected profile is unavailable. Choose another model for this conversation.');
    if (!profile.is_configured) throw new Error('The selected profile needs connection details in Settings.');
    return prepared;
  }

  async function refreshModelSelection(id) {
    await loadProfiles();
    return modelSelection.refresh(id);
  }

  function selectConversation(id, data) {
    modelSelection.selectConversation(id, data);
    if (state.catalogLoaded && (data.profile_available === false || data.profile_id && !state.profiles.some(profile => profile.id === data.profile_id))) loadProfiles();
  }

  async function openSettingsDialog(panelName) {
    const selectedPanel = panelName || $('#settings-dialog [data-settings-tab][aria-selected="true"]')?.dataset.settingsTab || 'general';
    closeMobileSidebar();
    await loadProfiles();
    if (state.profiles.length) {
      const active = state.profiles.find((p) => p.id === modelSelection.snapshot().profileId)
        || state.profiles.find((p) => p.id === state.defaultProfileId) || state.profiles[0];
      selectProfileForEditing(active);
    } else {
      resetProfileForm();
    }
    $('#settings-dialog').showModal();
    selectSettingsPanel(selectedPanel, true);
  }

  function readThreshold() {
    const input = $('#azure-long-threshold');
    return input.value === '' && !input.validity.badInput ? Number(input.placeholder) : input.valueAsNumber;
  }

  function updateValidity() {
    for (const input of form.querySelectorAll('input, select')) input.setCustomValidity('');
    profileContext.validate();
    validatePrices();
    const thresholdInput = $('#azure-long-threshold');
    const threshold = readThreshold();
    thresholdInput.setCustomValidity(isValidThreshold(threshold) ? ''
      : threshold > Number.MAX_SAFE_INTEGER ? `Enter a whole number no greater than ${Number.MAX_SAFE_INTEGER.toLocaleString()}, or leave blank for the default.`
      : 'Enter a whole number of at least 1, or leave blank for the default.');
    for (const [id, message] of [
      ['profile-name', 'Enter a profile name.'],
      ['azure-deployment', 'Enter a deployment name.'],
      ['azure-endpoint', 'Enter a Foundry endpoint URL.'],
      ...(!$('#editing-profile-id').value ? [['azure-api-key', 'Enter an API key for the new profile.']] : [])
    ]) {
      const input = $(`#${id}`);
      if (!input.value.trim()) input.setCustomValidity(message);
    }
    const endpoint = $('#azure-endpoint');
    if (endpoint.validity.typeMismatch) endpoint.setCustomValidity('Enter a valid Foundry endpoint URL, including https://.');
  }

  function updateThresholdLabels() {
    const threshold = readThreshold();
    const stdLabel = $('#standard-range-label');
    const longLabel = $('#long-range-label');
    const valid = isValidThreshold(threshold);
    stdLabel.hidden = !valid;
    longLabel.hidden = !valid;
    if (valid) {
      const display = threshold >= 1000 && threshold % 1000 === 0 ? `${threshold / 1000}K` : threshold.toLocaleString();
      setText(stdLabel, `≤ ${display} tokens`);
      setText(longLabel, `> ${display} tokens`);
    }
  }

  function setGenerating(value) {
    const select = $('#profile-select');
    if (select) select.disabled = value;
    const modelPicker = $('#model-picker');
    if (modelPicker) {
      if (value) modelPicker.open = false;
      const summary = modelPicker.querySelector('summary');
      summary.setAttribute('aria-disabled', String(value));
      summary.tabIndex = value ? -1 : 0;
    }
  }

  function setPausedRun(run) {
    pausedRun = run;
    renderModelSelection();
  }

  function getModelSelection() {
    const selected = modelSelection.snapshot();
    return { ...selected, profile: state.profiles.find(profile => profile.id === selected.profileId),
      signature: modelContextSignature, ready: selected.ready && state.catalogLoaded && !state.catalogError };
  }

  function init() {
    profileContext.init();
    $('#reload-profile-catalog').onclick = loadProfiles;
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
    $('#add-profile-btn').onclick = async () => { if (await canDiscardProfileChanges()) resetProfileForm(); };
    $('#discard-profile-edit').onclick = () => {
      if (state.saving) return;
      const profile = state.profiles.find(item => item.id === state.editingProfileId);
      if (profile) selectProfileForEditing(profile);
      else resetProfileForm();
      $('#settings-connections-tab').focus({ preventScroll: true });
    };
    form.addEventListener('input', event => {
      setStatus('');
      if (event.target.closest('#profile-pricing-accordion')) updatePricingStatus();
    });

    const toggleKeyBtn = $('#toggle-api-key-btn');
    if (toggleKeyBtn) {
      toggleKeyBtn.onclick = () => {
        setKeyVisibility($('#azure-api-key').type === 'password');
        refreshIcons();
      };
    }

    $('#azure-settings-form').onsubmit = async (event) => {
      event.preventDefault();
      if (state.saving || state.defaultSaving || !hasUnsavedChanges()) return;
      if (!validation.showErrors()) return;
      const threshold = readThreshold();
      const editingId = $('#editing-profile-id').value;
      const name = $('#profile-name').value.trim();
      const endpoint = $('#azure-endpoint').value.trim();
      const api_key = $('#azure-api-key').value.trim();
      const deployment = $('#azure-deployment').value.trim();
      const payload = { name, endpoint, deployment, ...profileContext.changes(Boolean(editingId)), ...cachePriceChanges(Boolean(editingId)) };
      payload.reasoning_summary = $('#profile-reasoning-summary').value;
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
      const longInVal = $('#azure-long-input-price').value.trim();
      const longOutVal = $('#azure-long-output-price').value.trim();
      payload.long_context_threshold = threshold;
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
        const data = await readProfileResponse(response, 'Could not save profile.');
        $('#azure-api-key').value = '';
        renderProfiles(data);
        health();
        setSaving(false);
        if (!editingId) {
          resetProfileForm();
          toast('Profile created.', 'success');
        } else {
          const updated = (data.profiles || []).find((p) => p.id === editingId);
          if (updated) selectProfileForEditing(updated);
          toast('Changes saved.', 'success');
        }
      } catch (err) {
        setStatus(`${err.message || 'Could not confirm the save.'}\nYour edits are kept.`, 'error');
      } finally {
        setSaving(false);
      }
    };

    $('#delete-profile-btn').onclick = async () => {
      const editingId = $('#editing-profile-id').value;
      if (!editingId || state.saving || state.defaultSaving) return;
      const name = state.profiles.find(profile => profile.id === editingId)?.name;
      const deleted = await showActionDialog({ title: 'Delete profile?',
        description: `Delete ${nameInText(name, 'this profile')}? Conversations using this profile will need another model selection. Paused runs using this connection cannot resume. This cannot be undone. Unsaved edits to this profile will also be lost.`,
        confirmLabel: 'Delete profile', danger: true,
        async onConfirm() {
          const response = await fetch(`/api/profiles/${editingId}`, { method: 'DELETE' });
          const data = await readProfileResponse(response, 'Could not delete profile. Please try again.');
          renderProfiles(data);
          health();
          resetProfileForm();
        }
      });
      if (deleted) toast('Profile deleted.', 'success');
    };

    $('#profile-select').onchange = async event => {
      try { await modelSelection.select(event.target.value); }
      catch (error) { toast(error.message, 'warning'); }
    };
    $('#reload-model-selection').onclick = async () => {
      const { conversationId, epoch } = modelSelection.snapshot();
      if (reloadingSelection?.epoch === epoch) return;
      const reload = { epoch };
      reloadingSelection = reload;
      renderModelSelection();
      try {
        await refreshModelSelection(conversationId);
      } finally {
        if (reloadingSelection === reload) {
          reloadingSelection = null;
          renderModelSelection();
        }
      }
    };

    $('#settings-dialog').addEventListener('close', () => {
      if ($('#settings-dialog').open) return;
      resetProfileForm();
    });

    $('#azure-long-threshold').addEventListener('input', updateThresholdLabels);
  }

  return { init, load: loadProfiles, checkHealth: health, setGenerating, hasUnsavedChanges, isSaving: () => state.saving || state.defaultSaving,
    getSelectedProfileId: () => modelSelection.snapshot().profileId, getModelContextSignature: () => modelContextSignature,
    prepareRequestProfile, refreshModelSelection, getModelSelection, setPausedRun, startDraft: modelSelection.startDraft,
    selectConversation, adoptConversation: modelSelection.adoptConversation,
    openSettings: openSettingsDialog };
}
