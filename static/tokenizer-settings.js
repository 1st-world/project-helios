/* Display the local estimation vocabulary status and handle explicit downloads in Settings. */

import { $, formatSize, setRefreshBusy, setStatusMessage, setText } from './ui.js';
import { fallbackEstimateLabel, vocabularyNamePattern } from './estimation.js';

const vocabularyMessages = {
  ready: 'Current method: o200k_base vocabulary.',
  missing: `Current method: ${fallbackEstimateLabel}.`,
  invalid: `The o200k_base vocabulary could not be verified. Current method: ${fallbackEstimateLabel}.`
};

async function readVocabulary(response, fallback) {
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : fallback);
  if (typeof data?.can_install !== 'boolean') throw new Error(fallback);
  if (data.state === 'installing') {
    const download = data.download;
    if (!['downloading', 'verifying'].includes(download?.phase) || !Number.isSafeInteger(download.received_bytes)
      || download.received_bytes < 0 || (download.total_bytes !== null && (!Number.isSafeInteger(download.total_bytes)
        || download.total_bytes <= 0 || download.total_bytes < download.received_bytes))) throw new Error(fallback);
  } else if (!Object.hasOwn(vocabularyMessages, data.state)) throw new Error(fallback);
  return data;
}

export function initializeTokenizerSettings(dialog) {
  const panel = $('#settings-context');
  const body = $('#context-tokenizer-body');
  const status = $('#context-tokenizer-status');
  const error = $('#context-tokenizer-error');
  const loadFeedback = $('#context-tokenizer-load-feedback');
  const loadError = $('#context-tokenizer-load-error');
  const reloadButton = $('#reload-tokenizer-status');
  const button = $('#context-tokenizer-action');
  const label = button.querySelector('span');
  const transfer = $('#context-tokenizer-transfer');
  const bar = $('#context-tokenizer-progress');
  const bytes = $('#context-tokenizer-bytes');
  let vocabulary = null;
  let download = null;
  let installationPending = false;
  let statusRequest = null;
  let failure = null;
  let pollTimer;

  function render() {
    const state = vocabulary?.state;
    const reload = failure?.action === 'check';
    const installing = installationPending || (!reload && download !== null);
    let message = vocabularyMessages[state] || 'Checking the local vocabulary…';
    if (installing) message = download?.phase === 'verifying' ? 'Verifying the o200k_base vocabulary…' : 'Downloading the o200k_base vocabulary…';
    else if (state === 'missing' || state === 'invalid') {
      if (!vocabulary.can_install) message += ' Open Helios on its host computer to install o200k_base.';
      else if (state === 'missing') message += ' The o200k_base vocabulary is available to download.';
    }

    setStatusMessage(status, message, installing ? 'running' : state === 'invalid' ? 'warning' : null, vocabularyNamePattern);
    body.hidden = reload;
    loadFeedback.hidden = !reload;
    setStatusMessage(loadError, reload ? failure.message : '', 'error', vocabularyNamePattern);
    error.hidden = failure?.action !== 'install';
    setStatusMessage(error, error.hidden ? '' : failure.message, 'error', vocabularyNamePattern);
    transfer.hidden = !installing;
    if (installing) {
      const received = download?.received_bytes ?? 0;
      const total = download?.total_bytes;
      bar.hidden = total == null;
      if (total != null) {
        bar.max = total;
        bar.value = received;
      }
      setText(bytes, total == null ? `${formatSize(received)} received` : `${formatSize(received)} of ${formatSize(total)} received`);
    }

    const canInstall = !reload && vocabulary?.can_install && (state === 'missing' || state === 'invalid');
    setText(label, state === 'invalid' ? 'Repair vocabulary' : 'Download vocabulary');
    setRefreshBusy(button, installing, !canInstall || installing || Boolean(statusRequest));
    setRefreshBusy(reloadButton, Boolean(statusRequest));
  }

  function acceptVocabulary(data) {
    if (data.state === 'installing') {
      download = data.download;
      return;
    }
    const changed = vocabulary?.state !== data.state;
    vocabulary = data;
    download = null;
    if (changed) dialog.dispatchEvent(new Event('tokenizer-changed'));
  }

  function stopChecking() {
    clearTimeout(pollTimer);
    statusRequest?.abort();
    statusRequest = null;
  }

  function watchDownload() {
    clearTimeout(pollTimer);
    if (!failure && (installationPending || download) && dialog.open && !panel.hidden) {
      pollTimer = setTimeout(check, 400);
    }
  }

  async function check() {
    if (statusRequest) return;
    const request = new AbortController();
    statusRequest = request;
    render();
    const fallback = 'Could not check the vocabulary status.';
    try {
      const response = await fetch('/api/settings/tokenizer', { cache: 'no-store', signal: request.signal });
      const data = await readVocabulary(response, fallback);
      if (statusRequest === request) {
        acceptVocabulary(data);
        if (!installationPending && typeof data.installation_error === 'string' && data.installation_error) {
          failure = { action: 'install', message: data.installation_error };
        } else if (failure?.action !== 'install' || data.state === 'ready' || data.state === 'installing') failure = null;
      }
    } catch (err) {
      if (statusRequest === request && !installationPending) {
        failure = { action: 'check', message: err instanceof TypeError ? fallback : err.message || fallback };
      }
    } finally {
      if (statusRequest === request) {
        statusRequest = null;
        render();
        watchDownload();
      }
    }
  }

  async function install() {
    if (installationPending || download) return;
    installationPending = true;
    failure = null;
    stopChecking();
    render();
    watchDownload();
    const fallback = 'Could not install the vocabulary. Try again.';
    try {
      const response = await fetch('/api/settings/tokenizer', { method: 'POST', headers: { 'X-Helios-Tokenizer': 'install' } });
      acceptVocabulary(await readVocabulary(response, fallback));
    } catch (err) {
      download = null;
      failure = { action: 'install', message: err instanceof TypeError ? fallback : err.message || fallback };
    } finally {
      stopChecking();
      installationPending = false;
      render();
      watchDownload();
    }
  }

  button.addEventListener('click', install);
  reloadButton.addEventListener('click', check);
  dialog.addEventListener('settings-panel-change', event => {
    stopChecking();
    if (dialog.open && event.detail.name === 'context') void check();
  });
  dialog.addEventListener('close', () => {
    if (!dialog.open) stopChecking();
  });
}
