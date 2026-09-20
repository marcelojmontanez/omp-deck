import assert from 'node:assert/strict';

const IDS = Object.freeze({
  reload: 'composer-draft-reload',
  failedClear: 'composer-draft-failed-clear',
  delayedClear: 'composer-draft-delayed-clear',
  localOptOut: 'composer-draft-local-opt-out',
  remoteOptOut: 'composer-draft-remote-opt-out',
  doubleSend: 'composer-draft-double-send',
});

export async function verifyComposerDrafts(page) {
  const result = await page.evaluate(async ids => {
    const savedState = Object.fromEntries(Object.keys(state).map(key => [key, state[key]]));
    const originalRequest = ompDeckApi.request;
    const originalFiles = Object.getOwnPropertyDescriptor(el.attach, 'files');
    const image = {data: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jBuoAAAAASUVORK5CYII=', mimeType: 'image/png', name: 'saved.png'};
    const canvas = document.createElement('canvas');
    canvas.width = 2;
    canvas.height = 2;
    canvas.getContext('2d').fillRect(0, 0, 2, 2);
    const newerImage = {data: canvas.toDataURL('image/png').split(',')[1], mimeType: 'image/png', name: 'newer.png'};
    const now = new Date().toISOString();
    const response = (draft, status = 200) => ({
      ok: status >= 200 && status < 300,
      status,
      json: async () => draft,
    });
    const remote = (revision, text, images = [image]) => ({
      enabled: true, revision, text, images, updatedAt: now, expiresAt: now,
    });
    const agent = id => ({
      id, lifecycle: 'ongoing', project: id, label: id, machineId: 'local',
      nodeAvailable: true, status: 'idle', executionState: 'idle',
      inboxState: 'read', bridgeConnected: true,
      capabilities: {sendMessage: true, followUp: true, attachments: true},
      attachmentLimits: {maxImages: 4, maxTotalBytes: 20 * 1024 * 1024},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      pendingUiRequest: null, subagents: [],
      session: {activity: [], archiveActivity: [], todo: '', updatedMs: Date.now()},
    });
    const tick = () => new Promise(resolve => setTimeout(resolve, 0));
    const waitFor = async (predicate, label) => {
      const deadline = performance.now() + 4000;
      while (!predicate()) {
        if (performance.now() >= deadline) throw new Error(`Draft case timed out: ${label}`);
        await new Promise(resolve => setTimeout(resolve, 10));
      }
    };
    const select = fixture => {
      composer.remove(fixture.id);
      state.agents = [fixture];
      state.selectedId = fixture.id;
      composer.select(fixture.id);
      composer.syncDraftSaving(fixture);
      renderDetail({selectionChanged: true});
    };
    const type = text => {
      el.message.value = text;
      el.message.dispatchEvent(new Event('input', {bubbles: true}));
    };

    try {
      const reloadAgent = agent(ids.reload);
      const reloadCalls = [];
      ompDeckApi.request = async (path, options = {}) => {
        reloadCalls.push({path, method: options.method || 'GET'});
        if (path.endsWith('/draft')) return response(remote('reload-r1', 'Restored text'));
        if (path.endsWith('/deliveries')) return response({version: 1, deliveries: []});
        throw new Error(`Unexpected reload request: ${path}`);
      };
      select(reloadAgent);
      await waitFor(() => el.message.value === 'Restored text' && el.attachments.querySelectorAll('img').length === 1,
        'text and image reload');
      const reload = {
        text: el.message.value,
        images: el.attachments.querySelectorAll('img').length,
        enabled: el['save-draft'].checked,
        deliveries: reloadCalls.filter(call => call.path.endsWith('/messages')).length,
      };

      const failedAgent = agent(ids.failedClear);
      let failedGets = 0;
      const failedRequests = [];
      ompDeckApi.request = async (path, options = {}) => {
        const method = options.method || 'GET';
        failedRequests.push({path, method, body: options.body ? JSON.parse(options.body) : null});
        if (path.endsWith('/deliveries')) return response({version: 1, deliveries: []});
        if (path.endsWith('/draft') && method === 'GET') {
          failedGets += 1;
          return response(remote('failed-r1', 'Keep after failed clear'));
        }
        if (path.endsWith('/draft') && method === 'POST') throw new TypeError('clear connection lost');
        throw new Error(`Unexpected failed-clear request: ${path}`);
      };
      select(failedAgent);
      await waitFor(() => el.message.value === 'Keep after failed clear' && el.attachments.querySelectorAll('img').length === 1,
        'failed-clear load');
      el.composer.requestSubmit();
      await waitFor(() => failedGets === 2 && el['draft-save-status'].textContent.includes('Unsaved'),
        'failed clear reconciliation');
      const failedClear = {
        text: el.message.value,
        images: el.attachments.querySelectorAll('img').length,
        deliveries: failedRequests.filter(call => call.path.endsWith('/messages')).length,
        status: el['draft-save-status'].textContent,
        conflictVisible: !el['draft-conflict'].hidden,
        clear: failedRequests.find(call => call.path.endsWith('/draft') && call.method === 'POST')?.body,
      };

      const delayedAgent = agent(ids.delayedClear);
      let releaseClear;
      let clearStarted = false;
      let revision = 'delayed-r1';
      const delayedRequests = [];
      ompDeckApi.request = async (path, options = {}) => {
        const method = options.method || 'GET';
        const body = options.body ? JSON.parse(options.body) : null;
        delayedRequests.push({path, method, body});
        if (path.endsWith('/deliveries')) return response({version: 1, deliveries: []});
        if (path.endsWith('/draft') && method === 'GET') return response(remote(revision, 'Original text'));
        if (path.endsWith('/draft') && method === 'POST' && body.text === '') {
          clearStarted = true;
          await new Promise(resolve => { releaseClear = resolve; });
          revision = 'delayed-r2';
          return response({enabled: true, revision, text: '', updatedAt: now, expiresAt: now});
        }
        if (path.endsWith('/draft') && method === 'POST') {
          revision = 'delayed-r3';
          return response({enabled: true, revision, text: body.text, updatedAt: now, expiresAt: now});
        }
        throw new Error(`Unexpected delayed-clear request: ${path}`);
      };
      select(delayedAgent);
      await waitFor(() => el.message.value === 'Original text' && el.attachments.querySelectorAll('img').length === 1,
        'delayed-clear load');
      el.composer.requestSubmit();
      await waitFor(() => clearStarted, 'clear request');
      type('Newer text while clearing');
      const transfer = new DataTransfer();
      transfer.items.add(new File([Uint8Array.from(atob(newerImage.data), char => char.charCodeAt(0))], newerImage.name,
        {type: newerImage.mimeType}));
      Object.defineProperty(el.attach, 'files', {configurable: true, value: transfer.files});
      el.attach.dispatchEvent(new Event('change', {bubbles: true}));
      await waitFor(() => el.attachments.querySelectorAll('img').length === 2, 'new image preview');
      releaseClear();
      await waitFor(() => delayedRequests.some(call => call.path.endsWith('/draft')
        && call.method === 'POST' && call.body.text === 'Newer text while clearing'), 'newer draft resave');
      await waitFor(() => el['draft-save-status'].textContent === 'Saved', 'newer draft saved');
      await tick();
      const delayedClear = {
        text: el.message.value,
        images: el.attachments.querySelectorAll('img').length,
        deliveries: delayedRequests.filter(call => call.path.endsWith('/messages')).length,
        saves: delayedRequests.filter(call => call.path.endsWith('/draft') && call.method === 'POST').map(call => call.body),
        status: el['draft-save-status'].textContent,
      };
      let clearCount = 0;
      let deliveryCount = 0;
      let releaseDuplicateClear;
      ompDeckApi.request = async (path, options = {}) => {
        if (path.endsWith('/deliveries')) return response({version: 1, deliveries: []});
        if (path.endsWith('/draft') && (options.method || 'GET') === 'GET') {
          return response(remote('double-r1', 'Send exactly once'));
        }
        if (path.endsWith('/draft')) {
          clearCount += 1;
          if (clearCount === 1) await new Promise(resolve => { releaseDuplicateClear = resolve; });
          return response({enabled: true, revision: `double-r${clearCount + 1}`, text: '', updatedAt: now, expiresAt: now});
        }
        if (path.endsWith('/messages')) {
          deliveryCount += 1;
          return response({requestId: JSON.parse(options.body).requestId, status: 'accepted', acceptance: 'accepted'}, 202);
        }
        throw new Error(`Unexpected duplicate-send request: ${path}`);
      };
      select(agent(ids.doubleSend));
      await waitFor(() => el.message.value === 'Send exactly once', 'duplicate-send initial load');
      el.composer.requestSubmit();
      await waitFor(() => releaseDuplicateClear, 'duplicate-send clear pending');
      el.composer.requestSubmit();
      releaseDuplicateClear();
      await waitFor(() => deliveryCount > 0 && el.message.value === '', 'duplicate-send accepted');
      await tick();
      const doubleSend = {clearCount, deliveryCount};

      const optOutCases = [];
      for (const locallyDisabled of [true, false]) {
        const fixture = agent(locallyDisabled ? ids.localOptOut : ids.remoteOptOut);
        let stored = remote('opt-r1', 'Retain this browser-only draft');
        let reads = 0;
        let writes = 0;
        ompDeckApi.request = async (path, options = {}) => {
          if (path.endsWith('/deliveries')) return response({version: 1, deliveries: []});
          if (!path.endsWith('/draft')) throw new Error(`Unexpected opt-out request: ${path}`);
          if ((options.method || 'GET') === 'GET') {
            reads += 1;
            return response(stored);
          }
          const body = JSON.parse(options.body);
          if (body.revision !== stored.revision) return response({error: 'Changed', draft: stored}, 409);
          writes += 1;
          stored = {...stored, ...body, revision: `opt-write-${writes}`};
          const {images, ...acknowledgement} = stored;
          return response(acknowledgement);
        };
        select(fixture);
        await waitFor(() => el.message.value === 'Retain this browser-only draft', 'opt-out initial load');
        if (locallyDisabled) {
          el['save-draft'].click();
          await waitFor(() => !stored.enabled && el['draft-save-status'].textContent === 'Not saved on this machine',
            'opt-out acknowledged');
        } else stored = {...stored, enabled: false, revision: 'opt-remote-disabled', text: '', images: []};
        composer.select(null);
        composer.select(fixture.id);
        await waitFor(() => reads === 2 && el['draft-save-status'].textContent === 'Not saved on this machine',
          'disabled state refreshed');
        stored = remote('opt-new-device', 'Different content saved on another device');
        composer.select(null);
        composer.select(fixture.id);
        await waitFor(() => reads === 3 && !el['draft-conflict'].hidden, 'browser-only ownership conflict');
        optOutCases.push({
          locallyDisabled,
          text: el.message.value,
          images: el.attachments.querySelectorAll('img').length,
          remoteText: stored.text,
          writes,
        });
      }
      return {reload, failedClear, delayedClear, doubleSend, optOutCases};
    } finally {
      if (originalFiles) Object.defineProperty(el.attach, 'files', originalFiles);
      else delete el.attach.files;
      for (const id of Object.values(ids)) composer.remove(id);
      ompDeckApi.request = originalRequest;
      Object.assign(state, savedState);
      composer.select(state.selectedId);
      renderDetail({selectionChanged: true});
    }
  }, IDS);

  assert.deepEqual(result.reload, {text: 'Restored text', images: 1, enabled: true, deliveries: 0},
    'an opted-in text and image draft reloads without delivering a message');
  assert.equal(result.failedClear.text, 'Keep after failed clear');
  assert.equal(result.failedClear.images, 1);
  assert.equal(result.failedClear.deliveries, 0, 'a failed saved-draft clear must block delivery');
  assert.match(result.failedClear.status, /Unsaved/i);
  assert.doesNotMatch(result.failedClear.status, /another device/i,
    'an unchanged revision after an uncertain clear must not claim another-device conflict');
  assert.equal(result.failedClear.conflictVisible, false);
  assert.deepEqual(result.failedClear.clear, {
    revision: 'failed-r1', enabled: true, text: '', images: [],
  }, 'Send clears the exact saved revision before attempting delivery');

  assert.equal(result.delayedClear.text, 'Newer text while clearing');
  assert.equal(result.delayedClear.images, 2);
  assert.equal(result.delayedClear.deliveries, 0,
    'editing while the saved draft is clearing must cancel the captured send');
  assert.equal(result.delayedClear.status, 'Saved');
  assert.deepEqual(result.delayedClear.saves[0], {
    revision: 'delayed-r1', enabled: true, text: '', images: [],
  });
  assert.equal(result.delayedClear.saves[1].revision, 'delayed-r2');
  assert.equal(result.delayedClear.saves[1].text, 'Newer text while clearing');
  assert.equal(result.delayedClear.saves[1].images.length, 2,
    'the replacement save preserves both the restored and newly selected image');
  assert.deepEqual(result.doubleSend, {clearCount: 1, deliveryCount: 1},
    'a repeated click while the saved draft is clearing must not submit duplicate messages');
  for (const fixture of result.optOutCases) {
    assert.equal(fixture.text, 'Retain this browser-only draft',
      'disabling saving must not surrender ownership of retained browser text');
    assert.equal(fixture.images, 1);
    assert.equal(fixture.remoteText, 'Different content saved on another device');
    assert.equal(fixture.writes, fixture.locallyDisabled ? 1 : 0,
      'refresh must neither overwrite local content nor re-enable saving automatically');
  }
}
