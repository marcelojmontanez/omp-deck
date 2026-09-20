import assert from 'node:assert/strict';

const IDS = Object.freeze({
  held: 'composer-optimistic-held',
  failed: 'composer-optimistic-failed',
  edited: 'composer-optimistic-edited',
  switched: 'composer-optimistic-switched',
  other: 'composer-optimistic-other',
  cleared: 'composer-optimistic-cleared',
});

export async function verifyComposerOptimism(page) {
  const result = await page.evaluate(async ids => {
    const savedState = Object.fromEntries(Object.keys(state).map(key => [key, state[key]]));
    const originalRequest = ompDeckApi.request;
    const releases = new Set();
    const image = {
      data: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jBuoAAAAASUVORK5CYII=',
      mimeType: 'image/png',
      name: 'optimistic.png',
    };
    const now = new Date().toISOString();
    const response = (payload, status = 200) => ({
      ok: status >= 200 && status < 300,
      status,
      json: async () => payload,
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
        if (performance.now() >= deadline) throw new Error(`Optimistic composer case timed out: ${label}`);
        await new Promise(resolve => setTimeout(resolve, 10));
      }
    };
    const hold = () => {
      let release;
      const promise = new Promise(resolve => { release = resolve; });
      const trackedRelease = () => {
        releases.delete(trackedRelease);
        release();
      };
      releases.add(trackedRelease);
      return {promise, release: trackedRelease};
    };
    const pending = () => [...el.activity.querySelectorAll('.event.pending')];
    const snapshot = () => ({
      count: pending().length,
      text: pending()[0]?.querySelector('.event-detail')?.textContent.trim() || '',
      images: pending()[0]?.querySelectorAll('.message-attachments img').length || 0,
      controls: pending()[0]?.querySelectorAll('.delivery-controls').length || 0,
    });
    const select = fixture => {
      composer.remove(fixture.id);
      state.agents = [fixture];
      state.selectedId = fixture.id;
      composer.select(fixture.id);
      composer.syncDraftSaving(fixture);
      renderDetail({selectionChanged: true});
    };

    try {
      const heldClear = hold();
      const heldDelivery = hold();
      let heldMessages = 0;
      ompDeckApi.request = async (path, options = {}) => {
        const method = options.method || 'GET';
        if (path.endsWith('/deliveries')) return response({version: 1, deliveries: []});
        if (path.endsWith('/draft') && method === 'GET') return response(remote('held-r1', 'Visible before clear'));
        if (path.endsWith('/draft')) {
          await heldClear.promise;
          return response(remote('held-r2', '', []));
        }
        if (path.endsWith('/messages')) {
          heldMessages += 1;
          await heldDelivery.promise;
          const requestId = JSON.parse(options.body).requestId;
          return response({requestId, status: 'accepted', acceptance: 'accepted'}, 202);
        }
        throw new Error(`Unexpected held request: ${path}`);
      };
      select(agent(ids.held));
      await waitFor(() => el.message.value === 'Visible before clear' && el.attachments.querySelectorAll('img').length === 1,
        'held draft load');
      el.composer.requestSubmit();
      await waitFor(() => pending().length === 1, 'provisional bubble');
      const beforeClear = {...snapshot(), messages: heldMessages, draft: el.message.value};
      el.composer.requestSubmit();
      await tick();
      const duplicate = {...snapshot(), messages: heldMessages};
      heldClear.release();
      await waitFor(() => heldMessages === 1, 'delivery after clear');
      const duringDelivery = snapshot();
      heldDelivery.release();
      await waitFor(() => pending().length === 0, 'accepted bubble styling');
      const acceptedBubble = el.activity.querySelector('.event.user:not(.pending)');
      const accepted = {
        pending: pending().length,
        title: acceptedBubble?.querySelector('.event-title')?.textContent.trim() || '',
        text: acceptedBubble?.querySelector('.event-detail')?.textContent.trim() || '',
        images: acceptedBubble?.querySelectorAll('.message-attachments img').length || 0,
        controls: acceptedBubble?.querySelectorAll('.delivery-controls').length || 0,
      };

      let failedMessages = 0;
      ompDeckApi.request = async (path, options = {}) => {
        const method = options.method || 'GET';
        if (path.endsWith('/deliveries')) return response({version: 1, deliveries: []});
        if (path.endsWith('/draft') && method === 'GET') return response(remote('failed-r1', 'Keep failed draft'));
        if (path.endsWith('/draft')) throw new TypeError('draft clear unavailable');
        if (path.endsWith('/messages')) failedMessages += 1;
        throw new Error(`Unexpected failed request: ${path}`);
      };
      select(agent(ids.failed));
      await waitFor(() => el.message.value === 'Keep failed draft' && el.attachments.querySelectorAll('img').length === 1,
        'failed draft load');
      el.composer.requestSubmit();
      await waitFor(() => el['draft-save-status'].textContent.includes('Unsaved'), 'failed clear feedback');
      const failed = {
        bubble: pending().length,
        text: el.message.value,
        images: el.attachments.querySelectorAll('img').length,
        messages: failedMessages,
      };

      const editedClear = hold();
      let editedMessages = 0;
      ompDeckApi.request = async (path, options = {}) => {
        const method = options.method || 'GET';
        if (path.endsWith('/deliveries')) return response({version: 1, deliveries: []});
        if (path.endsWith('/draft') && method === 'GET') return response(remote('edited-r1', 'Captured text', []));
        if (path.endsWith('/draft') && JSON.parse(options.body).text === '') {
          await editedClear.promise;
          return response(remote('edited-r2', '', []));
        }
        if (path.endsWith('/draft')) return response(remote('edited-r3', JSON.parse(options.body).text, []));
        if (path.endsWith('/messages')) editedMessages += 1;
        throw new Error(`Unexpected edited request: ${path}`);
      };
      select(agent(ids.edited));
      await waitFor(() => el.message.value === 'Captured text', 'edited draft load');
      el.composer.requestSubmit();
      await waitFor(() => pending().length === 1, 'edited provisional bubble');
      el.message.value = 'Newer text';
      el.message.dispatchEvent(new Event('input', {bubbles: true}));
      editedClear.release();
      await waitFor(() => pending().length === 0, 'edited bubble withdrawal');
      await tick();
      const edited = {text: el.message.value, messages: editedMessages};

      const switchClear = hold();
      const switchDelivery = hold();
      let switchedPath = '';
      const switchedAgent = agent(ids.switched);
      const otherAgent = agent(ids.other);
      ompDeckApi.request = async (path, options = {}) => {
        const method = options.method || 'GET';
        if (path.endsWith('/deliveries')) return response({version: 1, deliveries: []});
        if (path.endsWith('/draft') && method === 'GET') {
          return response(remote(path.includes(encodeURIComponent(ids.switched)) ? 'switch-r1' : 'other-r1',
            path.includes(encodeURIComponent(ids.switched)) ? 'Switch in flight' : '', []));
        }
        if (path.endsWith('/draft')) {
          await switchClear.promise;
          return response(remote('switch-r2', '', []));
        }
        if (path.endsWith('/messages')) {
          switchedPath = path;
          await switchDelivery.promise;
          const requestId = JSON.parse(options.body).requestId;
          return response({requestId, status: 'accepted', acceptance: 'accepted'}, 202);
        }
        throw new Error(`Unexpected switch request: ${path}`);
      };
      state.agents = [switchedAgent, otherAgent];
      state.selectedId = ids.switched;
      composer.select(ids.switched);
      composer.syncDraftSaving(switchedAgent);
      renderDetail({selectionChanged: true});
      await waitFor(() => el.message.value === 'Switch in flight', 'switch draft load');
      el.composer.requestSubmit();
      await waitFor(() => pending().length === 1, 'switch provisional bubble');
      state.selectedId = ids.other;
      composer.select(ids.other);
      composer.syncDraftSaving(otherAgent);
      renderDetail({selectionChanged: true});
      switchClear.release();
      await waitFor(() => switchedPath !== '', 'original-session delivery');
      const otherPending = pending().length;
      state.selectedId = ids.switched;
      composer.select(ids.switched);
      renderDetail({selectionChanged: true});
      await waitFor(() => pending().length === 1, 'original-session pending bubble');
      const switched = {otherPending, originalPending: snapshot(), path: switchedPath};
      switchDelivery.release();
      await waitFor(() => pending().length === 0, 'switched delivery accepted');

      const clearGeneration = hold();
      let clearedMessages = 0;
      ompDeckApi.request = async (path, options = {}) => {
        const method = options.method || 'GET';
        if (path.endsWith('/deliveries')) return response({version: 1, deliveries: []});
        if (path.endsWith('/draft') && method === 'GET') return response(remote('clear-r1', 'Discard generation', []));
        if (path.endsWith('/draft')) {
          await clearGeneration.promise;
          return response(remote('clear-r2', '', []));
        }
        if (path.endsWith('/messages')) clearedMessages += 1;
        throw new Error(`Unexpected generation request: ${path}`);
      };
      select(agent(ids.cleared));
      await waitFor(() => el.message.value === 'Discard generation', 'generation draft load');
      el.composer.requestSubmit();
      await waitFor(() => pending().length === 1, 'generation provisional bubble');
      composer.clearLocalData();
      clearGeneration.release();
      await tick();
      await tick();
      const cleared = {pending: pending().length, messages: clearedMessages, text: el.message.value};

      return {beforeClear, duplicate, duringDelivery, accepted, failed, edited, switched, cleared};
    } finally {
      for (const release of [...releases]) release();
      for (const id of Object.values(ids)) composer.remove(id);
      ompDeckApi.request = originalRequest;
      Object.assign(state, savedState);
      composer.select(state.selectedId);
      renderDetail({selectionChanged: true});
    }
  }, IDS);

  assert.deepEqual(result.beforeClear, {
    count: 1, text: 'Visible before clear', images: 1, controls: 0, messages: 0,
    draft: 'Visible before clear',
  }, 'Send immediately shows the text and image while saved-draft clearing is held');
  assert.deepEqual(result.duplicate, {
    count: 1, text: 'Visible before clear', images: 1, controls: 0, messages: 0,
  }, 'a duplicate click during preparation neither duplicates the bubble nor dispatches');
  assert.deepEqual(result.duringDelivery, {
    count: 1, text: 'Visible before clear', images: 1, controls: 0,
  }, 'the same pending bubble remains while the message request is held');
  assert.deepEqual(result.accepted, {
    pending: 0, title: 'You · Accepted', text: 'Visible before clear', images: 1, controls: 1,
  }, 'accepted image delivery remains visible as an honest, dismissible receipt until a native echo exists');
  assert.deepEqual(result.failed, {
    bubble: 0, text: 'Keep failed draft', images: 1, messages: 0,
  }, 'failed saved-draft clearing withdraws the bubble and preserves the draft and image');
  assert.deepEqual(result.edited, {text: 'Newer text', messages: 0},
    'editing during preparation withdraws the captured bubble without sending or clearing newer text');
  assert.equal(result.switched.otherPending, 0);
  assert.equal(result.switched.originalPending.count, 1,
    'switching sessions retains the pending bubble with its original session');
  assert.equal(result.switched.originalPending.text, 'Switch in flight');
  assert(result.switched.path.endsWith(`/${encodeURIComponent(IDS.switched)}/messages`),
    'switching sessions does not redirect the eventual delivery');
  assert.deepEqual(result.cleared, {pending: 0, messages: 0, text: ''},
    'clearing the composer generation prevents a late send or bubble resurrection');
}
