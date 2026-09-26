const state = {
  conversationId: null,
  conversationVersion: null,
  controller: null,
  workspaceFile: null,
  workspaceRoot: '',
  workspaceEntries: [],
  pendingFile: null,
  attachingFile: false,
  fileDialogVersion: 0,
  workspaceRequest: 0,
  pickingWorkspace: false,
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

if (window.DOMPurify) {
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A') {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  });
}

function renderMarkdown(text) {
  const parsed = marked.parse(text);
  if (window.DOMPurify) {
    return DOMPurify.sanitize(parsed);
  }
  return parsed;
}

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
function isNearBottom(threshold = 80) {
  return chat.scrollHeight - chat.scrollTop - chat.clientHeight <= threshold;
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
  ['load-file', 'workspace-card', 'workspace-context-chip', 'remove-focus-file', 'profile-select'].forEach((id) => {
    const el = $(`#${id}`);
    if (el) el.disabled = value;
  });
  const modelPicker = $('#model-picker');
  if (modelPicker) {
    if (value) modelPicker.open = false;
    modelPicker.inert = value;
  }
  chat.querySelectorAll('[data-mutates-conversation]').forEach((button) => { button.disabled = value; });
}
function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle('sidebar-collapsed', collapsed);
  const toggle = $('#sidebar-toggle');
  const showExpand = collapsed && window.innerWidth > 720;
  const iconName = showExpand ? 'panel-left-open' : 'panel-left-close';
  if (toggle.dataset.icon !== iconName) {
    toggle.innerHTML = `<i data-lucide="${iconName}"></i>`;
    toggle.dataset.icon = iconName;
  }
  toggle.setAttribute('aria-expanded', String(!showExpand));
  $('#sidebar-navigation').inert = showExpand;
  const sidebar = $('#sidebar');
  if (sidebar) {
    sidebar.inert = window.innerWidth <= 720 && !document.body.classList.contains('sidebar-open');
    if (showExpand) {
      sidebar.title = 'Click to expand sidebar';
    } else {
      sidebar.removeAttribute('title');
    }
  }
  toggle.title = window.innerWidth <= 720 ? 'Close sidebar' : (collapsed ? 'Expand sidebar' : 'Collapse sidebar');
  toggle.setAttribute('aria-label', toggle.title);
  localStorage.setItem('helios.sidebarCollapsed', String(collapsed));
  refreshIcons();
}
function setMobileSidebarOpen(open) {
  document.body.classList.toggle('sidebar-open', open);
  $('#sidebar').inert = window.innerWidth <= 720 && !open;
  const mobileToggle = $('#mobile-sidebar-toggle');
  if (mobileToggle) {
    mobileToggle.innerHTML = `<i data-lucide="${open ? 'panel-left-close' : 'panel-left-open'}"></i>`;
    mobileToggle.title = open ? 'Close sidebar' : 'Open sidebar';
    mobileToggle.setAttribute('aria-label', mobileToggle.title);
  }
  refreshIcons();
}
function closeMobileSidebar() {
  if (window.innerWidth <= 720 && document.body.classList.contains('sidebar-open')) {
    setMobileSidebarOpen(false);
  }
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

function appendMessage(role, content = '', messageIndex = null, shouldScroll = true, smooth = false, meta = null, regeneration = null) {
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
    textEl.innerHTML = role === 'assistant' ? renderMarkdown(text) : escapeHtml(text).replace(/\n/g, '<br>');
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
    edit.dataset.mutatesConversation = '';
    edit.disabled = state.generating;
    edit.innerHTML = '<i data-lucide="pencil"></i>';
    edit.setAttribute('title', 'Edit message');
    edit.setAttribute('aria-label', 'Edit message');
    edit.onclick = () => editUserMessage(messageIndex, textEl.innerText || content);
    actionsEl.append(edit);
  }
  if (role === 'assistant' && regeneration) {
    const regenerate = document.createElement('button');
    regenerate.className = 'message-action-btn';
    regenerate.type = 'button';
    regenerate.dataset.mutatesConversation = '';
    regenerate.disabled = state.generating;
    regenerate.innerHTML = '<i data-lucide="rotate-cw"></i>';
    regenerate.setAttribute('title', 'Regenerate response');
    regenerate.setAttribute('aria-label', 'Regenerate response');
    regenerate.onclick = () => regenerateResponse(regeneration);
    actionsEl.append(regenerate);
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

function renderTree(entries, parent, query = '') {
  for (const entry of entries) {
    if (entry.type === 'directory') {
      const group = document.createElement('details');
      group.open = true;
      const label = document.createElement('summary');
      label.className = 'tree-entry tree-folder';
      label.innerHTML = '<i data-lucide="folder"></i>';
      const name = document.createElement('span');
      name.textContent = entry.name;
      label.append(name);
      const children = document.createElement('div');
      children.className = 'tree-children';
      renderTree(entry.children || [], children, query);
      if (!children.childElementCount) continue;
      group.append(label, children);
      parent.append(group);
    } else if (!query || entry.path.toLowerCase().includes(query)) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'tree-entry file';
      row.dataset.path = entry.path;
      row.title = entry.path;
      row.setAttribute('aria-pressed', String(entry.path === state.pendingFile));
      row.classList.toggle('selected', entry.path === state.pendingFile);
      row.innerHTML = '<i data-lucide="file-text"></i>';
      const name = document.createElement('span');
      name.textContent = query ? entry.path : entry.name;
      row.append(name);
      row.onclick = () => selectFile(entry.path);
      parent.append(row);
    }
  }
}

function renderFileList() {
  const tree = $('#workspace-tree');
  tree.replaceChildren();
  const query = $('#file-search').value.trim().toLowerCase();
  renderTree(state.workspaceEntries, tree, query);
  if (!tree.childElementCount) {
    const empty = document.createElement('p');
    empty.className = 'workspace-empty-state';
    empty.textContent = query ? 'No matching files. Try another name or path.' : 'No files in this folder. Choose another folder in Workspace settings.';
    tree.append(empty);
  }
  refreshIcons();
}

// API & Event Handlers
function onStart(event) { state.conversationId = event.conversation_id; }
function onDelta(event, assistant, answer) {
  const wasNearBottom = isNearBottom();
  answer.value += event.text;
  assistant.render(answer.value);
  if (wasNearBottom) { scrollDown(false); }
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

async function openConversation(id, preserveScroll = false) {
  closeMobileSidebar();
  const response = await fetch(`/api/conversations/${id}`);
  if (!response.ok) return toast('Could not open conversation.', 'error');
  const data = await response.json();
  state.conversationId = data.id;
  state.conversationVersion = data.version;
  const savedScrollTop = chat.scrollTop;
  const wasNearBottom = isNearBottom();
  chat.innerHTML = '';
  data.messages.forEach((message, index) => {
    const source = data.messages[index - 1];
    const regeneration = message.role === 'assistant' && source?.role === 'user'
      ? { userIndex: index - 1, content: source.content, hasFollowing: index < data.messages.length - 1 }
      : null;
    const shouldScroll = preserveScroll && !wasNearBottom ? false : index === data.messages.length - 1;
    appendMessage(message.role, message.content, index, shouldScroll, false, message, regeneration);
  });
  if (preserveScroll && !wasNearBottom) { chat.scrollTop = savedScrollTop; }
  loadConversations();
}

function newChat() {
  closeMobileSidebar();
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

function countFiles(entries) {
  return entries.reduce((count, entry) => count + (entry.type === 'file' ? 1 : countFiles(entry.children || [])), 0);
}

function applyWorkspace(data) {
  if (state.workspaceRoot && state.workspaceRoot !== data.root) clearFocusFile();
  state.workspaceRoot = data.root;
  state.workspaceEntries = data.entries;
  const folderName = data.root.split(/[/\\]/).filter(Boolean).pop() || data.root;
  const fileCount = countFiles(data.entries);
  $('#workspace-folder-name').textContent = folderName;
  $('#workspace-file-count').textContent = `${fileCount} file${fileCount === 1 ? '' : 's'} · Folder context on`;
  $('#workspace-card').title = `Manage workspace: ${data.root}`;
  $('#composer-workspace-name').textContent = folderName;
  $('#composer-workspace-sub').textContent = 'Folder context';
  $('#workspace-context-chip').title = `Folder paths included: ${data.root}`;
  $('#file-source-name').textContent = folderName;
  $('#file-source-name').title = data.root;
  const paths = new Set();
  const collect = (entries) => entries.forEach((entry) => {
    if (entry.type === 'file') paths.add(entry.path);
    if (entry.children) collect(entry.children);
  });
  collect(data.entries);
  if (state.workspaceFile && !paths.has(state.workspaceFile)) clearFocusFile();
  if (state.pendingFile && !paths.has(state.pendingFile)) state.pendingFile = null;
  selectFile(state.pendingFile);
  renderFileList();
}

async function loadWorkspace() {
  const request = ++state.workspaceRequest;
  try {
    const response = await fetch('/api/workspace');
    if (!response.ok) throw new Error('Could not load workspace.');
    const data = await response.json();
    if (request !== state.workspaceRequest) return;
    applyWorkspace(data);
  } catch {
    if (request !== state.workspaceRequest) return;
    $('#workspace-file-count').textContent = 'Folder unavailable';
    $('#composer-workspace-sub').textContent = 'Unavailable';
    $('#workspace-tree').textContent = 'Could not load files. Use Refresh to try again.';
    state.workspaceEntries = [];
    selectFile(null);
    toast('Could not load workspace.', 'error');
  }
}

function selectFile(path) {
  state.pendingFile = path;
  $('#workspace-tree').querySelectorAll('[data-path]').forEach((row) => {
    const selected = row.dataset.path === path;
    row.classList.toggle('selected', selected);
    row.setAttribute('aria-pressed', String(selected));
  });
  $('#selected-file-label').textContent = path || 'No file selected';
  $('#selected-file-label').dataset.hasFile = String(Boolean(path));
  $('#selected-file-label').title = path || '';
  $('#attach-selected-file').disabled = !path || state.attachingFile;
}

function clearFocusFile() {
  state.workspaceFile = null;
  $('#file-chip').classList.add('hidden');
}

function openWorkspaceDialog() {
  closeMobileSidebar();
  $('#workspace-path-input').value = state.workspaceRoot;
  $('#workspace-dialog').showModal();
}

async function openFileDialog() {
  ++state.fileDialogVersion;
  closeMobileSidebar();
  state.pendingFile = state.workspaceFile;
  $('#file-search').value = '';
  selectFile(state.pendingFile);
  renderFileList();
  $('#file-dialog').showModal();
  $('#file-search').focus();
  await loadWorkspace();
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
  await openConversation(state.conversationId, true);
  sendMessage({ prompt: content.trim(), regenerateMessageIndex: messageIndex, appendUser: false });
}

async function sendMessage(options = {}) {
  const text = options.prompt ?? prompt.value.trim();
  if (!text || state.generating) return;
  if (options.appendUser !== false) appendMessage('user', text);
  if (!options.preserveDraft) {
    prompt.value = '';
    autoResize();
  }
  const requestConversationId = state.conversationId;
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
        regenerate_message_index: options.regenerateMessageIndex,
        expected_conversation_version: options.expectedConversationVersion
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
        if (event.type === 'start' && (!options.preserveDraft || state.conversationId === requestConversationId)) onStart(event);
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
    try {
      if (state.conversationId && (!options.preserveDraft || state.conversationId === requestConversationId)) {
        await openConversation(state.conversationId, true);
      } else {
        loadConversations();
      }
    } finally {
      setGenerating(false);
    }
  }
}

function regenerateResponse({ userIndex, content, hasFollowing }) {
  if (state.generating || !state.conversationId) return;
  if (hasFollowing && !window.confirm('Regenerate this response? Once successful, this will replace the response and remove all following messages.')) return;
  // The server keeps the original branch until regeneration completes successfully.
  [...chat.querySelectorAll('.message')].slice(userIndex + 1).forEach((message) => message.remove());
  sendMessage({ prompt: content, regenerateMessageIndex: userIndex, appendUser: false,
    preserveDraft: true, expectedConversationVersion: state.conversationVersion });
}

function updatePricingStatus(hasPricing) {
  const statusEl = $('#pricing-accordion-status');
  if (!statusEl) return;
  statusEl.classList.toggle('configured', Boolean(hasPricing));
  statusEl.innerHTML = hasPricing
    ? '<i data-lucide="check"></i> Configured'
    : '<i data-lucide="circle-dashed"></i> Not configured';
  refreshIcons();
}

function resetProfileForm() {
  state.editingProfileId = null;
  $('#editing-profile-id').value = '';
  $('#profile-form-title').textContent = 'Add new profile';
  const saveLabel = $('#save-profile-label');
  if (saveLabel) saveLabel.textContent = 'Add profile';
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
  $('#profile-form-title').textContent = `Edit profile: ${profile.name}`;
  const saveLabel = $('#save-profile-label');
  if (saveLabel) saveLabel.textContent = 'Save changes';
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
  const activeProfile = state.profiles.find((p) => p.id === state.activeProfileId);

  // Sync background select
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

  // Update model dropup trigger label
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

  // Populate model dropup popover list
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
  if (state.generating) return;
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
    const modelTitle = data.configured ? 'Model for the next response' : 'No configured model. Open Settings in the sidebar.';
    if ($('#profile-select')) $('#profile-select').title = modelTitle;
    if ($('#model-trigger')) $('#model-trigger').title = modelTitle;
  } catch {
    el.className = 'status error';
    el.textContent = 'Helios is offline. Check the local server.';
  }
}

// Event Listeners & Initialization
const sidebarEl = $('#sidebar');
if (sidebarEl) {
  sidebarEl.onclick = (event) => {
    if (!document.body.classList.contains('sidebar-collapsed')) return;
    if (event.target.closest('button, .workspace-card, .sidebar-dock-btn')) return;
    setSidebarCollapsed(false);
  };
}

$('#new-chat').onclick = newChat;
$('#sidebar-toggle').onclick = (event) => {
  event.stopPropagation();
  if (window.innerWidth <= 720) {
    setMobileSidebarOpen(false);
  } else {
    setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed'));
  }
};

const mobileSidebarToggle = $('#mobile-sidebar-toggle');
if (mobileSidebarToggle) {
  mobileSidebarToggle.onclick = (event) => {
    event.stopPropagation();
    setMobileSidebarOpen(!document.body.classList.contains('sidebar-open'));
  };
}

const sidebarBackdrop = $('#sidebar-backdrop');
if (sidebarBackdrop) {
  sidebarBackdrop.onclick = () => setMobileSidebarOpen(false);
}

$('#workspace-card').onclick = openWorkspaceDialog;
$('#workspace-context-chip').onclick = openWorkspaceDialog;
$('#file-search').oninput = renderFileList;
$('#attach-selected-file').onclick = async () => {
  const path = state.pendingFile;
  const root = state.workspaceRoot;
  const dialogVersion = state.fileDialogVersion;
  if (!path || state.attachingFile) return;
  state.attachingFile = true;
  const button = $('#attach-selected-file');
  button.disabled = true;
  button.textContent = 'Checking...';
  try {
    const response = await fetch('/api/read-file', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path })
    });
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Could not attach this file.');
    }
    if (!$('#file-dialog').open || dialogVersion !== state.fileDialogVersion || state.pendingFile !== path || state.workspaceRoot !== root) return;
    state.workspaceFile = path;
    $('#file-chip .file-chip-name').textContent = path;
    $('#file-chip .file-chip-name').title = path;
    $('#file-chip').classList.remove('hidden');
    $('#file-dialog').close();
    prompt.focus();
  } catch (error) {
    if ($('#file-dialog').open && dialogVersion === state.fileDialogVersion) {
      toast(error.message || 'Could not attach this file.', 'error');
    }
  } finally {
    state.attachingFile = false;
    button.textContent = 'Attach file';
    button.disabled = !state.pendingFile;
  }
};

