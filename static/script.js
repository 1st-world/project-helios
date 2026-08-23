const state = {
  conversationId: null,
  controller: null,
  workspaceFile: null,
  workspaceRoot: '',
  generating: false,
  activeProfileId: null,
  editingProfileId: null,
  profiles: []
};
const $ = (selector) => document.querySelector(selector);
const chat = $('#chat');
const prompt = $('#prompt');
const send = $('#send');
const stop = $('#stop');
const emptyChatMarkup = `
  <div class="empty-state">
    <span><i data-lucide="sparkles"></i></span>
    <h1>How can I help?</h1>
    <p>Use your Azure OpenAI deployment to explore and work with local project files.</p>
  </div>
`;

// Markdown Highlighting Setup
marked.setOptions({
  highlight(code, language) {
    return language && hljs.getLanguage(language) ? hljs.highlight(code, { language }).value : hljs.highlightAuto(code).value;
  }
});

function escapeHtml(value) {
  const div = document.createElement('div');
  div.textContent = value;
  return div.innerHTML;
}
function refreshIcons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { 'stroke-width': 1.8 } });
}

let toastTimer = null;
function toast(message, type = 'info') {
  const el = $('#toast');
  if (!el) return;
  const icons = {
    success: 'check-circle-2',
    error: 'alert-circle',
    danger: 'alert-circle',
    warning: 'alert-triangle',
    info: 'info'
  };
  const iconName = icons[type] || 'info';
  el.className = `toast toast-${type}`;
  el.innerHTML = `<i data-lucide="${iconName}"></i><span>${escapeHtml(String(message))}</span>`;
  const openDialog = document.querySelector('dialog[open]');
  if (openDialog) {
    if (el.parentElement !== openDialog) {
      openDialog.appendChild(el);
    }
  } else {
    if (el.parentElement !== document.body) {
      document.body.appendChild(el);
    }
  }
  refreshIcons();
  el.classList.remove('hidden');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 4500);
}
function scrollDown(smooth = true) {
  chat.scrollTo({
    top: chat.scrollHeight,
    behavior: smooth ? 'smooth' : 'auto'
  });
}
function autoResize() {
  prompt.style.height = 'auto';
  prompt.style.height = `${Math.min(prompt.scrollHeight, 180)}px`;
}
function setGenerating(value) {
  state.generating = value;
  send.disabled = value;
  stop.classList.toggle('hidden', !value);
  $('#load-file').disabled = value;
}
function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle('sidebar-collapsed', collapsed);
  const toggle = $('#sidebar-toggle');
  toggle.innerHTML = `<i data-lucide="${collapsed ? 'panel-left-open' : 'panel-left-close'}"></i>`;
  toggle.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
  toggle.setAttribute('aria-label', toggle.title);
  localStorage.setItem('helios.sidebarCollapsed', String(collapsed));
  refreshIcons();
}

// UI Rendering
function formatMessageMeta(meta) {
  if (!meta) return '';
  const parts = [];
  const profileLabel = meta.profile_name || meta.deployment;
  if (profileLabel) {
    parts.push(`<span class="message-meta-tag"><i data-lucide="bot"></i> ${escapeHtml(profileLabel)}</span>`);
  }
  const inTokens = meta.input_tokens;
  const outTokens = meta.output_tokens;
  const totalTokens = meta.total_tokens ?? (inTokens != null && outTokens != null ? inTokens + outTokens : null);
  if (totalTokens != null) {
    const detail = (inTokens != null && outTokens != null) ? ` (${inTokens.toLocaleString()} in / ${outTokens.toLocaleString()} out)` : '';
    parts.push(`<span class="message-meta-item">${totalTokens.toLocaleString()} tokens${detail}</span>`);
  }
  if (meta.response_time_ms != null) {
    parts.push(`<span class="message-meta-item">· ${(meta.response_time_ms / 1000).toFixed(1)}s</span>`);
  }
  if (meta.estimated_cost !== null && meta.estimated_cost !== undefined) {
    parts.push(`<span class="message-meta-item message-meta-cost">· $${Number(meta.estimated_cost).toFixed(6)}</span>`);
  }
  if (meta.is_long_context) {
    parts.push(`<span class="message-meta-tag message-meta-tier">Long Tier</span>`);
  }
  return parts.join(' ');
}

