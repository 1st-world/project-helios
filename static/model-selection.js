/* Own one draft or conversation's model preference, guarding asynchronous saves and independent revisions. */

export function createModelSelection({ onChange = () => {}, request = (...args) => fetch(...args) } = {}) {
  let defaultId = null;
  let defaultsLoaded = false;
  let epoch = 0;
  let readVersion = 0;
  let pending = null;
  const state = { conversationId: null, profileId: null, available: null, revision: null, ready: false, saving: false, error: '' };
  const snapshot = () => ({ ...state, epoch });
  const changed = () => onChange(snapshot());

  function apply(data) {
    if (!Object.hasOwn(data, 'profile_id') || !Number.isSafeInteger(data.profile_revision) || data.profile_revision < 0) throw new Error('Could not verify the saved model selection. Reload it before continuing.');
    if (state.revision !== null && data.profile_revision < state.revision) return false;
    state.profileId = data.profile_id;
    state.available = data.profile_available !== false;
    state.revision = data.profile_revision;
    state.ready = true;
    state.error = '';
    return true;
  }

  async function read(response) {
    let data;
    try { data = await response.json(); }
    catch { throw new Error('Could not confirm the model selection. Reload it before continuing.'); }
    if (!response.ok) throw new Error(data?.detail?.message || data?.detail || 'Could not save the model selection.');
    return data;
  }

  function startDraft() {
    ++epoch;
    ++readVersion;
    pending = null;
    Object.assign(state, { conversationId: null, profileId: defaultId, available: null, revision: null, ready: defaultsLoaded, saving: false, error: '' });
    changed();
  }

  function setDefault(profileId) {
    defaultId = profileId;
    defaultsLoaded = true;
    if (state.conversationId === null && (!state.ready || state.profileId === null)) {
      state.profileId = defaultId;
      state.available = null;
      state.ready = true;
    }
    changed();
  }

  function selectConversation(id, data) {
    if (state.conversationId !== id) {
      ++epoch;
      ++readVersion;
      pending = null;
      Object.assign(state, { conversationId: id, profileId: null, available: null, revision: null, ready: false, saving: false, error: '' });
    }
    try { apply(data); }
    catch (error) { state.ready = false; state.error = error.message; }
    changed();
  }

  function adoptConversation(id, prepared) {
    if (state.conversationId !== null || prepared?.epoch !== epoch) return;
    ++epoch;
    ++readVersion;
    Object.assign(state, { conversationId: id, profileId: prepared.profile_id, available: true, revision: 0 });
    changed();
  }

  async function refresh(id = state.conversationId) {
    if (id !== state.conversationId || id === null) return false;
    const context = epoch;
    const reading = ++readVersion;
    state.ready = false;
    changed();
    try {
      const data = await read(await request(`/api/conversations/${encodeURIComponent(id)}/profile`, { cache: 'no-store', signal: AbortSignal.timeout(12000) }));
      if (context !== epoch || reading !== readVersion) return false;
      if (!apply(data)) state.ready = true;
      return true;
    } catch (error) {
      if (context === epoch && reading === readVersion) state.error = error.message;
      return false;
    } finally {
      if (context === epoch && reading === readVersion) changed();
    }
  }

  function select(profileId) {
    if (pending) return pending;
    if (!state.ready) return Promise.reject(new Error('Reload the model selection before choosing a profile.'));
    if (profileId === state.profileId) return Promise.resolve(false);
    if (state.conversationId === null) {
      state.profileId = profileId;
      state.available = true;
      state.error = '';
      changed();
      return Promise.resolve(true);
    }
    const context = epoch;
    const id = state.conversationId;
    const revision = state.revision;
    ++readVersion;
    state.saving = true;
    state.error = '';
    changed();
    pending = (async () => {
      try {
        const data = await read(await request(`/api/conversations/${encodeURIComponent(id)}/profile`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ profile_id: profileId, expected_profile_revision: revision }), signal: AbortSignal.timeout(12000)
        }));
        if (context !== epoch) return false;
        if (!apply(data)) throw new Error('The model selection changed again. Review the current model before sending.');
        return true;
      } catch (error) {
        if (context !== epoch) return false;
        await refresh(id);
        if (context !== epoch) return false;
        state.error = error.message;
        throw error;
      } finally {
        if (context === epoch) {
          pending = null;
          state.saving = false;
          changed();
        }
      }
    })();
    return pending;
  }

  async function prepare() {
    const context = epoch;
    if (pending) await pending;
    if (context !== epoch) throw new DOMException('Conversation changed while saving its model.', 'AbortError');
    if (!state.ready) throw new Error('Reload the model selection before sending.');
    if (!state.profileId) throw new Error('Select a model profile for this conversation before sending.');
    if (state.available === false) throw new Error('The selected model profile is unavailable. Choose another profile before sending.');
    return { profile_id: state.profileId, ...(state.conversationId === null ? {} : { expected_profile_revision: state.revision }), epoch };
  }

  return { snapshot, startDraft, setDefault, selectConversation, adoptConversation, select, refresh, prepare };
}