const workTools = $('#work-tools');
document.addEventListener('click', (event) => {
  if (!workTools.contains(event.target)) workTools.open = false;
});
workTools.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    workTools.open = false;
    workTools.querySelector('summary').focus();
    event.stopPropagation();
  }
});

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

window.addEventListener('resize', () => {
  document.body.classList.toggle('mobile-layout', window.innerWidth <= 720);
  setSidebarCollapsed(document.body.classList.contains('sidebar-collapsed'));
  if (window.innerWidth > 720 && document.body.classList.contains('sidebar-open')) {
    document.body.classList.remove('sidebar-open');
  }
});

window.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
    setMobileSidebarOpen(false);
  }
});

async function openSettingsDialog() {
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
}

$('#refresh-workspace').onclick = loadWorkspace;
$('#open-settings').onclick = openSettingsDialog;
const modelSettingsBtn = $('#model-popover-settings');
if (modelSettingsBtn) {
  modelSettingsBtn.onclick = () => {
    if (modelPicker) modelPicker.open = false;
    openSettingsDialog();
  };
}
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
  button.onclick = () => {
    if (button.dataset.closeDialog === 'workspace-dialog' && state.pickingWorkspace) {
      cancelWorkspacePicker();
      return;
    }
    $(`#${button.dataset.closeDialog}`).close();
  };
});

