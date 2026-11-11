/* Display and announce the selected conversation's status above the composer. */

import { setText } from './ui.js';

export function createConversationNotice(region, render) {
  const notice = region.querySelector('.composer-conversation-notice');
  const label = notice.querySelector('span');
  const status = region.querySelector('[role="status"]');
  let conversationId = null;

  function show(id, update) {
    if (!id || id !== conversationId) return;
    const { message = '', tone = 'info' } = update ?? {};
    if (status.textContent === message && (!message || notice.dataset.tone === tone)) return;
    render(() => {
      if (message) {
        notice.dataset.tone = tone;
        setText(label, message);
      }
      setText(status, message);
      notice.hidden = !message;
    });
  }

  function selectConversation(id) {
    if (id === conversationId) return;
    conversationId = id;
    render(() => {
      notice.hidden = true;
      delete notice.dataset.tone;
      setText(label, '');
      setText(status, '');
    });
  }

  return { selectConversation, show };
}
