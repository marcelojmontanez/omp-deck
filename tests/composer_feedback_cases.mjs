import assert from 'node:assert/strict';

export async function verifyComposerFeedback(page) {
  const observed = await page.evaluate(async () => {
    const savedState = Object.fromEntries(Object.keys(state).map(key => [key, state[key]]));
    const originalRequest = ompDeckApi.request;
    const ids = ['feedback-primary', 'feedback-other', 'feedback-history', 'feedback-recovery'];
    const receipts = new Map();
    const held = new Map();
    let recoveryRead;
    const status = () => el['send-state'].textContent;
    const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
    const waitFor = async (predicate, label, timeout = 1500) => {
      const deadline = Date.now() + timeout;
      while (!predicate()) {
        if (Date.now() >= deadline) throw new Error(`Composer feedback timed out: ${label}; status=${status()}`);
        await pause(10);
      }
    };
    const agent = id => ({
      id, lifecycle: 'ongoing', project: 'demo', label: id, machineId: 'local',
      nodeAvailable: true, status: 'idle', executionState: 'idle', inboxState: 'read',
      bridgeConnected: true, capabilities: {sendMessage: true, attachments: true},
      attachmentLimits: {maxImages: 4, maxTotalBytes: 20 * 1024 * 1024},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      pendingUiRequest: null, subagents: [],
      session: {activity: [], archiveActivity: [], todo: '', updatedMs: Date.now()},
    });
    const receipt = (id, body) => ({
      sessionId: id, requestId: body.requestId, action: 'sendMessage', state: 'accepted',
      message: body.message, attachments: [], createdAt: 1, updatedAt: 2, expiresAt: 3,
    });
    const type = text => {
      el.message.value = text;
      el.message.dispatchEvent(new Event('input', {bubbles: true}));
    };
    const submit = text => {
      type(text);
      el.composer.requestSubmit();
    };
    const accepted = async text => {
      submit(text);
      await waitFor(() => status() === 'Accepted', text);
    };
    const result = {};
    try {
      for (const id of ids) composer.remove(id);
      receipts.set(ids[2], receipt(ids[2], {requestId: 'historical', message: 'already accepted'}));
      ompDeckApi.request = async (path, options = {}) => {
        if (path.endsWith('/draft')) return {ok: true, status: 200, json: async () => ({
          enabled: false, revision: null, text: '', images: [], updatedAt: null, expiresAt: null,
        })};
        const id = decodeURIComponent(path.split('/')[3]);
        if (path.endsWith('/deliveries')) {
          if (id === ids[3]) await new Promise((resolve, reject) => { recoveryRead = {resolve, reject}; });
          return {ok: true, json: async () => ({version: 1, deliveries: receipts.has(id) ? [receipts.get(id)] : []})};
        }
        if (!path.endsWith('/messages')) return {ok: true, json: async () => ({})};
        const body = JSON.parse(options.body);
        if (body.message.startsWith('held ')) {
          await new Promise((resolve, reject) => { held.set(body.message, {resolve, reject}); });
        }
        receipts.set(id, receipt(id, body));
        return {ok: true, status: 202, json: async () => ({
          requestId: body.requestId, status: 'accepted', acceptance: 'accepted',
        })};
      };
      state.agents = ids.map(agent);
      selectAgent(ids[0], {pushHistory: false});
      await accepted('expires even after recovery');
      const acceptedAt = Date.now();
      await pause(2100);
      await composer.recover(state.agents[0]);
      await waitFor(() => status() === '', 'success expiry without replay extension',
        Math.max(1, 4200 - (Date.now() - acceptedAt)));
      result.expired = status();
      await pause(1200);
      await composer.recover(state.agents[0]);
      result.replayAfterExpiry = status();

      await accepted('start a new draft');
      type('a different message');
      result.afterTyping = status();
      submit('held draft');
      await waitFor(() => held.has('held draft'), 'held draft request');
      type('keep this newer draft');
      held.get('held draft').resolve();
      await waitFor(() => [...el.activity.querySelectorAll('.event.user:not(.pending) .event-detail')]
        .some(detail => detail.textContent.trim() === 'held draft'), 'old draft accepted preview');
      result.lateAcceptance = {
        status: status(),
        draft: el.message.value,
        acceptedVisible: [...el.activity.querySelectorAll('.event-title')]
          .some(title => title.textContent.trim() === 'You · Accepted'),
      };

      await accepted('add a photo');
      const transfer = new DataTransfer();
      transfer.items.add(new File([Uint8Array.from(atob(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jBuoAAAAASUVORK5CYII='
      ), character => character.charCodeAt(0))], 'pixel.png', {type: 'image/png'}));
      el.attach.files = transfer.files;
      el.attach.dispatchEvent(new Event('change', {bubbles: true}));
      await waitFor(() => el.attachments.querySelector('[data-remove-attachment]'), 'attachment draft');
      result.afterAttachment = status();
      el.attachments.querySelector('[data-remove-attachment]').click();

      await accepted('switch away');
      selectAgent(ids[1], {pushHistory: false});
      result.otherSession = status();
      selectAgent(ids[0], {pushHistory: false});
      result.returnedSession = status();
      selectAgent(ids[2], {pushHistory: false});
      await composer.recover(state.agents[2]);
      await pause(20);
      result.historicalSuccess = status();

      selectAgent(ids[0], {pushHistory: false});
      await accepted('success before slow send');
      submit('held submitting');
      await waitFor(() => held.has('held submitting'), 'slow request');
      await pause(3300);
      result.submittingPastOldExpiry = status();
      held.get('held submitting').resolve();
      await waitFor(() => status() === 'Accepted', 'slow acceptance');
      submit('held failure');
      await waitFor(() => held.has('held failure'), 'failing request');
      held.get('held failure').reject(new TypeError('connection lost after dispatch'));
      await waitFor(() => status() === 'Delivery uncertain', 'delivery uncertainty');
      await pause(3300);
      result.failurePastOldExpiry = {status: status(), draft: el.message.value};

      selectAgent(ids[3], {pushHistory: false});
      await waitFor(() => recoveryRead, 'held recovery');
      await accepted('new send during old recovery');
      recoveryRead.reject(new TypeError('old recovery failed'));
      await pause(20);
      result.staleRecoveryError = status();

      selectAgent(ids[0], {pushHistory: false});
      submit('held background failure');
      await waitFor(() => held.has('held background failure'), 'background send');
      selectAgent(ids[1], {pushHistory: false});
      await accepted('other session success');
      held.get('held background failure').reject(new TypeError('background disconnect'));
      await waitFor(() => composer.pendingFor(ids[0]).every(item => item.state !== 'submitting'), 'background failure');
      result.backgroundFailure = status();
      composer.clearLocalData();
      result.cleared = status();
      return result;
    } finally {
      for (const pending of held.values()) pending.resolve();
      recoveryRead?.resolve();
      for (const id of ids) composer.remove(id);
      ompDeckApi.request = originalRequest;
      Object.assign(state, savedState);
      composer.select(state.selectedId);
      renderDetail({selectionChanged: true});
      if (!state.selectedId) showMenu();
    }
  });

  for (const key of ['expired', 'replayAfterExpiry', 'afterTyping', 'afterAttachment',
    'otherSession', 'returnedSession', 'historicalSuccess']) {
    assert.equal(observed[key], '', `${key} must not show stale acceptance`);
  }
  assert.deepEqual(observed.lateAcceptance, {
    status: '', draft: 'keep this newer draft', acceptedVisible: true,
  }, 'an acknowledgement for an earlier draft keeps its content visible without labeling the current draft');
  assert.equal(observed.submittingPastOldExpiry, 'Submitting', 'an old success timer must not clear a newer send');
  assert.deepEqual(observed.failurePastOldExpiry, {status: 'Delivery uncertain', draft: 'held failure'},
    'delivery uncertainty and its recoverable draft must survive an old success timer');
  assert.equal(observed.staleRecoveryError, 'Accepted', 'an old recovery failure must not overwrite a new send');
  assert.equal(observed.backgroundFailure, 'Accepted', 'another session failure must not overwrite the selected session');
  assert.equal(observed.cleared, '', 'clearing local data removes send feedback');
}