function appendMessage(role, content = '', messageIndex = null, shouldScroll = true, smooth = false, meta = null) {
  chat.querySelector('.empty-state')?.remove();
  const el = document.createElement('article');
  el.className = `message ${role}`;
  if (role === 'assistant') {
    el.innerHTML = `<div class="avatar"><i data-lucide="sparkles"></i></div><div class="message-body"><div class="message-text"></div><div class="message-footer"><div class="message-meta"></div><div class="message-actions"></div></div></div>`;
  } else {
    el.innerHTML = `<div class="message-body"><div class="message-text"></div><div class="message-footer"><div class="message-actions"></div></div></div>`;
  }
  const textEl = el.querySelector('.message-text');
  const metaEl = el.querySelector('.message-meta');
  const actionsEl = el.querySelector('.message-actions');

  const render = (text) => {
    textEl.innerHTML = role === 'assistant' ? marked.parse(text) : escapeHtml(text).replace(/\n/g, '<br>');
  };
  render(content);

  const setMeta = (newMeta) => {
    if (!metaEl) return;
    metaEl.innerHTML = formatMessageMeta(newMeta);
    refreshIcons();
  };
  if (meta && metaEl) {
    setMeta(meta);
  }

  if (role === 'user' && Number.isInteger(messageIndex)) {
    const edit = document.createElement('button');
    edit.className = 'message-action-btn';
    edit.type = 'button';
    edit.innerHTML = '<i data-lucide="pencil"></i>';
    edit.setAttribute('title', 'Edit message');
    edit.setAttribute('aria-label', 'Edit message');
    edit.onclick = () => editUserMessage(messageIndex, textEl.innerText || content);
    actionsEl.append(edit);
  }
  const copy = document.createElement('button');
  copy.className = 'message-action-btn';
  copy.type = 'button';
  copy.innerHTML = '<i data-lucide="copy"></i>';
  copy.setAttribute('title', 'Copy content');
  copy.setAttribute('aria-label', 'Copy content');
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(textEl.innerText || content);
      toast('Copied to clipboard!', 'success');
    } catch {
      toast('Failed to copy.', 'error');
    }
  };
  actionsEl.append(copy);

  chat.append(el);
  refreshIcons();
  if (shouldScroll) scrollDown(smooth);
  return { el, render, setMeta };
}

function renderTree(entries, parent) {
  entries.forEach((entry) => {
    const row = document.createElement('div');
    row.className = `tree-entry ${entry.type === 'file' ? 'file' : 'tree-folder'} ${entry.path === state.workspaceFile ? 'selected' : ''}`;
    const icon = document.createElement('i');
    icon.setAttribute('data-lucide', entry.type === 'directory' ? 'folder' : 'file-text');
    icon.setAttribute('aria-hidden', 'true');
    const label = document.createElement('span');
    label.textContent = entry.name;
    row.append(icon, label);
    if (entry.type === 'file') { row.onclick = () => selectFile(entry.path); }
    parent.append(row);
    if (entry.children?.length) {
      const children = document.createElement('div');
      children.className = 'tree-children';
      parent.append(children);
      renderTree(entry.children, children);
    }
  });
}

// API & Event Handlers
function onStart(event) { state.conversationId = event.conversation_id; }
function onDelta(event, assistant, answer) {
  answer.value += event.text;
  assistant.render(answer.value);
  scrollDown(true);
}
function onFinish() { loadConversations(); }
function onError(message) { toast(message, 'error'); }
function onAbort(assistant, answer) { assistant.render(answer.value || '_Generation stopped._'); }

