import assert from 'node:assert/strict';

const IDS = Object.freeze({
  source: 'fresh-action-source',
  other: 'fresh-action-other',
  success: 'fresh-action-success',
  retry: 'fresh-action-retry',
  switched: 'fresh-action-switched',
  logout: 'fresh-action-logout',
});

export async function verifyFreshSessionAction(page) {
  const result = await page.evaluate(async ids => {
    const savedState = Object.fromEntries(Object.keys(state).map(key => [key, state[key]]));
    const savedNodes = new Map([...state.nodes].map(([id, node]) => [id, {
      sessions: node.sessions instanceof Map ? new Map(node.sessions) : node.sessions,
      snapshotGeneration: node.snapshotGeneration,
      presentationRevision: node.presentationRevision,
    }]));
    const savedUrl = location.href;
    const savedHistoryState = history.state;
    const originalRequest = ompDeckApi.request;
    const originalLogout = ompDeckApi.logout;
    const originalConfirm = globalThis.confirm;
    const originalSettingsBusy = settingsBusy;
    const fixtureIds = Object.values(ids);
    const calls = [];
    const prompts = [];
    let confirmResult = true;
    let freshHandler = null;
    let rejectLogout = null;

    const waitFor = async (predicate, label) => {
      const deadline = performance.now() + 2_000;
      while (!predicate()) {
        if (performance.now() >= deadline) throw new Error(`Fresh-session case timed out: ${label}`);
        await new Promise(resolve => setTimeout(resolve, 10));
      }
    };
    const response = (payload, status = 200) => ({
      ok: status >= 200 && status < 300,
      status,
      json: async () => payload,
    });
    const draftResponse = () => response({
      enabled: false, revision: null, text: '', images: [], updatedAt: null, expiresAt: null,
    });
    const rawSession = id => ({
      id,
      lifecycle: 'ongoing',
      state: 'idle',
      title: `Fresh ${id}`,
      project: {name: 'demo', path: '/fixture/demo'},
      model: {provider: 'fixture', id: 'model'},
      thinking: 'medium',
      thinkingLevels: ['low', 'medium', 'high'],
      unread: false,
      capabilities: {sendMessage: true, attachments: true, startFresh: true},
      attachmentLimits: {maxImages: 4, maxTotalBytes: 20 * 1024 * 1024},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      messages: [], todos: [], subagents: [], pendingUiRequest: null,
      updatedAt: new Date().toISOString(),
    });
    const makeAgent = (id, label, startFresh) => ({
      id, localId: id, machineId: 'local', machineName: 'Local',
      machineOrigin: ompDeckApi.machine('local')?.origin || location.origin,
      project: 'demo', cwd: '/fixture/demo', label, lifecycle: 'ongoing',
      nodeAvailable: true, bridgeConnected: true, executionState: 'idle', status: 'idle',
      inboxState: 'read', capabilities: {
        sendMessage: true, attachments: true, compact: true, shutdown: true,
        ...(startFresh ? {startFresh: true} : {}),
      },
      attachmentLimits: {maxImages: 4, maxTotalBytes: 20 * 1024 * 1024},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      pendingUiRequest: null, subagents: [],
      session: {
        model: 'fixture/model', thinking: 'medium',
        activity: [{kind: 'assistant', title: 'OMP', detail: 'Existing source conversation', time: null}],
        archiveActivity: [], todos: [], updatedMs: Date.now(),
      },
    });
    const openKebab = () => {
      if (!el['session-menu'].hidden) closeSessionMenu();
      el['overflow-button'].click();
    };
    const clickFresh = () => {
      openKebab();
      el['session-menu'].querySelector('[data-session-action="fresh"]').click();
    };

    try {
      globalThis.confirm = message => {
        prompts.push(message);
        return confirmResult;
      };
      ompDeckApi.request = async (path, options = {}) => {
        if (path.endsWith('/draft')) return draftResponse();
        if (path.endsWith('/deliveries')) return response({version: 1, deliveries: []});
        if (!path.endsWith('/fresh')) throw new Error(`Unexpected request: ${path}`);
        const body = JSON.parse(options.body);
        calls.push({path, body});
        if (!freshHandler) throw new Error('Fresh request was not expected');
        return await freshHandler(path, options, body);
      };

      const source = makeAgent(ids.source, 'Source session', false);
      const other = makeAgent(ids.other, 'Other session', true);
      state.agents = [source, other];
      state.selectedId = null;
      selectAgent(source.id, {pushHistory: false});
      await new Promise(resolve => setTimeout(resolve, 0));

      openKebab();
      const freshButton = el['session-menu'].querySelector('[data-session-action="fresh"]');
      const hiddenWithoutCapability = freshButton.hidden;
      closeSessionMenu();
      source.capabilities.startFresh = true;
      renderDetail();
      el.message.value = '/fresh';
      el.message.dispatchEvent(new Event('input', {bubbles: true}));
      const slashAliasVisible = !el['command-menu'].hidden
        && el['command-menu'].textContent.includes('/fresh');

      el.message.value = 'Keep source text';
      el.message.dispatchEvent(new Event('input', {bubbles: true}));
      const files = new DataTransfer();
      files.items.add(new File([
        Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM1sAAAAASUVORK5CYII='), value => value.charCodeAt(0)),
      ], 'fresh-source.png', {type: 'image/png'}));
      el.attach.files = files.files;
      el.attach.dispatchEvent(new Event('change', {bubbles: true}));
      await waitFor(() => {
        const image = el.attachments.querySelector('img');
        return image?.complete && image.naturalWidth === 1;
      }, 'decoded source image');

      confirmResult = false;
      const beforeCancel = calls.length;
      clickFresh();
      await new Promise(resolve => setTimeout(resolve, 0));
      const cancelled = {
        requests: calls.length - beforeCancel,
        selected: state.selectedId,
        draft: el.message.value,
        images: el.attachments.querySelectorAll('img').length,
        confirmationShown: prompts.length === 1,
      };

      confirmResult = true;
      let releaseSuccess;
      freshHandler = (_path, _options, body) => new Promise(resolve => {
        releaseSuccess = () => resolve(response({
          requestId: body.requestId, status: 'accepted', session: rawSession(ids.success),
        }, 201));
      });
      clickFresh();
      await waitFor(() => calls.length === beforeCancel + 1, 'held success request');
      reconcileMachineSessionActions({renewedIds: ['local']});
      openKebab();
      const pending = {disabled: freshButton.disabled};
      freshButton.click();
      closeSessionMenu();
      await new Promise(resolve => setTimeout(resolve, 0));
      const callsWhilePending = calls.length;
      releaseSuccess();
      await waitFor(() => state.selectedId === ids.success, 'fresh session selection');
      const successCall = calls.at(-1);
      const freshEmpty = {
        events: el.activity.querySelectorAll('.event').length,
        draft: el.message.value,
        images: el.attachments.querySelectorAll('img').length,
      };
      selectAgent(source.id, {pushHistory: false});
      await waitFor(() => el.message.value === 'Keep source text', 'source draft restoration');
      const sourcePreserved = {
        draft: el.message.value,
        images: el.attachments.querySelectorAll('img').length,
        conversation: el.activity.textContent.includes('Existing source conversation'),
        decoded: Boolean(el.attachments.querySelector('img')?.naturalWidth),
      };

      let retryAttempt = 0;
      freshHandler = async (_path, _options, body) => {
        retryAttempt += 1;
        if (retryAttempt === 1) {
          reconcileMachineSessionActions({renewedIds: ['local']});
          throw new TypeError('response lost');
        }
        return response({requestId: body.requestId, status: 'accepted', session: rawSession(ids.retry)}, 201);
      };
      const retryStart = calls.length;
      clickFresh();
      await waitFor(() => sessionActionStates.get(sessionActionKey(source, 'fresh')) === 'uncertain', 'uncertain action');
      openKebab();
      const uncertain = {
        enabled: !freshButton.disabled,
        visible: !freshButton.hidden,
      };
      freshButton.click();
      await waitFor(() => state.selectedId === ids.retry, 'uncertain retry success');
      const retryCalls = calls.slice(retryStart);

      selectAgent(source.id, {pushHistory: false});
      let releaseSwitch;
      freshHandler = (_path, _options, body) => new Promise(resolve => {
        releaseSwitch = () => resolve(response({
          requestId: body.requestId, status: 'accepted', session: rawSession(ids.switched),
        }, 201));
      });
      const switchStart = calls.length;
      clickFresh();
      await waitFor(() => calls.length === switchStart + 1, 'switch request');
      selectAgent(other.id, {pushHistory: false});
      releaseSwitch();
      await waitFor(() => state.agents.some(agent => agent.id === ids.switched), 'late switched integration');
      const switched = {selected: state.selectedId, integrated: state.agents.some(agent => agent.id === ids.switched)};

      selectAgent(source.id, {pushHistory: false});
      let releaseLogoutFresh;
      freshHandler = (_path, _options, body) => new Promise(resolve => {
        releaseLogoutFresh = () => resolve(response({
          requestId: body.requestId, status: 'accepted', session: rawSession(ids.logout),
        }, 201));
      });
      let logoutStarted = false;
      ompDeckApi.logout = () => new Promise((_, reject) => {
        logoutStarted = true;
        rejectLogout = reject;
      });
      const logoutStart = calls.length;
      clickFresh();
      await waitFor(() => calls.length === logoutStart + 1, 'logout-fenced request');
      showDeckSettings(el['overflow-button']);
      el['deck-settings'].querySelector('[data-session-action="logout"]').click();
      await waitFor(() => logoutStarted, 'logout start');
      releaseLogoutFresh();
      await new Promise(resolve => setTimeout(resolve, 20));
      const logoutFenced = {
        selected: state.selectedId,
        integrated: state.agents.some(agent => agent.id === ids.logout),
      };
      rejectLogout(new Error('Fixture kept authentication active'));
      await waitFor(() => !settingsBusy, 'logout fixture cleanup');

      return {
        hiddenWithoutCapability,
        slashAliasVisible,
        cancelled,
        pending,
        callsWhilePending,
        success: {
          path: successCall.path,
          bodyKeys: Object.keys(successCall.body),
          status: freshEmpty,
          source: sourcePreserved,
        },
        uncertain,
        retryIds: retryCalls.map(call => call.body.requestId),
        switched,
        logoutFenced,
      };
    } finally {
      ompDeckApi.request = originalRequest;
      ompDeckApi.logout = originalLogout;
      globalThis.confirm = originalConfirm;
      rejectLogout?.(new Error('Fresh-session fixture cleanup'));
      setSettingsBusy(originalSettingsBusy);
      closeDialog(el['deck-settings'], {restoreFocus: false});
      closeSessionMenu();
      clearSessionActionAttempts();
      for (const id of fixtureIds) composer.remove(id);
      Object.assign(state, savedState);
      for (const [id, saved] of savedNodes) {
        const node = state.nodes.get(id);
        if (!node) continue;
        node.sessions = saved.sessions;
        node.snapshotGeneration = saved.snapshotGeneration;
        node.presentationRevision = saved.presentationRevision;
      }
      history.replaceState(savedHistoryState, '', savedUrl);
      touchRender();
      renderSummary();
      renderList();
      renderDetail({selectionChanged: true});
      el.toast.hidden = true;
      el.toast.textContent = '';
      el['settings-status'].textContent = '';
    }
  }, IDS);

  assert.equal(result.hiddenWithoutCapability, true,
    'the fresh action must be hidden unless the normalized capability is advertised');
  assert.equal(result.slashAliasVisible, false, 'fresh creation must remain a kebab-only action');
  assert.deepEqual(result.cancelled, {
    requests: 0, selected: IDS.source, draft: 'Keep source text', images: 1,
    confirmationShown: true,
  });
  assert.deepEqual(result.pending, {disabled: true});
  assert.equal(result.callsWhilePending, 1, 'a pending fresh action must ignore duplicate clicks');
  assert.equal(result.success.path, `/api/sessions/${IDS.source}/fresh`);
  assert.deepEqual(result.success.bodyKeys, ['requestId'], 'fresh creation sends only its stable identity');
  assert.equal(result.success.status.events, 0, 'the fresh session must display no copied conversation');
  assert.deepEqual(
    {draft: result.success.status.draft, images: result.success.status.images},
    {draft: '', images: 0},
    'the accepted fresh session opens with no copied browser draft',
  );
  assert.deepEqual(result.success.source, {
    draft: 'Keep source text', images: 1, conversation: true, decoded: true,
  }, 'creating a fresh session must preserve the source conversation and decoded text/image draft');
  assert.deepEqual(result.uncertain, {
    enabled: true, visible: true,
  });
  assert.equal(result.retryIds.length, 2);
  assert.equal(new Set(result.retryIds).size, 1,
    'an uncertain retry must reuse the original fresh request identity');
  assert.deepEqual(result.switched, {selected: IDS.other, integrated: true},
    'a late fresh response must integrate without stealing focus after navigation');
  assert.deepEqual(result.logoutFenced, {selected: IDS.source, integrated: false},
    'logout initiation must fence a late fresh response');
}