document.querySelectorAll('dialog').forEach((dlg) => {
  let isMouseDownOnBackdrop = false;

  dlg.addEventListener('cancel', (event) => {
    if (dlg.id === 'workspace-dialog' && state.pickingWorkspace) {
      event.preventDefault();
      cancelWorkspacePicker();
    }
  });

  dlg.addEventListener('mousedown', (event) => {
    if (event.target !== dlg) {
      isMouseDownOnBackdrop = false;
      return;
    }
    const rect = dlg.getBoundingClientRect();
    const isInside =
      event.clientX >= rect.left &&
      event.clientX <= rect.right &&
      event.clientY >= rect.top &&
      event.clientY <= rect.bottom;
    isMouseDownOnBackdrop = !isInside;
  });

  dlg.addEventListener('mouseup', (event) => {
    if (isMouseDownOnBackdrop && event.target === dlg) {
      const rect = dlg.getBoundingClientRect();
      const isInside =
        event.clientX >= rect.left &&
        event.clientX <= rect.right &&
        event.clientY >= rect.top &&
        event.clientY <= rect.bottom;
      if (!isInside) {
        if (dlg.id === 'workspace-dialog' && state.pickingWorkspace) cancelWorkspacePicker();
        else dlg.close();
      }
    }
    isMouseDownOnBackdrop = false;
  });

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
  if (state.pickingWorkspace) return;
  const button = $('#apply-workspace');
  button.disabled = true;
  try {
    const response = await fetch('/api/workspace/root', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: $('#workspace-path-input').value.trim() })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not open workspace folder.');
    ++state.workspaceRequest;
    clearFocusFile();
    state.pendingFile = null;
    applyWorkspace(data);
    $('#workspace-dialog').close();
    toast('Workspace folder updated.', 'success');
  } catch (error) {
    toast(error.message || 'Could not open workspace folder.', 'error');
  } finally {
    button.disabled = false;
  }
};

