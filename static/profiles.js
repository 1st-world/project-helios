/* Own connection profiles, model selection, and credential, context, and pricing editing. */

import { $, closeMobileSidebar, escapeHtml, nameInText, refreshIcons, toast } from './ui.js';
import { selectSettingsPanel } from './settings.js';
import { createProfileContext } from './profile-context.js';
import { describeRequestError } from './chat-feedback.js';
import { showActionDialog } from './dialogs.js';
import { createModelSelection } from './model-selection.js';

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

export function createProfiles({ isGenerating, onModelContextChange = () => {}, onSelectionChange = () => {} }) {
  const state = { defaultProfileId: null, editingProfileId: null, profiles: [], formBaseline: null, saving: false, defaultSaving: false, feedback: null, catalogLoaded: false, catalogError: '', online: true };
  const profileContext = createProfileContext({ isSaving: () => state.saving });
  const modelSelection = createModelSelection({ onChange: renderModelSelection });
  let catalogPromise = null;
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
    status.textContent = state.feedback?.message || (state.saving ? 'Saving changes...' : dirty ? 'Unsaved changes' : '');
    status.dataset.state = state.feedback?.type || (state.saving ? 'info' : dirty ? 'dirty' : 'idle');
    $('#save-profile-btn').disabled = state.saving || state.defaultSaving || !dirty;
    $('#delete-profile-btn').disabled = state.saving || state.defaultSaving;
    $('#discard-profile-edit').disabled = state.saving || !dirty;
    $('#save-profile-label').textContent = state.saving ? 'Saving...' : 'Save changes';
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

    $('#model-selection-status').textContent = model.error || state.catalogError || (model.saving ? 'Saving model selection...'
      : !model.ready ? 'Loading saved model selection...'
      : !activeProfile ? model.profileId ? 'The saved model is unavailable. Choose another model.' : 'Choose a model for this conversation.'
      : pausedRun ? 'Choose a model to continue this run.' : 'Choose a model for the next response.');
    $('#reload-model-selection').hidden = model.ready && !model.error && !state.catalogError;
    $('#reload-model-selection').disabled = model.saving;

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
    status.textContent = message;
    const loading = state.online && !state.catalogError && !model.error && (!state.catalogLoaded || !model.ready);
    status.className = `status ${message ? loading ? '' : 'error' : 'hidden'}`;
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

  async function loadProfiles() {
    if (catalogPromise) return catalogPromise;
    catalogPromise = (async () => {
      try {
        const data = await readProfileResponse(await fetch('/api/profiles', { cache: 'no-store', signal: AbortSignal.timeout(12000) }), 'Could not load connection profiles.');
        renderProfiles(data);
        health();
        return true;
      } catch {
        state.catalogError = 'Could not load model profiles. Open the model picker to reload them.';
        renderModelSelection();
        toast('Could not load connection profiles.', 'error');
        return false;
      } finally { catalogPromise = null; }
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
    if (!state.catalogLoaded && !await loadProfiles()) throw new Error('Reload model profiles before sending.');
    if (state.catalogError) throw new Error('Reload model profiles before sending.');
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

  function updateThresholdLabels() {
    const inputEl = $('#azure-long-threshold');
    if (!inputEl) return;
    inputEl.setCustomValidity('');
    const val = readThreshold();
    let display = '—';
    if (Number.isSafeInteger(val) && val > 0) {
      display = (val >= 1000 && val % 1000 === 0) ? `${val / 1000}K` : val.toLocaleString();
    }
    const stdLabel = $('#standard-range-label');
    const longLabel = $('#long-range-label');
    if (stdLabel) stdLabel.textContent = `≤ ${display} tokens`;
    if (longLabel) longLabel.textContent = `> ${display} tokens`;
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
    $('#add-profile-btn').onclick = async () => { if (await canDiscardProfileChanges()) resetProfileForm(); };
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
        setKeyVisibility($('#azure-api-key').type === 'password');
        refreshIcons();
      };
    }

    $('#azure-settings-form').onsubmit = async (event) => {
      event.preventDefault();
      if (state.saving || state.defaultSaving || !hasUnsavedChanges()) return;
      profileContext.validate();
      validateCachePrices();
      const thresholdInput = $('#azure-long-threshold');
      const threshold = readThreshold();
      thresholdInput.setCustomValidity(Number.isSafeInteger(threshold) && threshold > 0
        ? '' : 'Enter a positive whole number the browser can represent exactly, or leave blank for the default breakpoint.');
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
    $('#reload-model-selection').onclick = () => refreshModelSelection(modelSelection.snapshot().conversationId);

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
