import assert from 'node:assert/strict';

const FIXTURE_ID = 'composer-parity-session';

async function nextPaint(page) {
  await page.evaluate(() => new Promise(resolve => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
}

export async function verifyComposerParity(page) {
  const originalViewport = page.viewport();

  await page.evaluate(fixtureId => {
    globalThis.__composerParityRestore = {
      state: Object.fromEntries(Object.keys(state).map(key => [key, state[key]])),
      request: ompDeckApi.request,
      appClass: el.app.className,
      bodyClass: document.body.className,
      workspaceClass: el.workspace.className,
      documentScroll: {left: window.scrollX, top: window.scrollY},
      detailScroll: el.detail.scrollTop,
      activityScroll: el.activity.scrollTop,
    };

    const agent = {
      id: fixtureId,
      lifecycle: 'ongoing',
      project: 'parity-fixture',
      label: 'Composer parity fixture',
      machineName: 'Fixture machine',
      machineId: 'local',
      nodeAvailable: true,
      status: 'idle',
      executionState: 'idle',
      inboxState: 'read',
      bridgeConnected: true,
      capabilities: {sendMessage: true, attachments: true},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      pendingUiRequest: null,
      subagents: [],
      session: {
        activity: [
          {
            id: 'composer-parity-older',
            kind: 'user',
            title: 'You',
            detail: 'Older fixture request',
            time: '2026-09-15T10:00:00.000Z',
          },
          {
            id: 'composer-parity-newer',
            kind: 'assistant',
            title: 'OMP',
            detail: 'Newest fixture response',
            time: '2026-09-15T10:01:00.000Z',
          },
        ],
        archiveActivity: [],
        todo: '',
        updatedMs: Date.parse('2026-09-15T10:01:00.000Z'),
      },
    };
    state.agents = [agent];
    state.selectedId = null;
    state.pendingSelection = null;
    state.filter = 'all';
    state.sessionScope = 'ongoing';
    state.showTodo = false;
    state.showArchive = false;
    state.renderedRevision = -1;
    state.renderedSessionId = null;
    state.renderedUpdatedMs = null;
    selectAgent(fixtureId, {pushHistory: false});
  }, FIXTURE_ID);

  try {
    await page.setViewport({width: 390, height: 844, deviceScaleFactor: 1});
    const messages = await page.evaluate(() => [...document.querySelectorAll('#activity .event-detail')]
      .map(element => element.textContent.trim()));
    assert.deepEqual(messages, ['Newest fixture response', 'Older fixture request']);
    await page.evaluate(() => {
      globalThis.__composerParityRequests = [];
      ompDeckApi.request = async (path, options) => {
        globalThis.__composerParityRequests.push({path, body: JSON.parse(options.body)});
        return {ok: true, status: 202, json: async () => ({})};
      };
      const message = document.getElementById('message');
      message.value = 'Mobile draft';
      message.selectionStart = message.value.length;
      message.selectionEnd = message.value.length;
      message.dispatchEvent(new Event('input', {bubbles: true}));
      message.scrollIntoView({block: 'nearest'});
    });
    await page.focus('#message');
    await page.keyboard.press('Enter');
    const enter = await page.evaluate(() => ({
      value: document.getElementById('message').value,
      requests: globalThis.__composerParityRequests.length,
    }));
    assert.equal(enter.value, 'Mobile draft\n', 'mobile Enter must insert a newline');
    assert.equal(enter.requests, 0, 'mobile Enter must not submit the composer');

    await page.click('#send');
    await page.waitForFunction(() => globalThis.__composerParityRequests.length === 1);
    const explicitSend = await page.evaluate(() => globalThis.__composerParityRequests[0]);
    assert.equal(explicitSend.path, `/api/sessions/${FIXTURE_ID}/messages`);
    assert.equal(explicitSend.body.message, 'Mobile draft');
  } finally {
    if (originalViewport) await page.setViewport(originalViewport);
    await page.evaluate(() => {
      const saved = globalThis.__composerParityRestore;
      if (!saved) return;
      ompDeckApi.request = saved.request;
      const validIds = saved.state.agents.map(agent => agent.id);
      composer.prune(validIds);
      Object.assign(state, saved.state);
      state.renderedRevision = -1;
      renderSummary();
      renderList();
      renderDetail({selectionChanged: true});
      el.app.className = saved.appClass;
      document.body.className = saved.bodyClass;
      el.workspace.className = saved.workspaceClass;
      requestAnimationFrame(() => {
        el.detail.scrollTop = saved.detailScroll;
        el.activity.scrollTop = saved.activityScroll;
        window.scrollTo(saved.documentScroll.left, saved.documentScroll.top);
      });
      delete globalThis.__composerParityRequests;
      delete globalThis.__composerParityRestore;
    });
    await nextPaint(page);
  }
}