const pickerStorageKey = 'helios.workspacePicker';
let workspacePicker = null;
let pickerPageLeaving = false;

function disconnectWorkspacePicker(picker) {
  if (!picker || workspacePicker !== picker) return;
  picker.cancelRequested = true;
  picker.restartAfterCancel = false;
  sessionStorage.setItem(pickerStorageKey + '.cancel', picker.token);
  finishWorkspacePicker(picker, { status: 'disconnected' });
  toast('Connection lost. The picker could not be confirmed closed. Its status will be checked before the next Browse.', 'warning');
}

function waitForPickerConnection(picker) {
  if (!picker.connectionTimer) {
    // Bound connection recovery only; an active folder selection has no timeout.
    picker.connectionTimer = setTimeout(() => disconnectWorkspacePicker(picker), 10000);
  }
}

function finishWorkspacePicker(picker, result) {
  if (workspacePicker !== picker) return;
  workspacePicker = null;
  state.pickingWorkspace = false;
  clearTimeout(picker.retry);
  clearTimeout(picker.connectionTimer);
  clearTimeout(picker.cancelTimer);
  if (result.status !== 'disconnected') {
    sessionStorage.removeItem(pickerStorageKey);
    sessionStorage.removeItem(pickerStorageKey + '.cancel');
    sessionStorage.removeItem(pickerStorageKey + '.path');
  }
  picker.socket?.close();
  picker.controls.forEach((control, index) => { control.disabled = picker.disabled[index]; });
  $('#workspace-path-help').textContent = picker.help;
  if (picker.closeOnCancel) {
    $('#workspace-dialog').close();
  } else if (result.status === 'selected' && $('#workspace-dialog').open) {
    $('#workspace-path-input').value = result.path;
  } else if (result.status === 'error' || result.status === 'missing') {
    toast(result.detail || 'The previous folder picker has closed. Browse again to choose a folder.', 'warning');
  }
  if ($('#workspace-dialog').open) $('#workspace-path-input').focus();
  if (picker.restartAfterCancel && ['cancelled', 'missing'].includes(result.status)) beginWorkspacePicker();
}

