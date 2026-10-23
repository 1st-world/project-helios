/* Own conversation state, render messages and request feedback, and coordinate reply streaming and regeneration. */

import { $, closeMobileSidebar, escapeHtml, refreshIcons, toast } from './ui.js';
import { describeContextWarning, describeRequestError } from './chat-feedback.js';
import { describeWorkRun } from './work-run-status.js';
import { describeAttachmentNotes } from './attachments.js';
import { calendarDate, formatMessageTimestamp, getTimezone } from './timezone.js';
import { createDisclosure } from './disclosure.js';
import { showActionDialog } from './dialogs.js';
import { createResponseFooter } from './response-footer.js';

export function createChat({ getActiveProfileId, getAttachmentSelection, restoreAttachments, onAttachmentsSaved, getWorkspaceContext = () => ({}), selectWorkspace, startWorkspaceDraft, adoptWorkspace, onAttachmentMetadata, clearFocusFiles, onGeneratingChange, onConversationChange, openSettings, exploration }) {
  const state = {
    conversationId: null,
    conversationVersion: null,
    conversationLoadVersion: 0,
    controller: null,
    generating: false,
    showConversationUsage: true,
    showResponseUsage: true,
    // Keep the latest request's feedback per conversation without storing estimates as transcript usage.
    requestFeedback: new Map()
  };

  const chat = $('#chat');
  const messageMetadata = new WeakMap();
  const responseFooters = new Map();
  let timestampRefreshTimer = null;
  let timestampDay = '';
  let timestampTimezone = '';
  let messageEditor = null;
  const prompt = $('#prompt');
  const send = $('#send');
  const stop = $('#stop');
  const feedbackRegion = document.createElement('aside');
  feedbackRegion.className = 'chat-request-notices';
  feedbackRegion.setAttribute('aria-label', 'Latest request status');
  feedbackRegion.setAttribute('aria-live', 'polite');
  const usageFooter = document.createElement('aside');
  usageFooter.className = 'conversation-usage';
  usageFooter.id = 'conversation-usage';
  usageFooter.setAttribute('aria-label', 'Recorded conversation usage');
  usageFooter.setAttribute('aria-live', 'off');
  usageFooter.innerHTML = `
    <div class="conversation-usage-heading"><i data-lucide="chart-no-axes-combined"></i><strong>Conversation usage</strong><button class="quiet-btn" type="button" data-usage-retry aria-label="Retry conversation usage" hidden><i data-lucide="refresh-cw"></i> Retry</button></div>
    <p data-usage-state role="status">Select a conversation to see recorded usage.</p>
    <dl class="conversation-usage-metrics">
      <div><dt>Calls</dt><dd data-usage-field="calls">—</dd></div>
      <div><dt>Input / output</dt><dd data-usage-field="input_output">— / —</dd></div>
      <div><dt>Total tokens</dt><dd data-usage-field="total_tokens">—</dd></div>
      <div><dt>Cache read / write</dt><dd data-usage-field="cache">— / —</dd></div>
      <div><dt>Reasoning</dt><dd data-usage-field="reasoning_tokens">—</dd></div>
      <div><dt>Memory-summary calls</dt><dd data-usage-field="summaries">—</dd></div>
      <div><dt>Estimated cost (USD)</dt><dd data-usage-field="cost">—</dd></div>
    </dl>
    <p data-usage-completeness></p>
    <details class="usage-details"><summary><i data-lucide="chevron-down" class="usage-details-chevron" aria-hidden="true"></i><span>Usage and coverage details</span></summary><div class="usage-details-body"><p data-usage-notes></p></div></details>
  `;
  const footerDetails = usageFooter.querySelector('.usage-details');
  const footerDisclosure = createDisclosure(footerDetails, footerDetails.querySelector(':scope > .usage-details-body'));
  const footerResizeObserver = new ResizeObserver(() => {
    if (usageFooter.isConnected && !usageFooter.hidden && isNearBottom()) scrollDown(false);
  });
  footerResizeObserver.observe(usageFooter);
  const emptyChatMarkup = `
    <div class="empty-state">
      <span><i data-lucide="sparkles"></i></span>
      <h1>How can I help?</h1>
      <p>Use your Microsoft Foundry deployment to explore and work with local project files.</p>
    </div>
  `;

  marked.use(markedHighlight.markedHighlight({
    highlight(code, language) {
      return language && hljs.getLanguage(language) ? hljs.highlight(code, { language }).value : hljs.highlightAuto(code).value;
    }
  }));

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
    const wasNearBottom = isNearBottom();
    state.generating = value;
    send.disabled = value;
    stop.classList.toggle('hidden', !value);
    onGeneratingChange(value);
    chat.querySelectorAll('[data-mutates-conversation]').forEach((button) => { button.disabled = value; });
    updateConversationUsage();
    if (!value && wasNearBottom) scrollDown(false);
  }

  function updateConversationUsage() {
    usageFooter.hidden = !state.showConversationUsage || !state.conversationId || state.generating || !chat.querySelector('.message');
    if (usageFooter.hidden) {
      footerDisclosure.finish();
    } else {
      chat.append(usageFooter);
      refreshIcons();
    }
  }

  function setUsageVisibility(visible) {
    const wasNearBottom = isNearBottom();
    state.showConversationUsage = visible;
    updateConversationUsage();
    if (wasNearBottom) scrollDown(false);
  }

  function setResponseUsageVisibility(visible) {
    const wasNearBottom = isNearBottom();
    state.showResponseUsage = visible;
    exploration?.setUsageVisibility(visible);
    responseFooters.forEach(footer => footer.setUsageVisibility(visible));
    if (wasNearBottom) scrollDown(false);
  }

  function renderUsageContent(render) {
    const wasNearBottom = isNearBottom();
    render();
    if (wasNearBottom) scrollDown(false);
  }

  function renderRequestFeedback() {
    const items = state.requestFeedback.get(state.conversationId) || [];
    feedbackRegion.replaceChildren();
    if (!items.length) {
      feedbackRegion.remove();
      return;
    }
    items.forEach(({ type, title, message, details, recovery, actions = [], sourceCreatedAt }) => {
      const notice = document.createElement('div');
      notice.className = `chat-request-notice ${type}`;
      const heading = document.createElement('strong');
      heading.textContent = title;
      const text = document.createElement('p');
      text.textContent = message;
      notice.append(heading, text);
      if (details.length) {
        const explanation = document.createElement('p');
        explanation.className = 'chat-request-notice-details';
        explanation.textContent = details.join('\n');
        notice.append(explanation);
      }
      const controls = document.createElement('div');
      controls.className = 'chat-request-notice-actions';
      const labels = { question: 'Review question', attachments: 'Review attachments', connections: 'Connection settings', usage: 'Review usage', limits: 'Review input budget', output: 'Review output limits' };
      actions.forEach(action => {
        if (!labels[action]) return;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'quiet-btn';
        button.dataset.mutatesConversation = '';
        button.disabled = state.generating;
        button.textContent = labels[action];
        button.onclick = () => {
          if (state.generating) return;
          if (action === 'attachments') $('#load-file').click();
          else if (action === 'question') {
            const source = sourceCreatedAt && [...chat.querySelectorAll('.message.user')].find(node => messageMetadata.get(node)?.created_at === sourceCreatedAt);
            if (source) {
              source.scrollIntoView({ block: 'center', behavior: 'smooth' });
              source.querySelector('[aria-label="Edit message"]')?.click();
            } else prompt.focus();
          } else openSettings?.({ connections: 'connections', usage: 'usage', limits: 'context', output: 'context' }[action]);
        };
        controls.append(button);
      });
      if (recovery) {
        const action = document.createElement('button');
        action.type = 'button';
        action.className = 'quiet-btn';
        action.dataset.mutatesConversation = '';
        action.disabled = state.generating || recovery.used === true;
        action.textContent = 'Summarize once & retry…';
        action.onclick = () => recoverResponse(recovery);
        controls.append(action);
      }
      if (controls.childElementCount) notice.append(controls);
      feedbackRegion.append(notice);
    });
    chat.insertBefore(feedbackRegion, usageFooter.parentElement === chat ? usageFooter : null);
  }

  function addRequestFeedback(request, type, feedback) {
    const wasNearBottom = isNearBottom();
    request.items.push({ type, ...feedback, sourceCreatedAt: request.sourceCreatedAt });
    state.requestFeedback.set(request.conversationId, request.items);
    if (request.conversationId === state.conversationId) {
      renderRequestFeedback();
      if (wasNearBottom) scrollDown(false);
    }
  }

  function renderMessageTimestamp(el) {
    if (el.classList.contains('assistant')) return responseFooters.get(el)?.refreshTimestamp();
    const meta = messageMetadata.get(el) || {};
    const label = el.querySelector('.message-timestamp');
    const timestamp = formatMessageTimestamp(meta.created_at);
    label.hidden = !meta.created_at;
    if (!timestamp) {
      label.textContent = 'Time unknown';
      label.removeAttribute('title');
      label.removeAttribute('aria-label');
      return;
    }
    const time = document.createElement('time');
    time.dateTime = meta.created_at;
    time.textContent = timestamp.label;
    label.replaceChildren(time);
    const description = `${el.classList.contains('assistant') ? 'Response' : 'Message'} saved: ${timestamp.detail}`;
    label.title = description;
    label.setAttribute('aria-label', description);
  }

  function disposeMessageDisclosures(root = chat) {
    for (const [message, footer] of responseFooters) {
      if (message !== root && !root.contains(message)) continue;
      footer.destroy();
      responseFooters.delete(message);
    }
  }

  function refreshMessageTimestamps(force = false) {
    clearTimeout(timestampRefreshTimer);
    if (document.hidden) return;
    const day = calendarDate();
    const timezone = getTimezone();
    if (force || day !== timestampDay || timezone !== timestampTimezone) {
      const wasNearBottom = isNearBottom();
      chat.querySelectorAll('.message.user, .message.assistant').forEach(renderMessageTimestamp);
      exploration?.refreshTimestamps();
      timestampDay = day;
      timestampTimezone = timezone;
      if (wasNearBottom) scrollDown(false);
    }
    // Check minute boundaries rather than assuming calendar days last 24 hours across timezone changes and DST.
    timestampRefreshTimer = setTimeout(refreshMessageTimestamps, 60000 - Date.now() % 60000);
  }

  function appendMessage(role, content = '', messageIndex = null, shouldScroll = true, smooth = false, meta = null, regeneration = null) {
    chat.querySelector('.empty-state')?.remove();
    const el = document.createElement('article');
    el.className = `message ${role}`;
    if (Number.isInteger(messageIndex)) el.dataset.messageIndex = String(messageIndex);
    const avatar = role === 'assistant' ? '<div class="avatar"><i data-lucide="sparkles"></i></div>' : '';
    const timestamp = '<span class="message-timestamp" hidden></span>';
    el.innerHTML = `${avatar}<div class="message-body"><div class="message-text"></div><div class="message-footer"><div class="message-footer-main"><div class="message-actions">${role === 'user' ? timestamp : ''}<div class="message-action-buttons"></div></div></div></div></div>`;
    const textEl = el.querySelector('.message-text');
    const footer = el.querySelector('.message-footer');
    const responseFooter = role === 'assistant' ? createResponseFooter(footer, { onResize() {
      if (state.showResponseUsage && isNearBottom()) scrollDown(false);
    } }) : null;
    if (responseFooter) responseFooters.set(el, responseFooter);
    const actionsEl = el.querySelector('.message-action-buttons');

    const render = (text) => {
      textEl.innerHTML = role === 'assistant' ? renderMarkdown(text) : escapeHtml(text).replace(/\n/g, '<br>');
    };
    render(content);

    const setMeta = (newMeta) => {
      const current = { ...messageMetadata.get(el), ...newMeta, ...newMeta.response_metadata };
      messageMetadata.set(el, current);
      if (current.run_id) el.dataset.runId = current.run_id;
      if (responseFooter) {
        responseFooter.update(current);
        responseFooter.setUsageVisibility(state.showResponseUsage);
      } else renderMessageTimestamp(el);
      refreshIcons();
    };
    if (meta) setMeta(meta);

    if (role === 'user' && Number.isInteger(messageIndex)) {
      const edit = document.createElement('button');
      edit.className = 'message-action-btn';
      edit.type = 'button';
      edit.dataset.mutatesConversation = '';
      edit.disabled = state.generating;
      edit.innerHTML = '<i data-lucide="pencil"></i>';
      edit.setAttribute('title', 'Edit message');
      edit.setAttribute('aria-label', 'Edit message');
      edit.onclick = () => editUserMessage(messageIndex, content, el, edit);
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

    const next = chat.querySelector('.exploration-history') || (feedbackRegion.parentElement === chat ? feedbackRegion : usageFooter.parentElement === chat ? usageFooter : null);
    chat.insertBefore(el, next);
    refreshIcons();
    if (shouldScroll) scrollDown(smooth);
    return { el, render, setMeta };
  }

  function onStart(event) {
    state.conversationId = event.conversation_id;
    adoptWorkspace?.(state.conversationId);
  }

  function onDelta(event, assistant, answer) {
    const wasNearBottom = isNearBottom();
    answer.value += event.text;
    assistant.render(answer.value);
    if (wasNearBottom) { scrollDown(false); }
  }

  function onFinish() {
    loadConversations();
  }

  function onAbort(assistant, answer) {
    assistant.render(answer.value || '_Generation stopped._');
  }

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
    if (messageEditor?.saving && !preserveScroll) return;
    const loadVersion = ++state.conversationLoadVersion;
    closeMobileSidebar();
    const response = await fetch(`/api/conversations/${id}`);
    if (loadVersion !== state.conversationLoadVersion) return false;
    if (!response.ok) return toast('Could not open conversation.', 'error');
    const data = await response.json();
    if (loadVersion !== state.conversationLoadVersion) return false;
    messageEditor = null;
    state.conversationId = data.id;
    state.conversationVersion = data.version;
    selectWorkspace?.(data.id, { ...data.workspace, available: data.workspace_available }, { preserveSelection: preserveScroll });
    if (!preserveScroll) {
      exploration?.resetChoice();
      const latestUser = data.messages.findLast(message => message.role === 'user');
      restoreAttachments(latestUser?.attachment_sources || []);
    }
    const savedScrollTop = chat.scrollTop;
    const wasNearBottom = isNearBottom();
    footerDisclosure.finish();
    disposeMessageDisclosures();
    exploration?.prepareRender();
    chat.innerHTML = '';
    const messageTargets = data.messages.map((message, index) => {
      const source = data.messages[index - 1];
      const regeneration = message.role === 'assistant' && source?.role === 'user'
        ? { userIndex: index - 1, content: source.content, hasFollowing: index < data.messages.length - 1 }
        : null;
      const view = appendMessage(message.role, message.content, index, false, false, message, regeneration);
      return { ...message, index, element: view.el };
    });
    exploration?.show(data.id, messageTargets);
    renderRequestFeedback();
    updateConversationUsage();
    onConversationChange(state.conversationId);
    if (preserveScroll && !wasNearBottom) { chat.scrollTop = savedScrollTop; }
    else scrollDown(false);
    exploration?.restorePosition({ preserveScroll: preserveScroll && !wasNearBottom });
    loadConversations();
    return true;
  }

  function newChat() {
    if (messageEditor?.saving) return;
    ++state.conversationLoadVersion;
    messageEditor = null;
    closeMobileSidebar();
    state.conversationId = null;
    onConversationChange(null);
    state.requestFeedback.delete(null);
    clearFocusFiles();
    exploration?.resetChoice();
    exploration?.show(null);
    footerDisclosure.finish();
    disposeMessageDisclosures();
    chat.innerHTML = emptyChatMarkup;
    loadConversations();
    prompt.focus();
    return startWorkspaceDraft?.();
  }

  async function renameConversation(item) {
    const renamed = await showActionDialog({ title: 'Rename conversation', description: 'Choose a title to find this conversation easily.',
      confirmLabel: 'Save title', field: { label: 'Conversation title', value: item.title, maxLength: 100 },
      async onConfirm(title) {
        if (title === item.title) return;
        const response = await fetch(`/api/conversations/${item.id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title })
        });
        if (!response.ok) throw new Error((await readResponseError(response)).message);
      }
    });
    if (renamed) loadConversations();
  }

  async function deleteConversation(item) {
    if (messageEditor?.saving) return;
    const deleted = await showActionDialog({ title: 'Delete conversation?', description: `Delete “${item.title}”? This cannot be undone.`,
      confirmLabel: 'Delete conversation', danger: true,
      async onConfirm() {
        const response = await fetch(`/api/conversations/${item.id}`, { method: 'DELETE' });
        if (!response.ok) throw new Error((await readResponseError(response)).message);
      }
    });
    if (!deleted) return;
    state.requestFeedback.delete(item.id);
    if (state.conversationId === item.id) newChat();
    else loadConversations();
  }

  function editUserMessage(messageIndex, currentContent, message, editButton) {
    if (state.generating || !state.conversationId) return;
    if (messageEditor) {
      messageEditor.input.focus();
      return;
    }
    const conversationId = state.conversationId;
    const form = document.createElement('form');
    form.className = 'message-editor';
    form.innerHTML = `
      <label for="message-edit-content">Edit message</label>
      <textarea id="message-edit-content" rows="5" aria-describedby="message-edit-help message-edit-error"></textarea>
      <p id="message-edit-help">Saving removes all following messages and generates a new response using the current Tools selection. Enter saves and regenerates; Shift+Enter adds a new line.</p>
      <p id="message-edit-error" role="alert" hidden></p>
      <div class="dialog-actions"><button type="button" class="quiet">Cancel</button><button type="submit" class="primary-btn">Save &amp; regenerate</button></div>
    `;
    const input = form.querySelector('textarea');
    const error = form.querySelector('[role="alert"]');
    const save = form.querySelector('[type="submit"]');
    const cancel = form.querySelector('[type="button"]');
    const editor = { input, saving: false };
    messageEditor = editor;
    input.value = currentContent;
    message.classList.add('editing');
    message.querySelector('.message-body').append(form);

    function closeEditor() {
      form.remove();
      message.classList.remove('editing');
      if (messageEditor === editor) messageEditor = null;
      if (editButton.isConnected) editButton.focus({ preventScroll: true });
    }

    function updateSave() {
      save.disabled = !input.value.trim() || input.value.trim() === currentContent.trim();
      error.hidden = true;
    }

    cancel.onclick = closeEditor;
    input.oninput = updateSave;
    form.onkeydown = event => {
      if (event.isComposing) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        if (!editor.saving) closeEditor();
      } else if (event.target === input && event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        if (!save.disabled) form.requestSubmit();
      }
    };
    form.onsubmit = async event => {
      event.preventDefault();
      const content = input.value.trim();
      if (editor.saving || state.generating || !content || content === currentContent.trim()) return;
      if (state.conversationId !== conversationId || !form.isConnected) return;
      editor.saving = true;
      input.disabled = save.disabled = cancel.disabled = true;
      save.textContent = 'Saving...';
      form.setAttribute('aria-busy', 'true');
      error.hidden = true;
      try {
        const response = await fetch(`/api/conversations/${conversationId}/messages/${messageIndex}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content })
        });
        if (!response.ok) throw new Error((await readResponseError(response)).message);
      } catch (failure) {
        editor.saving = false;
        input.disabled = save.disabled = cancel.disabled = false;
        save.textContent = 'Save & regenerate';
        form.setAttribute('aria-busy', 'false');
        error.textContent = failure.message || 'Could not save your message. Your edits are kept.';
        error.hidden = false;
        input.focus();
        return;
      }
      try {
        if (!await openConversation(conversationId, true)) throw new Error('Could not reload the saved message.');
        closeEditor();
        if (state.conversationId === conversationId) {
          sendMessage({ prompt: content, regenerateMessageIndex: messageIndex, appendUser: false,
            preserveDraft: true, expectedConversationVersion: state.conversationVersion });
        }
      } catch {
        closeEditor();
        toast('Message saved, but the conversation could not be reloaded. Reopen it before continuing.', 'error');
      }
    };
    updateSave();
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    form.scrollIntoView({ block: 'nearest' });
  }

  async function readResponseError(response) {
    const status = `HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ''}`;
    let payload = null;
    try {
      const body = await response.text();
      try {
        payload = JSON.parse(body);
      } catch {
        if (response.headers.get('content-type')?.toLowerCase().startsWith('text/plain')) payload = body;
      }
    } catch (error) {
      if (error.name === 'AbortError') throw error;
    }
    return describeRequestError(payload, 'Request failed.', status);
  }

  async function sendMessage(options = {}) {
    if (messageEditor) return toast('Save or cancel your message edit before sending.', 'info');
    const text = options.prompt ?? prompt.value.trim();
    if (!text || state.generating) return;
    let workspaceContext;
    let explorationContext;
    let attachmentSelection;
    const workspaceViewVersion = state.conversationLoadVersion;
    try {
      workspaceContext = options.recoveryId ? {} : await getWorkspaceContext();
      if (state.generating || workspaceViewVersion !== state.conversationLoadVersion) return;
      explorationContext = options.recoveryId ? {} : exploration?.selection() || {};
      attachmentSelection = options.recoveryId ? {} : await getAttachmentSelection({ regenerating: options.regenerateMessageIndex != null });
      if (state.generating || workspaceViewVersion !== state.conversationLoadVersion) return;
    }
    catch (error) {
      toast(error.message || 'Review the workspace before sending.', 'error');
      if (options.preserveDraft && state.conversationId) await openConversation(state.conversationId, true);
      return;
    }
    const requestFeedback = { conversationId: state.conversationId, items: [], sourceCreatedAt: options.sourceCreatedAt };
    state.requestFeedback.delete(state.conversationId);
    renderRequestFeedback();
    const user = options.appendUser !== false ? appendMessage('user', text) : null;
    if (!options.preserveDraft) {
      prompt.value = '';
      autoResize();
    }
    const requestConversationId = state.conversationId;
    const requestViewVersion = state.conversationLoadVersion;
    setGenerating(true);
    const assistant = appendMessage('assistant', '');
    assistant.el.querySelector('.message-text').innerHTML = '<span class="typing">Thinking</span>';
    const answer = { value: '' };
    let contextPolicy = null;
    let requestStarted = false;
    let workRunId = null;
    try {
      state.controller = new AbortController();
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: text,
          conversation_id: state.conversationId,
          ...attachmentSelection,
          ...workspaceContext,
          ...explorationContext,
          profile_id: getActiveProfileId(),
          regenerate_message_index: options.regenerateMessageIndex,
          expected_conversation_version: options.expectedConversationVersion,
          recovery_id: options.recoveryId,
          operation_id: options.operationId
        }),
        signal: state.controller.signal
      });
      if (!response.ok) {
        const feedback = await readResponseError(response);
        throw Object.assign(new Error(feedback.message), { feedback });
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const buffer = { value: '' };
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer.value += decoder.decode(value, { stream: true });
        const frames = buffer.value.split(/\r?\n\r?\n/);
        buffer.value = frames.pop();
        for (const frame of frames) {
          if (!frame.startsWith('data: ')) continue;
          const event = JSON.parse(frame.slice(6));
          if (event.type === 'start') {
            workRunId = event.run_id || null;
            if (workRunId) assistant.el.hidden = true;
            requestStarted = true;
            requestFeedback.conversationId = event.conversation_id;
            requestFeedback.sourceCreatedAt = event.user_created_at || requestFeedback.sourceCreatedAt;
            if (requestViewVersion === state.conversationLoadVersion) onStart(event);
            if (event.user_created_at) user?.setMeta({ created_at: event.user_created_at });
            if (user && Number.isInteger(event.user_index)) user.el.dataset.messageIndex = String(event.user_index);
          }
          if (state.conversationId === requestFeedback.conversationId) exploration?.progress(event, {
            user: user?.el || [...chat.querySelectorAll('.message.user')].find(message => Number(message.dataset.messageIndex) === (event.user_index ?? options.regenerateMessageIndex)),
            assistant: assistant.el
          });
          if (event.type === 'context_policy') contextPolicy = event;
          if (event.type === 'attachments' && Array.isArray(event.files)) {
            onAttachmentMetadata(event.files);
            const notes = describeAttachmentNotes(event.files);
            if (notes) addRequestFeedback(requestFeedback, 'info', notes);
          }
          if (event.type === 'context_warning') {
            addRequestFeedback(requestFeedback, 'warning', describeContextWarning(event, contextPolicy));
          }
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
                uncached_input_tokens: event.usage.uncached_input_tokens,
                cache_read_tokens: event.usage.cache_read_tokens,
                cache_write_tokens: event.usage.cache_write_tokens,
                reasoning_tokens: event.usage.reasoning_tokens,
                usage_status: event.usage.usage_status,
                cost_status: event.usage.cost_status,
              });
            }
          }
          if (event.type === 'done') {
            assistant.el.hidden = false;
            if (!options.recoveryId) onAttachmentsSaved();
            if (event.created_at || event.response_metadata) assistant.setMeta({ created_at: event.created_at, response_metadata: event.response_metadata });
            onFinish();
          }
          if (event.type === 'error') {
            const feedback = describeRequestError(event, 'Unable to generate a response.');
            throw Object.assign(new Error(feedback.message), { feedback, workRun: event.run_id ? { status: event.run_status, reason: event.code, failure: event } : null });
          }
        }
      }
    } catch (error) {
      if (error.name === 'AbortError') {
        if (workRunId) {
          disposeMessageDisclosures(assistant.el);
          assistant.el.remove();
          exploration?.progress({ type: 'cancel_requested', run_id: workRunId });
        } else onAbort(assistant, answer);
      } else {
        if (!requestStarted && !options.preserveDraft && requestViewVersion === state.conversationLoadVersion && state.conversationId === requestConversationId && !prompt.value) {
          prompt.value = text;
          autoResize();
        }
        if ((error.workRun || workRunId) && exploration) {
          disposeMessageDisclosures(assistant.el);
          assistant.el.remove();
          const outcome = describeWorkRun(error.workRun || { status: 'unavailable' });
          toast(outcome.label, outcome.tone === 'neutral' ? 'info' : outcome.tone);
        } else {
          assistant.render(answer.value || '_Unable to generate a response._');
          const feedback = error.feedback || describeRequestError({ message: error.message });
          const type = feedback.tone || 'error';
          addRequestFeedback(requestFeedback, type, feedback);
          toast(feedback.title, type);
        }
      }
    } finally {
      state.controller = null;
      try {
        if (state.conversationId && state.conversationId === requestFeedback.conversationId) {
          await openConversation(state.conversationId, true);
        } else {
          loadConversations();
        }
      } finally {
        setGenerating(false);
      }
    }
  }

  async function prepareExploration({ conversationId, source }) {
    if (state.generating || messageEditor) return toast('Finish the current response or message edit before preparing another exploration.', 'info');
    const viewVersion = state.conversationLoadVersion;
    const isCurrent = () => {
      if (state.generating || state.conversationId !== conversationId || viewVersion !== state.conversationLoadVersion || !source) return false;
      const message = [...chat.querySelectorAll('.message.user')].find(element => Number(element.dataset.messageIndex) === source.index);
      const metadata = message && messageMetadata.get(message);
      return metadata?.created_at === source.created_at && metadata?.content === source.content;
    };
    if (!isCurrent()) return toast('The original question is no longer available in this conversation branch.', 'info');
    const replacesDraft = prompt.value.trim() && prompt.value !== source.content;
    if (!await showActionDialog({ title: 'Prepare a new exploration?',
      description: 'Copy this question to the composer and select Explore project. Review the current Workspace, model, attachments and applied limits before sending. The saved attempt is not resumed, and nothing is sent until you press Send.'
        + (replacesDraft ? ' This replaces your current message draft.' : ''),
      confirmLabel: 'Prepare', danger: Boolean(replacesDraft) }) || !isCurrent()) return;
    if (!await exploration.selectForNextResponse(isCurrent) || !isCurrent()) return;
    prompt.value = source.content;
    autoResize();
    prompt.focus();
    toast('Exploration prepared. Review the current settings and press Send when ready.', 'info');
  }

  async function regenerateResponse({ userIndex, content, hasFollowing }) {
    if (state.generating || !state.conversationId || messageEditor) return;
    const conversationId = state.conversationId;
    const version = state.conversationVersion;
    if (!await showActionDialog({ title: 'Regenerate response?',
      description: (hasFollowing ? 'Once successful, the new response will replace this response and remove all following messages.'
        : 'Once successful, the new response will replace this response.')
        + ' Uses the original attachments unless you changed the selection in Attach or removed a file from the composer.'
        + ` ${exploration?.describeSelection() || 'Work tools are off for this response.'}`,
      confirmLabel: 'Regenerate', danger: hasFollowing })) return;
    if (state.generating || state.conversationId !== conversationId || state.conversationVersion !== version) return;
    // The server keeps the original branch until regeneration completes successfully.
    [...chat.querySelectorAll('.message')].slice(userIndex + 1).forEach((message) => {
      disposeMessageDisclosures(message);
      message.remove();
    });
    sendMessage({ prompt: content, regenerateMessageIndex: userIndex, appendUser: false,
      preserveDraft: true, expectedConversationVersion: state.conversationVersion });
  }

  async function recoverResponse(recovery) {
    if (state.generating || messageEditor || recovery.used || !state.conversationId) return;
    const conversationId = state.conversationId;
    const version = state.conversationVersion;
    const calls = Number.isSafeInteger(recovery.summary_call_limit) ? recovery.summary_call_limit : 'the configured number of';
    if (!await showActionDialog({ title: 'Summarize once and retry?',
      description: `Use ${recovery.profile_name || 'the failed connection'} to summarize older complete turns. This can make up to ${calls} paid summary calls and one paid reply request. Reuse the failed request's question, workspace, and attachments, and preserve its latest complete turn. The summary is used only for this attempt. Automatic memory and saved memory stay unchanged.${recovery.replaces_following ? ' If successful, the new response replaces the selected response and removes all following messages.' : ''}`,
      confirmLabel: 'Allow summary & retry', danger: recovery.replaces_following === true })) return;
    if (state.generating || state.conversationId !== conversationId || state.conversationVersion !== version || recovery.used) return;
    recovery.used = true;
    if (Number.isSafeInteger(recovery.user_index) && recovery.user_index >= 0) {
      [...chat.querySelectorAll('.message')].slice(recovery.user_index + 1).forEach(message => {
        disposeMessageDisclosures(message);
        message.remove();
      });
    }
    sendMessage({ prompt: 'Recover failed request', recoveryId: recovery.id, operationId: crypto.randomUUID(),
      appendUser: false, preserveDraft: true,
      sourceCreatedAt: messageMetadata.get([...chat.querySelectorAll('.message')][recovery.user_index])?.created_at });
  }

  function init() {
    $('#new-chat').onclick = newChat;

    send.onclick = () => sendMessage();
    stop.onclick = () => state.controller?.abort();
    prompt.oninput = autoResize;
    document.addEventListener('text-size-change', autoResize);
    document.addEventListener('timezone-change', () => refreshMessageTimestamps(true));
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) footerDisclosure.finish();
      refreshMessageTimestamps();
    });
    window.addEventListener('focus', () => refreshMessageTimestamps());
    refreshMessageTimestamps();
    prompt.onkeydown = (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        sendMessage();
      }
    };
  }

  return { init, newChat, prepareExploration, setUsageVisibility, setResponseUsageVisibility, renderUsageContent, usageFooter, isGenerating: () => state.generating,
    setWorkspaceVersion(id, version) { if (id === state.conversationId && Number.isSafeInteger(version)) state.conversationVersion = version; }
  };
}
