import assert from 'node:assert/strict';

const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

export async function verifyBrowserStateRegressions(page) {
  const composer = await page.evaluate(async () => {
    const savedState = Object.fromEntries(Object.keys(state).map(key => [key, state[key]]));
    const originalRequest = ompDeckApi.request;
    const OriginalFileReader = globalThis.FileReader;
    const originalConfirm = window.confirm;
    const fixtureIds = [
      'browser-state-restore', 'browser-state-clear', 'browser-state-attachments',
      'browser-state-switch', 'browser-state-switch-other', 'browser-state-read-failure',
    ];
    const posts = [];
    const recoveries = new Map();
    const readers = [];
    const waitFor = async (predicate, label) => {
      const deadline = Date.now() + 2_000;
      while (!predicate()) {
        if (Date.now() >= deadline) throw new Error(`Browser state fixture timed out: ${label}`);
        await new Promise(resolve => setTimeout(resolve, 5));
      }
    };
    const agent = id => ({
      id, localId: id, lifecycle: 'ongoing', project: 'fixture', label: id, machineId: 'local',
      nodeAvailable: true, bridgeConnected: true, status: 'idle', executionState: 'idle',
      inboxState: 'read', capabilities: {sendMessage: true, attachments: true},
      attachmentLimits: {maxImages: 4, maxTotalBytes: 20 * 1024 * 1024},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      pendingUiRequest: null, subagents: [],
      session: {activity: [], archiveActivity: [], todos: [], updatedMs: Date.now()},
    });
    const selectFixture = id => {
      composer.remove(id);
      const selected = agent(id);
      state.agents = [selected];
      state.selectedId = null;
      selectAgent(id, {pushHistory: false});
      return selected;
    };
    const type = value => {
      el.message.value = value;
      el.message.dispatchEvent(new Event('input', {bubbles: true}));
    };
    const chooseFiles = files => {
      const transfer = new DataTransfer();
      files.forEach(file => transfer.items.add(file));
      el.attach.files = transfer.files;
      el.attach.dispatchEvent(new Event('change', {bubbles: true}));
    };
    const recoveryRecord = (sessionId, requestId, message) => ({
      sessionId, requestId, action: 'sendMessage', state: 'uncertain', message,
      attachments: [], createdAt: 1, updatedAt: 2, expiresAt: Date.now() + 60_000,
    });

    ompDeckApi.request = async (path, options = {}) => {
      if (path.endsWith('/draft')) {
        return {ok: true, status: 200, json: async () => ({
          enabled: false, revision: null, text: '', images: [], updatedAt: null, expiresAt: null,
        })};
      }
      if (path.endsWith('/deliveries')) {
        const id = decodeURIComponent(path.split('/')[3]);
        return {ok: true, status: 200, json: async () => ({
          version: 1,
          deliveries: recoveries.get(id) || [],
        })};
      }
      if (path.endsWith('/messages')) {
        const body = JSON.parse(options.body);
        posts.push(body);
        return {ok: true, status: 202, json: async () => ({
          requestId: body.requestId, status: 'accepted', acceptance: 'accepted',
        })};
      }
      return {ok: true, status: 200, json: async () => ({})};
    };

    try {
      const restoreId = fixtureIds[0];
      recoveries.set(restoreId, [
        recoveryRecord(restoreId, 'receipt-a', 'Restored A'),
        recoveryRecord(restoreId, 'receipt-b', 'Restored B'),
      ]);
      selectFixture(restoreId);
      await waitFor(() => composer.pendingFor(restoreId).length === 2, 'delivery recovery');
      composer.restoreDelivery('receipt-a');
      composer.restoreDelivery('receipt-b');
      const restoredDraft = el.message.value;
      const restorePostCount = posts.length;
      el.composer.requestSubmit();
      await waitFor(() => posts.length === restorePostCount + 1, 'restored draft send');
      const restoredPost = posts.at(-1);

      class ControlledFileReader {
        constructor() {
          this.error = null;
          this.result = null;
          this.aborted = false;
          readers.push(this);
        }
        readAsDataURL(file) { this.file = file; }
        abort() {
          this.aborted = true;
          this.onabort?.();
        }
        succeed(data) {
          this.result = data;
          this.onload?.();
        }
        fail(message) {
          this.error = new Error(message);
          this.onerror?.();
        }
      }
      globalThis.FileReader = ControlledFileReader;

      const clearId = fixtureIds[1];
      selectFixture(clearId);
      type('This local draft must disappear');
      chooseFiles([new File(['clear'], 'clear.png', {type: 'image/png'})]);
      await waitFor(() => readers.length === 1, 'clear reader');
      const clearPosts = posts.length;
      window.confirm = () => true;
      document.querySelector('[data-clear-local-data]').click();
      await waitFor(() => el.message.value === '' && el.attachments.children.length === 0
        && readers[0].aborted, 'browser draft and pending image read cleared');
      window.confirm = originalConfirm;
      el.composer.requestSubmit();
      await new Promise(resolve => setTimeout(resolve, 20));
      const cleared = {
        draft: el.message.value,
        attachmentCount: el.attachments.children.length,
        readerRemoved: readers[0].aborted,
        sent: posts.length - clearPosts,
      };

      const attachmentId = fixtureIds[2];
      selectFixture(attachmentId);
      const firstReader = readers.length;
      chooseFiles([new File(['one'], 'one.png', {type: 'image/png'})]);
      chooseFiles([new File(['two'], 'two.png', {type: 'image/png'})]);
      await waitFor(() => readers.length === firstReader + 2, 'ordered readers');
      type('Caption with both selected images');
      const attachmentPosts = posts.length;
      el.composer.requestSubmit();
      await waitFor(
        () => el['attachment-status'].textContent.includes('Preparing selected images'),
        'visible attachment wait',
      );
      readers[firstReader + 1].succeed('data:image/png;base64,dHdv');
      await new Promise(resolve => setTimeout(resolve, 20));
      const sentBeforeFirstFinished = posts.length - attachmentPosts;
      readers[firstReader].succeed('data:image/png;base64,b25l');
      await waitFor(() => posts.length === attachmentPosts + 1, 'attachment send');
      const attachmentPost = posts.at(-1);
      const switchTarget = agent(fixtureIds[3]);
      const switchOther = agent(fixtureIds[4]);
      composer.remove(switchTarget.id);
      composer.remove(switchOther.id);
      state.agents = [switchTarget, switchOther];
      state.selectedId = null;
      selectAgent(switchTarget.id, {pushHistory: false});
      type('Target session draft');
      const switchReader = readers.length;
      chooseFiles([new File(['switch'], 'switch.png', {type: 'image/png'})]);
      await waitFor(() => readers.length === switchReader + 1, 'switch reader');
      selectAgent(switchOther.id, {pushHistory: false});
      type('Other session draft');
      const switchPosts = posts.length;
      readers[switchReader].succeed('data:image/png;base64,c3dpdGNo');
      await new Promise(resolve => setTimeout(resolve, 20));
      selectAgent(switchTarget.id, {pushHistory: false});
      const switchedAttachment = {
        draft: el.message.value,
        attachment: el.attachments.querySelector('[aria-label^=\"Attachment:\"]')?.getAttribute('aria-label'),
        sent: posts.length - switchPosts,
      };


      const failureId = fixtureIds[5];
      selectFixture(failureId);
      const failureReader = readers.length;
      chooseFiles([new File(['bad'], 'unreadable.png', {type: 'image/png'})]);
      await waitFor(() => readers.length === failureReader + 1, 'failure reader');
      type('Keep this caption after a read failure');
      const failurePosts = posts.length;
      el.composer.requestSubmit();
      readers[failureReader].fail('fixture read failed');
      await waitFor(
        () => el['attachment-status'].textContent.includes('could not be read'),
        'read failure presentation',
      );
      const failedRead = {
        draft: el.message.value,
        sent: posts.length - failurePosts,
        status: el['attachment-status'].textContent,
      };

      return {
        restored: {
          draft: restoredDraft,
          sentMessage: restoredPost.message,
          reusedReceiptIdentity: ['receipt-a', 'receipt-b'].includes(restoredPost.requestId),
        },
        cleared,
        attachments: {
          sentBeforeFirstFinished,
          names: attachmentPost.attachments.map(item => item.name),
          message: attachmentPost.message,
        },
        failedRead,
        switchedAttachment,
      };
    } finally {
      globalThis.FileReader = OriginalFileReader;
      window.confirm = originalConfirm;
      ompDeckApi.request = originalRequest;
      for (const id of fixtureIds) composer.remove(id);
      Object.assign(state, savedState);
      composer.select(state.selectedId);
      renderDetail({selectionChanged: true});
      if (!state.selectedId) showMenu();
    }
  });

  assert.deepEqual(composer.restored, {
    draft: 'Restored A\nRestored B',
    sentMessage: 'Restored A\nRestored B',
    reusedReceiptIdentity: false,
  }, 'restoring two receipts must send the displayed combined draft under a fresh identity');
  assert.deepEqual(composer.cleared, {
    draft: '', attachmentCount: 0, readerRemoved: true, sent: 0,
  }, 'the settings clear action must remove the visible draft, attachments, and pending readers');
  assert.deepEqual(composer.attachments, {
    sentBeforeFirstFinished: 0,
    names: ['one.png', 'two.png'],
    message: 'Caption with both selected images',
  }, 'send must wait for every selected image and preserve selection order');
  assert.deepEqual(composer.switchedAttachment, {
    draft: 'Target session draft', attachment: 'Attachment: switch.png', sent: 0,
  }, 'an image read that finishes after switching must remain owned by its original draft');
  assert.equal(composer.failedRead.draft, 'Keep this caption after a read failure');
  assert.equal(composer.failedRead.sent, 0, 'a failed selected image read must not silently send without it');
  assert.match(composer.failedRead.status, /could not be read/i);

  const pending = await page.evaluate(async () => {
    const savedState = Object.fromEntries(Object.keys(state).map(key => [key, state[key]]));
    const originalRequest = ompDeckApi.request;
    const originalNetwork = ompDeckNetwork;
    const originalRefresh = refresh;
    const calls = [];
    const agent = {
      id: 'browser-state-toggle', lifecycle: 'ongoing', machineId: 'local',
      nodeAvailable: true, bridgeConnected: true,
      capabilities: {answerUiRequest: true, cancelUiRequest: true},
    };
    const toggle = (id, checkedIndices) => ({
      id, kind: 'select', title: 'Toggle choices', questionIndex: 0, allowCancel: true,
      questions: [{
        prompt: 'Choose one action', multi: false, toggleSelection: true, checkedIndices,
        options: [{label: 'Alpha'}, {label: 'Next'}],
      }],
    });
    let toggleAttempts = 0;
    let cancelAttempts = 0;
    let refreshMode = 'toggle';
    ompDeckNetwork = Object.freeze({...originalNetwork, STARTUP_TIMEOUT_MS: 25});
    ompDeckApi.request = async (path, options = {}) => {
      if (!path.includes('/ui-requests/')) return originalRequest(path, options);
      const body = JSON.parse(options.body);
      calls.push({path, body});
      if (path.includes('/toggle-1/')) {
        toggleAttempts += 1;
        if (toggleAttempts === 1) {
          return await new Promise((resolve, reject) => {
            options.signal.addEventListener('abort', () => reject(options.signal.reason), {once: true});
          });
        }
        if (toggleAttempts === 2) {
          return {ok: true, status: 202, json: () => new Promise(() => {})};
        }
      }
      if (path.includes('/cancel-stall/')) {
        cancelAttempts += 1;
        if (cancelAttempts === 1) {
          return await new Promise((resolve, reject) => {
            options.signal.addEventListener('abort', () => reject(options.signal.reason), {once: true});
          });
        }
      }
      return {ok: true, status: 202, json: async () => ({
        requestId: body.requestId, status: 'accepted',
      })};
    };
    refresh = async () => {
      if (refreshMode === 'toggle') agent.pendingUiRequest = toggle('toggle-2', [0, 1]);
      else agent.pendingUiRequest = null;
      renderPendingRequest(agent.pendingUiRequest ? agent : null);
    };

    try {
      state.agents = [agent];
      state.selectedId = agent.id;
      agent.pendingUiRequest = toggle('toggle-1', [0]);
      renderPendingRequest(agent);
      const next = el['pending-request'].querySelectorAll('[name="pending-choice"]')[1];
      next.click();
      await settlePendingRequest();
      const headerTimeout = {
        checked: next.checked,
        status: el['pending-request'].querySelector('[role="status"]').textContent,
        disabled: next.disabled,
      };
      await settlePendingRequest();
      const bodyTimeout = {
        checked: next.checked,
        status: el['pending-request'].querySelector('[role="status"]').textContent,
        disabled: next.disabled,
      };
      await settlePendingRequest();
      const reissued = {
        requestId: el['pending-request'].querySelector('[data-pending-form]')?.dataset.requestId,
        checked: [...el['pending-request'].querySelectorAll('[name="pending-choice"]')]
          .map(input => input.checked),
      };

      refreshMode = 'multi';
      agent.pendingUiRequest = {
        id: 'ordinary-multi', kind: 'select', title: 'Ordinary multi', questionIndex: 0,
        questions: [{multi: true, options: [
          {label: 'One', value: 'one'}, {label: 'Two', value: 'two'},
        ]}],
      };
      renderPendingRequest(agent);
      el['pending-request'].querySelectorAll('[name="pending-choice"]')[0].click();
      el['pending-request'].querySelectorAll('[name="pending-choice"]')[1].click();
      await settlePendingRequest();

      refreshMode = 'cancel';
      agent.pendingUiRequest = {
        id: 'cancel-stall', kind: 'confirm', title: 'Cancel safely', questionIndex: 0,
        allowCancel: true, questions: [{}],
      };
      renderPendingRequest(agent);
      await settlePendingRequest({cancel: true});
      const cancelStatus = el['pending-request'].querySelector('[role="status"]').textContent;
      await settlePendingRequest({cancel: true});

      return {calls, headerTimeout, bodyTimeout, reissued, cancelStatus};
    } finally {
      clearPendingRequestAttempts();
      refresh = originalRefresh;
      ompDeckNetwork = originalNetwork;
      ompDeckApi.request = originalRequest;
      Object.assign(state, savedState);
      renderDetail({selectionChanged: true});
    }
  });

  assert.deepEqual(pending.calls.slice(0, 3).map(call => call.body.value), ['Next', 'Next', 'Next']);
  assert.equal(new Set(pending.calls.slice(0, 3).map(call => call.body.requestId)).size, 1,
    'header/body timeout retries must retain one attempt identity');
  for (const timeout of [pending.headerTimeout, pending.bodyTimeout]) {
    assert.equal(timeout.checked, true, 'a stalled toggle answer must remain visibly selected');
    assert.equal(timeout.disabled, false, 'bounded failure must restore the answer controls');
    assert.match(timeout.status, /Outcome uncertain; retry will use the same request/i);
  }
  assert.deepEqual(pending.reissued, {requestId: 'toggle-2', checked: [true, true]},
    'accepted toggle action must render the host-reissued request and authoritative checks');
  assert.deepEqual(pending.calls[3].body.value, ['one', 'two'],
    'ordinary multi-choice requests must continue to send arrays');
  assert.equal(pending.calls[4].body.value, undefined);
  assert.equal(pending.calls[4].body.requestId, pending.calls[5].body.requestId,
    'a stalled cancellation must retry under the same operation identity');
  assert.match(pending.cancelStatus, /Outcome uncertain; retry will use the same request/i);

  await pause(0);
}