function connectWorkspacePicker(picker, start = false) {
  if (workspacePicker !== picker || pickerPageLeaving) return;
  waitForPickerConnection(picker);
  const url = new URL('/api/workspace/picker/' + picker.token, location.href);
  url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(url);
  picker.socket = socket;
  socket.onopen = () => {
    if (workspacePicker !== picker || picker.socket !== socket || pickerPageLeaving) { socket.close(); return; }
    socket.send(JSON.stringify({ action: start ? 'start' : 'resume' }));
    if (picker.cancelRequested) socket.send(JSON.stringify({ action: 'cancel' }));
  };
  socket.onmessage = (event) => {
    if (workspacePicker !== picker || picker.socket !== socket || pickerPageLeaving) return;
    const result = JSON.parse(event.data);
    if (result.status === 'reconnecting') {
      $('#workspace-path-help').textContent = 'Waiting for the previous picker connection to close…';
    } else if (result.status === 'pending') {
      clearTimeout(picker.connectionTimer);
      picker.connectionTimer = null;
      $('#workspace-path-help').textContent = picker.cancelRequested
        ? 'Closing folder picker…'
        : 'Choose a folder in the picker, or use Cancel here to close it.';
    } else if (result.status === 'cancel-error') {
      $('#workspace-path-help').textContent = result.detail;
      toast(result.detail, 'warning');
    } else if (picker.cancelRequested && !['cancelled', 'missing'].includes(result.status)) {
      // A selected result can race with Cancel. Wait for cancellation acknowledgement.
      socket.send(JSON.stringify({ action: 'cancel' }));
    } else {
      finishWorkspacePicker(picker, result);
    }
  };
  socket.onclose = () => {
    if (workspacePicker !== picker || picker.socket !== socket || pickerPageLeaving) return;
    waitForPickerConnection(picker);
    $('#workspace-path-help').textContent = 'Reconnecting to folder picker… You can use Cancel to leave this dialog.';
    picker.retry = setTimeout(() => connectWorkspacePicker(picker), 1000);
  };
}

