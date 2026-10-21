/* Retain the tab's current attachment selection and release copies after removal or discard. */

export function createAttachmentDrafts({ getFiles }) {
  let draftId = null;
  let desired = [];
  let saved = '[]';
  let running = null;
  let refresh = false;
  const observed = new Set();

  function snapshot() {
    const identities = [...new Set(getFiles().filter(file => file.reference.source === 'upload' && file.available !== false).map(file => file.reference.attachment_id))].sort();
    identities.forEach(identity => observed.add(identity));
    return identities;
  }

  function releasedIds() {
    const retained = new Set(desired);
    return [...observed].filter(identity => !retained.has(identity));
  }

  async function flush() {
    desired = snapshot();
    if (running) {
      await running;
      return flush();
    }
    if (!refresh && JSON.stringify(desired) === saved && !releasedIds().length) return;
    running = (async () => {
      while (refresh || JSON.stringify(desired) !== saved || releasedIds().length) {
        refresh = false;
        const identities = [...desired];
        const released = releasedIds();
        draftId ||= crypto.randomUUID();
        const response = await fetch(`/api/attachments/drafts/${draftId}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ attachment_ids: identities, released_attachment_ids: released }), signal: AbortSignal.timeout(10000)
        });
        if (!response.ok) throw new Error('Could not retain the attachment selection. Check the connection and try again.');
        saved = JSON.stringify(identities);
        released.forEach(identity => observed.delete(identity));
      }
    })();
    try { await running; }
    finally { running = null; }
  }

  function update() {
    // Remember copies immediately, even if a later selection replaces the queued snapshot.
    desired = snapshot();
    // Coalesce synchronous resets and restores before releasing any existing selection.
    queueMicrotask(() => { flush().catch(() => { /* Retry on the next change or renewal. */ }); });
  }

  function renew() {
    refresh = snapshot().length > 0;
    update();
  }

  function init() {
    setInterval(renew, 60000);
    window.addEventListener('pageshow', renew);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) renew(); });
    update();
  }

  return { init, update, flush };
}
