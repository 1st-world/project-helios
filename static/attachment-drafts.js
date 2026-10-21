/* Retain the tab's current attachment selection and release copies after removal or discard. */

export function createAttachmentDrafts({ getFiles }) {
  let draftId = null;
  let desired = [];
  let saved = '[]';
  let running = null;
  let refresh = false;
  const observed = new Set();

  function getDesiredIds() {
    return [...new Set(getFiles().filter(file => file.reference.source === 'upload' && file.available !== false).map(file => file.reference.attachment_id))].sort();
  }

  function capture() {
    // Remember copies immediately, even if a later selection replaces the queued snapshot.
    desired = getDesiredIds();
    desired.forEach(identity => observed.add(identity));
  }

  function releasedIds() {
    const retained = new Set(desired);
    return [...observed].filter(identity => !retained.has(identity));
  }

  async function synchronize() {
    if (running) {
      await running;
      return synchronize();
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

  function flush() {
    capture();
    return synchronize();
  }

  function schedule() {
    // Coalesce synchronous resets and restores before releasing any existing selection.
    queueMicrotask(() => { synchronize().catch(() => { /* Retry on the next change or renewal. */ }); });
  }

  function update() {
    capture();
    schedule();
  }

  function renew() {
    capture();
    refresh ||= desired.length > 0;
    schedule();
  }

  function init() {
    setInterval(renew, 15 * 60 * 1000);
    window.addEventListener('pageshow', renew);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) renew(); });
    update();
  }

  return { init, update, flush };
}
