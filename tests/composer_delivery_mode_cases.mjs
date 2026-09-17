import assert from 'node:assert/strict';

const PRIMARY_ID = 'composer-delivery-primary';
const OTHER_ID = 'composer-delivery-other';

export async function verifyComposerDeliveryModes(page) {
  const result = await page.evaluate(async ({primaryId, otherId}) => {
    const savedState = Object.fromEntries(Object.keys(state).map(key => [key, state[key]]));
    const originalRequest = ompDeckApi.request;
    const requests = [];
    let releaseFirst;
    let failNext = false;
    const tick = () => new Promise(resolve => setTimeout(resolve, 0));
    const waitFor = async predicate => {
      for (let attempt = 0; attempt < 100; attempt += 1) {
        if (predicate()) return;
        await tick();
      }
      throw new Error('Timed out waiting for composer delivery');
    };
    const agent = id => ({
      id, lifecycle: 'ongoing', project: id, label: id, machineId: 'local',
      nodeAvailable: true, status: 'working', executionState: 'working',
      inboxState: 'read', bridgeConnected: true,
      capabilities: {sendMessage: true, followUp: true},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      pendingUiRequest: null, subagents: [],
      session: {activity: [], archiveActivity: [], todo: '', updatedMs: Date.now()},
    });
    const primary = agent(primaryId);
    const other = agent(otherId);

    try {
      state.agents = [primary, other];
      state.selectedId = primaryId;
      composer.select(primaryId);
      composer.syncDeliveryMode(primary);
      ompDeckApi.request = async (path, options = {}) => {
        if (path.endsWith('/deliveries')) return {ok: true, json: async () => ({version: 1, deliveries: []})};
        const body = JSON.parse(options.body);
        requests.push({path, body});
        if (failNext) {
          failNext = false;
          throw new TypeError('connection lost after dispatch');
        }
        if (requests.length === 1) await new Promise(resolve => { releaseFirst = resolve; });
        return {ok: true, status: 202, json: async () => ({
          requestId: body.requestId, status: 'accepted', acceptance: 'accepted',
        })};
      };
      renderDetail({selectionChanged: true});

      el.message.value = 'first steer';
      el.message.dispatchEvent(new Event('input', {bubbles: true}));
      el.composer.requestSubmit();
      await waitFor(() => requests.length === 1);

      document.querySelector('[data-delivery-mode="queue"]').click();
      el.message.value = 'captured queue';
      el.message.dispatchEvent(new Event('input', {bubbles: true}));
      el.composer.requestSubmit();
      primary.executionState = 'idle';
      primary.status = 'idle';
      composer.syncDeliveryMode(primary);
      state.selectedId = otherId;
      composer.select(otherId);
      composer.syncDeliveryMode(other);
      const otherDefault = document.querySelector('[data-delivery-mode="steer"]').getAttribute('aria-pressed');
      document.querySelector('[data-delivery-mode="queue"]').click();
      state.selectedId = primaryId;
      composer.select(primaryId);
      primary.executionState = 'working';
      primary.status = 'working';
      composer.syncDeliveryMode(primary);
      const primaryMode = document.querySelector('[data-delivery-mode="queue"]').getAttribute('aria-pressed');
      state.selectedId = otherId;
      composer.select(otherId);
      composer.syncDeliveryMode(other);
      const otherMode = document.querySelector('[data-delivery-mode="queue"]').getAttribute('aria-pressed');
      releaseFirst();
      await waitFor(() => requests.length === 2);

      failNext = true;
      el.message.value = 'retry identity';
      el.message.dispatchEvent(new Event('input', {bubbles: true}));
      el.composer.requestSubmit();
      await waitFor(() => requests.length === 3 && el.message.value === 'retry identity');
      const uncertain = requests[2];
      document.querySelector('[data-delivery-mode="steer"]').click();
      el.composer.requestSubmit();
      await waitFor(() => requests.length === 4);
      const retried = requests[3];
      other.capabilities.followUp = false;
      composer.syncDeliveryMode(other);
      const unsupportedHidden = el['delivery-mode'].hidden;

      return {
        paths: requests.map(request => request.path),
        queuedMessage: requests[1].body.message,
        primaryMode,
        otherMode,
        otherDefault,
        unsupportedHidden,
        retryIds: [uncertain.body.requestId, retried.body.requestId],
        retryPaths: [uncertain.path, retried.path],
      };
    } finally {
      ompDeckApi.request = originalRequest;
      composer.remove(primaryId);
      composer.remove(otherId);
      Object.assign(state, savedState);
      composer.select(state.selectedId);
      renderDetail({selectionChanged: true});
    }
  }, {primaryId: PRIMARY_ID, otherId: OTHER_ID});

  assert(result.paths[0].endsWith('/messages'), 'default busy delivery steers the current turn');
  assert(result.paths[1].endsWith('/follow-ups'), 'queued action survives busy-to-idle and session changes');
  assert.equal(result.queuedMessage, 'captured queue');
  assert.equal(result.primaryMode, 'true', 'delivery preference is retained for the primary session');
  assert.equal(result.otherMode, 'true', 'delivery preference is independent for another session');
  assert.equal(result.otherDefault, 'true', 'a second session starts in steer mode');
  assert.equal(result.unsupportedHidden, true, 'sessions without advertised follow-up capability hide queueing');
  assert.equal(result.retryIds[0], result.retryIds[1], 'uncertain retry retains its request identity');
  assert.deepEqual(result.retryPaths, [result.retryPaths[0], result.retryPaths[0]],
    'uncertain retry retains its original native action after mode changes');
}