async function loadConversations() {
  const items = await fetch('/api/conversations').then((response) => response.json());
  const list = $('#conversation-list');
  list.innerHTML = '';
  items.forEach((item) => {
    const row = document.createElement('div');
    row.className = `conversation-row ${item.id === state.conversationId ? 'active' : ''}`;
    const open = document.createElement('button');
    open.className = 'conversation';
    open.type = 'button';
    open.textContent = item.title;
    open.title = item.title;
    open.onclick = () => openConversation(item.id);
    const actions = document.createElement('div');
    actions.className = 'conversation-actions';
    const rename = document.createElement('button');
    rename.className = 'conversation-action';
    rename.type = 'button';
    rename.innerHTML = '<i data-lucide="pencil"></i>';
    rename.title = 'Rename conversation';
    rename.setAttribute('aria-label', rename.title);
    rename.onclick = (event) => {
      event.stopPropagation();
      renameConversation(item);
    };
    const remove = document.createElement('button');
    remove.className = 'conversation-action delete';
    remove.type = 'button';
    remove.innerHTML = '<i data-lucide="trash-2"></i>';
    remove.title = 'Delete conversation';
    remove.setAttribute('aria-label', remove.title);
    remove.onclick = (event) => {
      event.stopPropagation();
      deleteConversation(item);
    };
    actions.append(rename, remove);
    row.append(open, actions);
    list.append(row);
  });
  refreshIcons();
}

async function openConversation(id) {
  const response = await fetch(`/api/conversations/${id}`);
  if (!response.ok) return toast('Could not open conversation.', 'error');
  const data = await response.json();
  state.conversationId = data.id;
  chat.innerHTML = '';
  data.messages.forEach((message, index) =>
    appendMessage(message.role, message.content, index, index === data.messages.length - 1, false, message)
  );
  loadConversations();
}

function newChat() {
  state.conversationId = null;
  state.workspaceFile = null;
  $('#file-chip').classList.add('hidden');
  chat.innerHTML = emptyChatMarkup;
  loadConversations();
  prompt.focus();
}

async function renameConversation(item) {
  const title = window.prompt('Conversation title', item.title);
  if (title === null || title.trim() === item.title) return;
  if (!title.trim()) return toast('A conversation title is required.', 'warning');
  const response = await fetch(`/api/conversations/${item.id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title })
  });
  if (!response.ok) return toast((await response.json()).detail || 'Could not rename conversation.', 'error');
  loadConversations();
}

async function deleteConversation(item) {
  if (!window.confirm(`Delete "${item.title}"? This cannot be undone.`)) return;
  const response = await fetch(`/api/conversations/${item.id}`, { method: 'DELETE' });
  if (!response.ok) return toast((await response.json()).detail || 'Could not delete conversation.', 'error');
  if (state.conversationId === item.id) newChat();
  else loadConversations();
}

async function loadWorkspace() {
  try {
    const data = await fetch('/api/workspace').then((response) => response.json());
    state.workspaceRoot = data.root;
    $('#workspace-path').textContent = data.root;
    $('#workspace-path').title = data.root;
    $('#workspace-path-input').value = data.root;
    const tree = $('#workspace-tree');
    tree.innerHTML = '';
    renderTree(data.entries, tree);
    if (!data.entries.length) { tree.innerHTML = '<p class="workspace-empty">This folder is empty. Choose another project folder or add files to it.</p>'; }
    refreshIcons();
  }
  catch {
    toast('Could not load workspace.', 'error');
  }
}

function selectFile(path) {
  state.workspaceFile = path;
  $('#file-chip span').textContent = path;
  $('#file-chip').classList.remove('hidden');
  loadWorkspace();
}

async function editUserMessage(messageIndex, currentContent) {
  if (state.generating || !state.conversationId) return;
  const content = window.prompt('Edit your message', currentContent);
  if (content === null || content.trim() === currentContent) return;
  if (!content.trim()) return toast('Message cannot be empty.', 'warning');
  const response = await fetch(`/api/conversations/${state.conversationId}/messages/${messageIndex}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content })
  });
  if (!response.ok) return toast((await response.json()).detail || 'Could not edit message.', 'error');
  await openConversation(state.conversationId);
  sendMessage({ prompt: content.trim(), regenerateMessageIndex: messageIndex, appendUser: false });
}

