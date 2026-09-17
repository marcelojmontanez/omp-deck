import assert from 'node:assert/strict';

const PRIMARY_ID = 'session-action-primary';
const OTHER_ID = 'session-action-other';

export async function verifySessionActions(page) {
  const result = await page.evaluate(async ({primaryId, otherId}) => {
    const savedState = Object.fromEntries(Object.keys(state).map(key => [key, state[key]]));
    const originalRequest = ompDeckApi.request;
    const originalConfirm = globalThis.confirm;
    const originalSetTimeout = globalThis.setTimeout;
    const makeAgent = (id, label) => ({
      id, machineId: 'local', project: 'demo', label, lifecycle: 'ongoing',
      nodeAvailable: true, bridgeConnected: true, executionState: 'idle', inboxState: 'read',
      capabilities: {compact: true, shutdown: true},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      pendingUiRequest: null, subagents: [],
      session: {activity: [], archiveActivity: [], updatedMs: Date.now()},
    });
    const acceptedResponse = options => {
      const {requestId} = JSON.parse(options.body);
      return {ok: true, status: 202, json: async () => ({requestId, status: 'accepted'})};
    };
    const primary = makeAgent(primaryId, 'Primary action');
    const other = makeAgent(otherId, 'Other action');

    try {
      globalThis.confirm = () => true;
      state.agents = [primary, other];
      state.selectedId = primary.id;
      renderDetail({selectionChanged: true});

      let releasePrimary;
      const calls = [];
      ompDeckApi.request = (path, options) => {
        calls.push({path, requestId: JSON.parse(options.body).requestId});
        if (path.includes(primary.id)) {
          return new Promise(resolve => { releasePrimary = () => resolve(acceptedResponse(options)); });
        }
        return Promise.resolve(acceptedResponse(options));
      };
      el.message.value = 'Newer composer draft';
      const rename = el['session-menu'].querySelector('[data-session-action="rename"]');
      showSessionNameEditor(rename);
      await Promise.resolve();
      el['session-name-input'].value = 'Newer rename draft';
      el['session-name-input'].focus();
      const primaryPending = runSessionAction('compact', primary);
      const pendingButton = el['session-menu'].querySelector('[data-session-action="compact"]');
      const immediate = {
        disabled: pendingButton.disabled,
        text: pendingButton.textContent,
        toast: el.toast.textContent,
      };
      const duplicate = await runSessionAction('compact', primary);
      const otherAccepted = await runSessionAction('compact', other);
      const drafts = {
        composer: el.message.value,
        rename: el['session-name-input'].value,
        dialogOpen: el['session-name-dialog'].open,
        focus: document.activeElement.id,
      };
      closeDialog(el['session-name-dialog'], {restoreFocus: false});
      state.selectedId = other.id;
      renderDetail({selectionChanged: true});
      openSessionMenu();
      const otherText = pendingButton.textContent;
      closeSessionMenu();
      state.selectedId = primary.id;
      renderDetail({selectionChanged: true});
      openSessionMenu();
      const primaryText = pendingButton.textContent;
      closeSessionMenu();
      releasePrimary();
      const primaryAccepted = await primaryPending;

      clearSessionActionAttempts();
      const retryIds = [];
      let retryCall = 0;
      ompDeckApi.request = (_path, options) => {
        retryCall += 1;
        retryIds.push(JSON.parse(options.body).requestId);
        if (retryCall === 1) {
          return Promise.resolve({
            ok: true,
            status: 202,
            json: () => new Promise((_, reject) => {
              const rejectTimeout = () => reject(options.signal.reason);
              if (options.signal.aborted) rejectTimeout();
              else options.signal.addEventListener('abort', rejectTimeout, {once: true});
            }),
          });
        }
        if (retryCall === 2) return Promise.reject(new TypeError('response lost'));
        return Promise.resolve(acceptedResponse(options));
      };
      globalThis.setTimeout = (callback, delay, ...args) => (
        originalSetTimeout(callback, Math.min(delay, 10), ...args)
      );
      const stalled = await runSessionAction('compact', primary);
      globalThis.setTimeout = originalSetTimeout;
      openSessionMenu();
      const uncertain = {text: pendingButton.textContent, title: pendingButton.title};
      closeSessionMenu();
      const lost = await runSessionAction('compact', primary);
      const retried = await runSessionAction('compact', primary);

      clearSessionActionAttempts();
      const failedIds = [];
      ompDeckApi.request = (_path, options) => {
        failedIds.push(JSON.parse(options.body).requestId);
        if (failedIds.length === 1) {
          return Promise.resolve({
            ok: false,
            status: 400,
            json: async () => ({
              status: 'failed', errorCode: 'command', error: 'Compact rejected',
            }),
          });
        }
        return Promise.resolve(acceptedResponse(options));
      };
      const failed = await runSessionAction('compact', primary);
      openSessionMenu();
      const failedPresentation = {text: pendingButton.textContent, title: pendingButton.title};
      closeSessionMenu();
      const afterFailure = await runSessionAction('compact', primary);

      clearSessionActionAttempts();
      const orderedIds = [];
      let releaseOld;
      ompDeckApi.request = (_path, options) => {
        orderedIds.push(JSON.parse(options.body).requestId);
        if (orderedIds.length === 1) {
          return new Promise(resolve => { releaseOld = () => resolve(acceptedResponse(options)); });
        }
        return Promise.resolve(acceptedResponse(options));
      };
      const oldOperation = runSessionAction('compact', primary);
      clearSessionActionAttempts();
      const newerAccepted = await runSessionAction('compact', primary);
      const acceptedToast = el.toast.textContent;
      releaseOld();
      const staleAccepted = await oldOperation;
      const toastPreserved = el.toast.textContent === acceptedToast;

      clearSessionActionAttempts();
      ompDeckApi.request = (_path, options) => Promise.resolve(acceptedResponse(options));
      const quitAccepted = await runSessionAction('quit', primary);
      openSessionMenu();
      const quitPresentation = {
        accepted: quitAccepted,
        text: el['session-menu'].querySelector('[data-session-action="quit"]').textContent,
        retained: state.agents.includes(primary),
        lifecycle: primary.lifecycle,
      };
      closeSessionMenu();

      return {
        immediate, duplicate, otherAccepted, primaryAccepted, calls, drafts,
        scoped: {otherText, primaryText},
        uncertain, stalled, lost, retried, retryIds,
        failure: {
          failed,
          presentation: failedPresentation,
          afterFailure,
          newIdentity: failedIds[0] !== failedIds[1],
        },
        ordering: {
          distinctIds: orderedIds[0] !== orderedIds[1],
          newerAccepted,
          staleAccepted,
          toastPreserved,
        },
        quitPresentation,
      };
    } finally {
      globalThis.setTimeout = originalSetTimeout;
      ompDeckApi.request = originalRequest;
      globalThis.confirm = originalConfirm;
      closeDialog(el['session-name-dialog'], {restoreFocus: false});
      closeSessionMenu();
      clearSessionActionAttempts();
      Object.assign(state, savedState);
      touchRender();
      renderSummary();
      renderList();
      renderDetail({selectionChanged: true});
      el.toast.hidden = true;
    }
  }, {primaryId: PRIMARY_ID, otherId: OTHER_ID});

  assert.deepEqual(result.immediate, {
    disabled: true,
    text: 'Compact context…',
    toast: 'Primary action: Requesting compact context…',
  });
  assert.equal(result.duplicate, false, 'a pending action must ignore duplicate activation');
  assert.equal(result.otherAccepted, true, 'a pending session must not block another session');
  assert.equal(result.primaryAccepted, true);
  assert.equal(result.calls.filter(call => call.path.includes(PRIMARY_ID)).length, 1);
  assert.deepEqual(result.drafts, {
    composer: 'Newer composer draft',
    rename: 'Newer rename draft',
    dialogOpen: true,
    focus: 'session-name-input',
  });
  assert.deepEqual(result.scoped, {
    otherText: 'Compact context accepted',
    primaryText: 'Compact context…',
  });
  assert.equal(result.stalled, false);
  assert.equal(result.lost, false);
  assert.equal(result.retried, true);
  assert.equal(new Set(result.retryIds).size, 1,
    'stalled and lost responses must retain one safe retry identity');
  assert.deepEqual(result.uncertain, {
    text: 'Retry compact context',
    title: 'The outcome is uncertain. Retry safely checks the same request.',
  });
  assert.deepEqual(result.failure, {
    failed: false,
    presentation: {text: 'Retry compact context', title: ''},
    afterFailure: true,
    newIdentity: true,
  });
  assert.deepEqual(result.ordering, {
    distinctIds: true,
    newerAccepted: true,
    staleAccepted: false,
    toastPreserved: true,
  });
  assert.deepEqual(result.quitPresentation, {
    accepted: true,
    text: 'Quit accepted',
    retained: true,
    lifecycle: 'ongoing',
  });
}
