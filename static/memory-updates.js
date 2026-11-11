/* Subscribe to the selected conversation's background memory updates and describe their notices. */

const notices = {
  updating: { message: 'Updating conversation memory', tone: 'progress' },
  updated: { message: 'Memory updated', tone: 'success' },
  not_updated: { message: 'Conversation memory was not updated', tone: 'info' }
};

export function createMemoryUpdates(onNotice) {
  let conversationId = null;
  let socket = null;
  let reconnectTimer = null;

  function close() {
    clearTimeout(reconnectTimer);
    const previous = socket;
    socket = null;
    previous?.close();
  }

  function connect() {
    if (!conversationId || socket || typeof WebSocket !== 'function') return;
    const id = conversationId;
    const url = new URL(`/api/conversations/${encodeURIComponent(id)}/memory/updates`, location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    const current = new WebSocket(url);
    socket = current;
    current.onmessage = event => {
      if (socket !== current) return;
      let phase;
      try { phase = JSON.parse(event.data)?.phase; }
      catch { phase = null; }
      const notice = typeof phase === 'string' && Object.hasOwn(notices, phase) ? notices[phase] : null;
      onNotice(id, notice);
    };
    current.onclose = event => {
      if (socket !== current) return;
      socket = null;
      onNotice(id, null);
      if (event.code !== 1000 && event.code !== 1008) reconnectTimer = setTimeout(connect, 2000);
    };
  }

  function selectConversation(id) {
    if (id === conversationId) return;
    close();
    conversationId = id;
    connect();
  }

  window.addEventListener('pagehide', close);
  window.addEventListener('pageshow', event => {
    if (event.persisted) {
      onNotice(conversationId, null);
      connect();
    }
  });
  return { selectConversation };
}
