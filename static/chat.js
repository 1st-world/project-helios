/* Own conversation state, render messages and request feedback, and coordinate reply streaming and regeneration. */

import { $, closeMobileSidebar, escapeHtml, refreshIcons, toast } from './ui.js';
import { describeContextWarning, describeRequestError } from './chat-feedback.js';
import { describeAttachmentNotes } from './attachments.js';

export function createChat({ getActiveProfileId, getAttachmentFiles, onAttachmentMetadata, clearFocusFiles, onGeneratingChange }) {
  const state = {
    conversationId: null,
    conversationVersion: null,
    controller: null,
    generating: false,
    showConversationUsage: true,
    // Keep the latest request's feedback per conversation without storing estimates as transcript usage.
    requestFeedback: new Map()
  };

  const chat = $('#chat');
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
  usageFooter.setAttribute('aria-label', 'Conversation usage preview');
  usageFooter.setAttribute('aria-live', 'off');
  usageFooter.innerHTML = `
    <div class="conversation-usage-heading"><i data-lucide="chart-no-axes-combined"></i><strong>Conversation usage</strong><span>Preview</span></div>
    <dl class="conversation-usage-metrics">
      <div><dt>Calls</dt><dd>—</dd></div>
      <div><dt>Input / output</dt><dd>— / —</dd></div>
      <div><dt>Total tokens</dt><dd>—</dd></div>
      <div><dt>Estimated cost</dt><dd>—</dd></div>
    </dl>
    <p>Cumulative totals, including regeneration and memory summaries, are not available yet.</p>
  `;
  const emptyChatMarkup = `
    <div class="empty-state">
      <span><i data-lucide="sparkles"></i></span>
      <h1>How can I help?</h1>
      <p>Use your Azure OpenAI deployment to explore and work with local project files.</p>
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
    if (!usageFooter.hidden) {
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

  function renderRequestFeedback() {
    const items = state.requestFeedback.get(state.conversationId) || [];
    feedbackRegion.replaceChildren();
    if (!items.length) {
      feedbackRegion.remove();
      return;
    }
    items.forEach(({ type, title, message, details }) => {
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
      feedbackRegion.append(notice);
    });
    chat.insertBefore(feedbackRegion, usageFooter.parentElement === chat ? usageFooter : null);
  }

  function addRequestFeedback(request, type, feedback) {
    const wasNearBottom = isNearBottom();
    request.items.push({ type, ...feedback });
    state.requestFeedback.set(request.conversationId, request.items);
    if (request.conversationId === state.conversationId) {
      renderRequestFeedback();
      if (wasNearBottom) scrollDown(false);
    }
  }

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
    const tokenDetails = [
      ['cache_read_tokens', 'cache read'],
      ['cache_write_tokens', 'cache write'],
      ['reasoning_tokens', 'reasoning'],
    ].filter(([name]) => Number.isInteger(meta[name]) && meta[name] > 0)
      .map(([name, label]) => `${meta[name].toLocaleString()} ${label}`);
    if (tokenDetails.length) {
      parts.push(`<span class="message-meta-item" title="Cache tokens are included in input; reasoning tokens are included in output.">· ${tokenDetails.join(' / ')}</span>`);
    }
    if (meta.response_time_ms != null) {
      parts.push(`<span class="message-meta-item">· ${(meta.response_time_ms / 1000).toFixed(1)}s</span>`);
    }
    if (meta.estimated_cost !== null && meta.estimated_cost !== undefined) {
      const qualification = meta.cost_status === 'complete' ? '' : meta.cost_status === 'partial' ? ' (Partial estimate)' : ' (Completeness unknown)';
      parts.push(`<span class="message-meta-item message-meta-cost">· $${Number(meta.estimated_cost).toFixed(6)}${qualification}</span>`);
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
      edit.onclick = () => editUserMessage(messageIndex, content);
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

    const next = feedbackRegion.parentElement === chat ? feedbackRegion : usageFooter.parentElement === chat ? usageFooter : null;
    chat.insertBefore(el, next);
    refreshIcons();
    if (shouldScroll) scrollDown(smooth);
    return { el, render, setMeta };
  }

  function onStart(event) {
    state.conversationId = event.conversation_id;
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

  function onError(message) {
    toast(message, 'error');
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
      appendMessage(message.role, message.content, index, false, false, message, regeneration);
    });
    renderRequestFeedback();
    updateConversationUsage();
    if (preserveScroll && !wasNearBottom) { chat.scrollTop = savedScrollTop; }
    else scrollDown(false);
    loadConversations();
  }

  function newChat() {
    closeMobileSidebar();
    state.conversationId = null;
    state.requestFeedback.delete(null);
    clearFocusFiles();
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
    state.requestFeedback.delete(item.id);
    if (state.conversationId === item.id) newChat();
    else loadConversations();
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
    const text = options.prompt ?? prompt.value.trim();
    if (!text || state.generating) return;
    const requestFeedback = { conversationId: state.conversationId, items: [] };
    state.requestFeedback.delete(state.conversationId);
    renderRequestFeedback();
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
    let contextPolicy = null;
    let requestStarted = false;
    try {
      state.controller = new AbortController();
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: text,
          conversation_id: state.conversationId,
          attachment_files: getAttachmentFiles(),
          profile_id: getActiveProfileId(),
          regenerate_message_index: options.regenerateMessageIndex,
          expected_conversation_version: options.expectedConversationVersion
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
            requestStarted = true;
            requestFeedback.conversationId = event.conversation_id;
            if (!options.preserveDraft || state.conversationId === requestConversationId) onStart(event);
          }
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
                cache_read_tokens: event.usage.cache_read_tokens,
                cache_write_tokens: event.usage.cache_write_tokens,
                reasoning_tokens: event.usage.reasoning_tokens,
                usage_status: event.usage.usage_status,
                cost_status: event.usage.cost_status,
              });
            }
          }
          if (event.type === 'done') onFinish();
          if (event.type === 'error') {
            const feedback = describeRequestError(event, 'Unable to generate a response.');
            throw Object.assign(new Error(feedback.message), { feedback });
          }
        }
      }
    } catch (error) {
      if (error.name === 'AbortError') {
        onAbort(assistant, answer);
      } else {
        if (!requestStarted && !options.preserveDraft && state.conversationId === requestConversationId && !prompt.value) {
          prompt.value = text;
          autoResize();
        }
        assistant.render(answer.value || '_Unable to generate a response._');
        addRequestFeedback(requestFeedback, 'error', error.feedback || describeRequestError({ message: error.message }));
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

  function init() {
    $('#new-chat').onclick = newChat;

    send.onclick = () => sendMessage();
    stop.onclick = () => state.controller?.abort();
    prompt.oninput = autoResize;
    prompt.onkeydown = (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        sendMessage();
      }
    };
  }

  return { init, newChat, setUsageVisibility, isGenerating: () => state.generating };
}