async function sendMessage(options = {}) {
  const text = options.prompt ?? prompt.value.trim();
  if (!text || state.generating) return;
  if (options.appendUser !== false) appendMessage('user', text);
  prompt.value = '';
  autoResize();
  setGenerating(true);
  const assistant = appendMessage('assistant', '');
  assistant.el.querySelector('.message-text').innerHTML = '<span class="typing">Thinking</span>';
  const answer = { value: '' };
  try {
    state.controller = new AbortController();
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt: text,
        conversation_id: state.conversationId,
        workspace_file: state.workspaceFile,
        profile_id: state.activeProfileId,
        regenerate_message_index: options.regenerateMessageIndex
      }),
      signal: state.controller.signal
    });
    if (!response.ok) throw new Error((await response.json()).detail || 'Request failed.');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const buffer = { value: '' };
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer.value += decoder.decode(value, { stream: true });
      const frames = buffer.value.split('\n\n');
      buffer.value = frames.pop();
      for (const frame of frames) {
        if (!frame.startsWith('data: ')) continue;
        const event = JSON.parse(frame.slice(6));
        if (event.type === 'start') onStart(event);
        if (event.type === 'delta') onDelta(event, assistant, answer);
        if (event.type === 'usage') {
          if (assistant.setMeta) {
            assistant.setMeta({
              profile_id: event.profile?.id,
              profile_name: event.profile?.name,
              deployment: event.profile?.deployment,
              input_tokens: event.usage.input_tokens,
              output_tokens: event.usage.output_tokens,
              total_tokens: event.usage.total_tokens,
              response_time_ms: event.usage.response_time_ms,
              estimated_cost: event.usage.estimated_cost,
              is_long_context: event.usage.is_long_context,
            });
          }
        }
        if (event.type === 'done') onFinish();
        if (event.type === 'error') throw new Error(event.message);
      }
    }
  } catch (error) {
    if (error.name === 'AbortError') {
      onAbort(assistant, answer);
    } else {
      assistant.render(answer.value || '_Unable to generate a response._');
      onError(error.message);
    }
  } finally {
    state.controller = null;
    setGenerating(false);
    if (state.conversationId) {
      await openConversation(state.conversationId);
    } else {
      loadConversations();
    }
  }
}

function updatePricingStatus(hasPricing) {
  const statusEl = $('#pricing-accordion-status');
  if (!statusEl) return;
  statusEl.classList.toggle('configured', Boolean(hasPricing));
  statusEl.innerHTML = hasPricing
    ? '<i data-lucide="check"></i> Configured'
    : '<i data-lucide="circle-dashed"></i> Not Configured';
  refreshIcons();
}

function resetProfileForm() {
  state.editingProfileId = null;
  $('#editing-profile-id').value = '';
  $('#profile-form-title').textContent = 'Add New Profile';
  const saveLabel = $('#save-profile-label');
  if (saveLabel) saveLabel.textContent = 'Add Profile';
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
  updateThresholdLabels();
  updatePricingStatus(false);
  $('#delete-profile-btn').classList.add('hidden');
  const toggleBtn = $('#toggle-api-key-btn');
  if (toggleBtn) toggleBtn.innerHTML = '<i data-lucide="eye"></i>';
  document.querySelectorAll('.profile-item').forEach((item) => item.classList.remove('editing'));
  refreshIcons();
}

function selectProfileForEditing(profile) {
  state.editingProfileId = profile.id;
  $('#editing-profile-id').value = profile.id;
  $('#profile-form-title').textContent = `Edit Profile: ${profile.name}`;
  const saveLabel = $('#save-profile-label');
  if (saveLabel) saveLabel.textContent = 'Save Changes';
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
  updateThresholdLabels();
  const hasPricing = profile.input_price_per_million != null || profile.output_price_per_million != null || profile.long_input_price_per_million != null || profile.long_output_price_per_million != null;
  updatePricingStatus(hasPricing);
  $('#delete-profile-btn').classList.remove('hidden');
  const toggleBtn = $('#toggle-api-key-btn');
  if (toggleBtn) toggleBtn.innerHTML = '<i data-lucide="eye"></i>';
  document.querySelectorAll('.profile-item').forEach((item) => {
    item.classList.toggle('editing', item.dataset.profileId === profile.id);
  });
  refreshIcons();
}