function beginWorkspacePicker(token = null, resume = false) {
  if (state.pickingWorkspace) return;
  const savedToken = sessionStorage.getItem(pickerStorageKey);
  token = token || savedToken || crypto.randomUUID();
  const pendingCancel = sessionStorage.getItem(pickerStorageKey + '.cancel') === token;
  const restartAfterCancel = !resume && pendingCancel;
  resume = resume || token === savedToken;
  sessionStorage.setItem(pickerStorageKey, token);
  sessionStorage.setItem(pickerStorageKey + '.path', $('#workspace-path-input').value);
  state.pickingWorkspace = true;
  const controls = Array.from($('#workspace-dialog').querySelectorAll('#browse-workspace-btn, #apply-workspace, input'));
  const picker = {
    token, controls, disabled: controls.map((control) => control.disabled),
    help: $('#workspace-path-help').textContent,
    cancelRequested: pendingCancel, closeOnCancel: pendingCancel && !restartAfterCancel,
    restartAfterCancel, socket: null, retry: null, connectionTimer: null, cancelTimer: null
  };
  workspacePicker = picker;
  controls.forEach((control) => { control.disabled = true; });
  $('#workspace-path-help').textContent = resume ? 'Reconnecting to folder picker…' : 'Opening folder picker…';
  connectWorkspacePicker(picker, !resume);
  if (pendingCancel) picker.cancelTimer = setTimeout(() => disconnectWorkspacePicker(picker), 5000);
}

