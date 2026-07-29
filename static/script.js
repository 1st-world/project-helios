const state = {
  conversationId: null,
  controller: null,
  workspaceFile: null,
  workspaceRoot: '',
  generating: false
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

// Helper & Utility Functions
function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 5000);
}
function refreshIcons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { 'stroke-width': 1.8 } });
}
function escapeHtml(value) {
  const div = document.createElement('div');
  div.textContent = value;
  return div.innerHTML;
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
  const toggle = $('#sidebar-toggle'); toggle.innerHTML = `<i data-lucide="${collapsed ? 'panel-left-open' : 'panel-left-close'}"></i>`;
  toggle.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar'; toggle.setAttribute('aria-label', toggle.title);
  localStorage.setItem('helios.sidebarCollapsed', String(collapsed));
  refreshIcons();
}

// UI Rendering
function appendMessage(role, content = '', messageIndex = null, shouldScroll = true, smooth = false) {
  chat.querySelector('.empty-state')?.remove();
  const el = document.createElement('article');
  el.className = `message ${role}`;
  if (role === 'assistant') {
    el.innerHTML = `<div class="avatar"><i data-lucide="sparkles"></i></div><div class="message-body"><div class="message-text"></div></div>`;
  } else {
    el.innerHTML = `<div class="message-body"><div class="message-text"></div></div>`;
  }
  const body = el.querySelector('.message-body');
  const textEl = el.querySelector('.message-text');
  const render = (text) => {
    textEl.innerHTML = role === 'assistant' ? marked.parse(text) : escapeHtml(text).replace(/\n/g, '<br>');
  };
  render(content);
  const actions = document.createElement('div');
  actions.className = 'message-actions';
  if (role === 'user' && Number.isInteger(messageIndex)) {
    const edit = document.createElement('button');
    edit.className = 'message-action-btn';
    edit.type = 'button';
    edit.innerHTML = '<i data-lucide="pencil"></i>';
    edit.setAttribute('title', 'Edit message');
    edit.setAttribute('aria-label', 'Edit message');
    edit.onclick = () => editUserMessage(messageIndex, textEl.innerText || content);
    actions.append(edit);
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
      toast('Copied to clipboard!');
    } catch {
      toast('Failed to copy.');
    }
  };
  actions.append(copy);
  body.append(actions);
  chat.append(el);
  refreshIcons();
  if (shouldScroll) scrollDown(smooth);
  return { el, render };
}

function updateUsage(data) {
  $('#usage-panel').classList.remove('hidden');
  $('#input-tokens').textContent = data.input_tokens;
  $('#output-tokens').textContent = data.output_tokens;
  $('#total-tokens').textContent = data.total_tokens;
  $('#response-time').textContent = data.response_time_ms;
  $('#estimated-cost').textContent = Number(data.estimated_cost).toFixed(6);
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

function populateDeployments(data) {
  const select = $('#deployment-select');
  select.innerHTML = '';
  const deployments = data.deployments?.length ? data.deployments : [data.deployment || 'No deployment configured'];
  deployments.forEach((deployment) => {
    const option = document.createElement('option');
    option.value = deployment;
    option.textContent = deployment;
    option.selected = deployment === data.deployment;
    select.append(option);
  });
  $('#azure-endpoint').value = data.endpoint || '';
  $('#azure-api-version').value = data.api_version || '';
  $('#azure-deployment').value = data.deployment || '';
  $('#azure-deployments').value = (data.deployments || []).join(', ');
}

// API & Event Handlers
function onStart(event) { state.conversationId = event.conversation_id; }
function onDelta(event, assistant, answer) {
  answer.value += event.text;
  assistant.render(answer.value);
  scrollDown(true);
}
function onFinish() { loadConversations(); }
function onError(message) { toast(message); }
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
  if (!response.ok) return toast('Could not open conversation.');
  const data = await response.json();
  state.conversationId = data.id;
  chat.innerHTML = '';
  data.messages.forEach((message, index) => appendMessage(message.role, message.content, index, index === data.messages.length - 1, false));
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
  if (!title.trim()) return toast('A conversation title is required.');
  const response = await fetch(`/api/conversations/${item.id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title })
  });
  if (!response.ok) return toast((await response.json()).detail || 'Could not rename conversation.');
  loadConversations();
}

async function deleteConversation(item) {
  if (!window.confirm(`Delete "${item.title}"? This cannot be undone.`)) return;
  const response = await fetch(`/api/conversations/${item.id}`, { method: 'DELETE' });
  if (!response.ok) return toast((await response.json()).detail || 'Could not delete conversation.');
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
    toast('Could not load workspace.');
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
  if (!content.trim()) return toast('Message cannot be empty.');
  const response = await fetch(`/api/conversations/${state.conversationId}/messages/${messageIndex}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content })
  });
  if (!response.ok) return toast((await response.json()).detail || 'Could not edit message.');
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
      body: JSON.stringify({ prompt: text, conversation_id: state.conversationId, workspace_file: state.workspaceFile, regenerate_message_index: options.regenerateMessageIndex }),
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
        if (event.type === 'usage') updateUsage(event.usage);
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
    if (state.conversationId) { await openConversation(state.conversationId) } else loadConversations();
  }
}

async function loadAzureSettings() {
  try {
    const data = await fetch('/api/settings/azure').then((response) => response.json());
    populateDeployments(data);
  } catch {
    toast('Could not load Azure OpenAI settings.');
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
$('#open-settings').onclick = () => $('#settings-dialog').showModal();

document.querySelectorAll('[data-close-dialog]').forEach((button) => {
  button.onclick = () => $(`#${button.dataset.closeDialog}`).close();
});

$('#workspace-form').onsubmit = async (event) => {
  event.preventDefault();
  const path = $('#workspace-path-input').value.trim();
  const response = await fetch('/api/workspace/root', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path })
  });
  if (!response.ok) return toast((await response.json()).detail || 'Could not open workspace folder.');
  state.workspaceFile = null;
  $('#file-chip').classList.add('hidden');
  $('#workspace-dialog').close();
  await loadWorkspace();
};
$('#azure-settings-form').onsubmit = async (event) => {
  event.preventDefault();
  const deployments = $('#azure-deployments').value.split(',').map((item) => item.trim()).filter(Boolean);
  const response = await fetch('/api/settings/azure', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      api_key: $('#azure-api-key').value || null,
      endpoint: $('#azure-endpoint').value || null,
      api_version: $('#azure-api-version').value || null,
      deployment: $('#azure-deployment').value,
      deployments
    })
  });
  if (!response.ok) return toast((await response.json()).detail || 'Could not save Azure OpenAI settings.');
  $('#azure-api-key').value = '';
  $('#settings-dialog').close();
  populateDeployments(await response.json());
  health();
};
$('#deployment-select').onchange = async (event) => {
  const response = await fetch('/api/settings/azure', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ deployment: event.target.value })
  });
  if (!response.ok) return toast('Could not switch deployment.');
  populateDeployments(await response.json());
  health();
};
$('#load-file').onclick = () => {
  if (!state.workspaceFile) {
    toast('Select a text file in the Workspace panel first.')
  } else {
    toast(`Attached for the next message: ${state.workspaceFile}`)
  };
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

// Initial setup
setSidebarCollapsed(localStorage.getItem('helios.sidebarCollapsed') === 'true');
refreshIcons();
health();
loadWorkspace();
newChat();
loadAzureSettings();