function renderProfiles(data) {
  state.activeProfileId = data.active_profile_id;
  state.profiles = data.profiles || [];
  const select = $('#profile-select');
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
        if (!e.target.closest('button')) selectProfileForEditing(p);
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
  try {
    const data = await fetch('/api/health').then((response) => response.json());
    const el = $('#connection-status');
    el.className = `status ${data.configured ? 'connected' : 'error'}`;
    el.innerHTML = `<i></i>${data.configured ? 'Azure connected' : 'Azure not configured'}`;
  }
  catch { $('#connection-status').textContent = 'Offline'; }
}

// Event Listeners & Initialization
$('#new-chat').onclick = newChat;
$('#sidebar-toggle').onclick = () => setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed'));
$('#refresh-workspace').onclick = loadWorkspace;
$('#choose-workspace').onclick = () => $('#workspace-dialog').showModal();
$('#open-settings').onclick = async () => {
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
};
$('#add-profile-btn').onclick = resetProfileForm;

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

document.querySelectorAll('[data-close-dialog]').forEach((button) => {
  button.onclick = () => $(`#${button.dataset.closeDialog}`).close();
});

document.querySelectorAll('dialog').forEach((dlg) => {
  dlg.addEventListener('close', () => {
    const el = $('#toast');
    if (el && el.parentElement === dlg) {
      document.body.appendChild(el);
    }
    if (dlg.id === 'settings-dialog') {
      const accordion = $('#profile-pricing-accordion');
      if (accordion) accordion.open = false;
    }
  });
});

$('#workspace-form').onsubmit = async (event) => {
  event.preventDefault();
  const path = $('#workspace-path-input').value.trim();
  const response = await fetch('/api/workspace/root', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path })
  });
  if (!response.ok) return toast((await response.json()).detail || 'Could not open workspace folder.', 'error');
  state.workspaceFile = null;
  $('#file-chip').classList.add('hidden');
  $('#workspace-dialog').close();
  await loadWorkspace();
  toast('Workspace folder updated.', 'success');
};

$('#azure-settings-form').onsubmit = async (event) => {
  event.preventDefault();
  const editingId = $('#editing-profile-id').value;
  const name = $('#profile-name').value.trim();
  const endpoint = $('#azure-endpoint').value.trim();
  const api_key = $('#azure-api-key').value.trim();
  const deployment = $('#azure-deployment').value.trim();
  if (!name) return toast('Profile name is required.', 'warning');
  if (!endpoint) return toast('Azure endpoint is required.', 'warning');
  if (!editingId && !api_key) return toast('API key is required when creating a new profile.', 'warning');
  if (!deployment) return toast('Deployment name is required.', 'warning');
  const payload = { name, endpoint, deployment };
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
  try {
    const response = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const err = await response.json();
      throw new Error(Array.isArray(err.detail) ? err.detail[0]?.msg : (err.detail || 'Could not save profile.'));
    }
    const data = await response.json();
    $('#azure-api-key').value = '';
    renderProfiles(data);
    health();
    if (!editingId) {
      resetProfileForm();
      toast('Connection profile created.', 'success');
    } else {
      toast('Connection profile saved.', 'success');
      const updated = (data.profiles || []).find((p) => p.id === editingId);
      if (updated) selectProfileForEditing(updated);
    }
  } catch (err) {
    toast(err.message, 'error');
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
    toast('Profile deleted.', 'success');
  } catch (err) {
    toast(err.message, 'error');
  }
};

$('#profile-select').onchange = (event) => switchActiveProfile(event.target.value);
$('#load-file').onclick = () => {
  if (!state.workspaceFile) {
    toast('Select a text file in the Workspace panel first.', 'info');
  } else {
    toast(`Attached for the next message: ${state.workspaceFile}`, 'info');
  }
};
$('#file-chip button').onclick = () => {
  state.workspaceFile = null;
  $('#file-chip').classList.add('hidden');
};
send.onclick = sendMessage;
stop.onclick = () => state.controller?.abort();
prompt.oninput = autoResize;
prompt.onkeydown = (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
};

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
  if (stdLabel) stdLabel.textContent = `≤ ${display} tokens`;
  if (longLabel) longLabel.textContent = `> ${display} tokens`;
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

// Initial setup
setSidebarCollapsed(localStorage.getItem('helios.sidebarCollapsed') === 'true');
refreshIcons();
health();
loadWorkspace();
newChat();
loadProfiles();
setupPricingAccordionAnimation();