function cancelWorkspacePicker() {
  const picker = workspacePicker;
  if (!picker) return;
  picker.cancelRequested = true;
  picker.closeOnCancel = true;
  picker.restartAfterCancel = false;
  sessionStorage.setItem(pickerStorageKey + '.cancel', picker.token);
  $('#workspace-path-help').textContent = 'Closing folder picker…';
  if (picker.socket?.readyState === WebSocket.OPEN) {
    picker.socket.send(JSON.stringify({ action: 'cancel' }));
    if (!picker.cancelTimer) picker.cancelTimer = setTimeout(() => disconnectWorkspacePicker(picker), 5000);
  } else {
    disconnectWorkspacePicker(picker);
  }
}

$('#browse-workspace-btn').onclick = () => beginWorkspacePicker();
window.addEventListener('pagehide', () => {
  pickerPageLeaving = true;
  if (workspacePicker) {
    clearTimeout(workspacePicker.retry);
    clearTimeout(workspacePicker.connectionTimer);
    workspacePicker.connectionTimer = null;
    clearTimeout(workspacePicker.cancelTimer);
    workspacePicker.cancelTimer = null;
    workspacePicker.socket?.close();
  }
});
window.addEventListener('pageshow', () => {
  if (!pickerPageLeaving) return;
  pickerPageLeaving = false;
  if (workspacePicker) {
    connectWorkspacePicker(workspacePicker);
    if (workspacePicker.cancelRequested) workspacePicker.cancelTimer = setTimeout(() => disconnectWorkspacePicker(workspacePicker), 5000);
  }
});

function restoreWorkspacePicker(workspaceReady) {
  const token = sessionStorage.getItem(pickerStorageKey);
  if (!token) return;
  openWorkspaceDialog();
  const input = $('#workspace-path-input');
  const savedPath = sessionStorage.getItem(pickerStorageKey + '.path');
  if (savedPath !== null) input.value = savedPath;
  const initialPath = input.value;
  let edited = false;
  const markEdited = () => { edited = true; };
  input.addEventListener('input', markEdited);
  beginWorkspacePicker(token, true);
  workspaceReady.then(() => {
    input.removeEventListener('input', markEdited);
    // Fill only missing initial data, never a selected path or a user's draft.
    if (savedPath === null && !edited && input.value === initialPath && !initialPath) input.value = state.workspaceRoot;
  });
}

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
$('#load-file').onclick = openFileDialog;

const removeFocusBtn = $('#remove-focus-file');
if (removeFocusBtn) {
  removeFocusBtn.onclick = (event) => {
    event.stopPropagation();
    clearFocusFile();
  };
}
send.onclick = sendMessage;
stop.onclick = () => state.controller?.abort();
prompt.oninput = autoResize;
prompt.onkeydown = (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
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
document.body.classList.toggle('mobile-layout', window.innerWidth <= 720);
setSidebarCollapsed(localStorage.getItem('helios.sidebarCollapsed') === 'true');
refreshIcons();
health();
const workspaceReady = loadWorkspace();
newChat();
loadProfiles();
setupPricingAccordionAnimation();

restoreWorkspacePicker(workspaceReady);
