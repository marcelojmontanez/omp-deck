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
      cwd: '/projects/parity-fixture/<private-directory>',
      machineName: 'Fixture machine',
      machineId: 'local',
      nodeAvailable: true,
      status: 'idle',
      executionState: 'idle',
      inboxState: 'read',
      bridgeConnected: true,
      capabilities: {sendMessage: true, attachments: true, compact: true, shutdown: true, setThinking: true},
      attachmentLimits: {maxImages: 4, maxTotalBytes: 20 * 1024 * 1024},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      pendingUiRequest: null,
      subagents: [],
      session: {
        model: 'openai/gpt-model-with-a-long-provider-qualified-name',
        thinking: 'high',
        thinkingLevels: ['off', 'medium', 'high'],
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
    state.agents = [
      agent,
      {...agent, id: `${fixtureId}-second`, project: 'second-project', label: 'A different conversation'},
      {...agent, id: `${fixtureId}-history`, lifecycle: 'history', project: 'saved-project'},
    ];
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
    await verifyAttachmentLayout(page);
    const messages = await page.evaluate(() => [...document.querySelectorAll('#activity .event-detail')]
      .map(element => element.textContent.trim()));
    assert.deepEqual(messages, ['Newest fixture response', 'Older fixture request']);
    await page.evaluate(() => {
      globalThis.__composerParityRequests = [];
      ompDeckApi.request = async (path, options) => {
        if (path.endsWith('/draft')) return {ok: true, status: 200, json: async () => ({
          enabled: false, revision: null, text: '', images: [], updatedAt: null, expiresAt: null,
        })};
        if (path.endsWith('/deliveries')) return {ok: true, json: async () => ({version: 1, deliveries: []})};
        const body = JSON.parse(options.body);
        globalThis.__composerParityRequests.push({path, body});
        return {ok: true, status: 202, json: async () => ({requestId: body.requestId, status: 'accepted', acceptance: 'accepted'})};
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

    await page.keyboard.type('Continue writing after Send');
    assert.equal(await page.$eval('#message', input => input.value), 'Continue writing after Send',
      'Sending must keep the composer ready for the next keystroke');
    await page.evaluate(() => {
      window.__composerFocusFailure = {request: ompDeckApi.request};
      ompDeckApi.request = () => new Promise((_, reject) => {
        window.__composerFocusFailure.reject = reject;
      });
    });
    try {
      await page.click('#send');
      await page.waitForFunction(() => Boolean(window.__composerFocusFailure.reject));
      await page.focus('#attach');
      await page.evaluate(() => window.__composerFocusFailure.reject(new TypeError('Connection lost')));
      await page.waitForFunction(() => document.getElementById('message').value === 'Continue writing after Send');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'attach',
        'A delayed send failure must not steal focus from the gallery control');
    } finally {
      await page.evaluate(() => {
        ompDeckApi.request = window.__composerFocusFailure.request;
        delete window.__composerFocusFailure;
        el.message.value = '';
        el.message.dispatchEvent(new Event('input', {bubbles: true}));
      });
    }
    for (const supported of [false, true]) {
      const gallery = await page.evaluate(supported => {
        selectedAgent().capabilities.attachments = supported;
        touchRender();
        renderDetail();
        const reason = document.getElementById('attachment-status');
        return {disabled: el.attach.disabled, reasonVisible: Boolean(reason?.checkVisibility())};
      }, supported);
      assert.deepEqual(gallery, {disabled: !supported, reasonVisible: !supported},
        'Unavailable image uploads must have a visible explanation, not a silent active-looking control');
    }

    await page.click('#overflow-button');
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await page.waitForSelector('#session-info[open]', {visible: true});
    assert((await page.$eval('#session-metadata', node => node.textContent))
      .includes('/projects/parity-fixture/<private-directory>'));
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => document.activeElement.id === 'overflow-button');
    await page.click('#overflow-button');
    await page.keyboard.press('End');
    await page.keyboard.press('Enter');
    await page.waitForSelector('#deck-settings[open]', {visible: true});
    await page.keyboard.press('Escape');

    for (const width of [320, 390, 1440]) {
      await page.setViewport({width, height: 844, deviceScaleFactor: 1});
      await nextPaint(page);
      const toolbar = await page.evaluate(() => {
        const rect = selector => {
          const {x, y, width, height} = document.querySelector(selector).getBoundingClientRect();
          return {x, y, width, height};
        };
        return {
          model: rect('.model-chip'), thinking: rect('[data-thinking-level]'),
          menu: rect('#overflow-button'), attach: rect('#attach'), send: rect('#send'),
          header: rect('.detail-header'),
          overflow: document.documentElement.scrollWidth > innerWidth,
        };
      });
      assert.equal(toolbar.overflow, false, `no page overflow at ${width}px`);
      for (const control of [toolbar.thinking, toolbar.menu, toolbar.attach, toolbar.send]) {
        assert(control.width >= 44 && control.height >= 44, `touch target at ${width}px`);
      }
      assert(toolbar.model.width > 0, `model remains visible at ${width}px`);
      assert(Math.abs(toolbar.thinking.y - toolbar.menu.y) <= 1, 'chat controls share one row');
      assert(toolbar.header.height < 72, 'routine identity metadata must not add header rows');

      await page.click('#overflow-button');
      await page.keyboard.press('Enter');
      const cards = await page.$$eval('.agent-card', nodes => nodes.map(node => {
        const {x, width} = node.getBoundingClientRect();
        const identity = ['.agent-name', '.agent-project', '.agent-machine-line']
          .map(selector => node.querySelector(selector));
        return {
          name: node.querySelector('.agent-name').textContent,
          contained: x >= 0 && x + width <= innerWidth && identity.every(element => (
            element.scrollWidth <= element.clientWidth && element.scrollHeight <= element.clientHeight
          )),
        };
      }));
      assert.deepEqual(cards.map(card => card.name).sort(), ['A different conversation', 'Composer parity fixture']);
      assert(cards.every(card => card.contained), `session names, projects and machines remain visible at ${width}px`);
      await page.click('.appbar [data-deck-settings]');
      await page.waitForSelector('#deck-settings[open]', {visible: true});
      await page.keyboard.press('Escape');
      await page.click(`[data-id="${FIXTURE_ID}"]`);
    }

    await page.focus('#message');
    await page.type('#message', '/co');
    await page.waitForSelector('#command-menu:not([hidden])');
    await page.keyboard.press('Tab');
    assert.equal(await page.$eval('#message', node => node.value), '/compact');
    assert.equal(await page.$eval('#send', node => node.textContent), 'Run');
    page.once('dialog', dialog => { void dialog.dismiss(); });
    await page.click('#send');
    await nextPaint(page);
    assert.equal(await page.$eval('#message', node => node.value), '/compact', 'cancel keeps draft');
    assert.equal(await page.evaluate(() => __composerParityRequests.length), 1);

    await page.evaluate(() => {
      const request = ompDeckApi.request;
      ompDeckApi.request = (path, options) => path.endsWith('/compact')
        ? new Promise(resolve => { globalThis.__finishCommand = resolve; })
        : request(path, options);
    });
    page.once('dialog', dialog => { void dialog.accept(); });
    await page.click('#send');
    await page.waitForFunction(() => typeof globalThis.__finishCommand === 'function');
    await page.focus('#message');
    await page.keyboard.press('End');
    await page.keyboard.type('x');
    await page.keyboard.press('Backspace');
    await page.evaluate(() => globalThis.__finishCommand({ok: true, status: 202}));
    await nextPaint(page);
    assert.equal(await page.$eval('#message', node => node.value), '/compact',
      'delayed acceptance must not clear a newer edit, even when its text matches');

    await page.evaluate(() => {
      el.message.value = '/thinking high';
      el.message.dispatchEvent(new Event('input', {bubbles: true}));
      selectedAgent().capabilities.setThinking = false;
      selectedAgent().session.thinkingLevels = [];
      touchRender();
      renderDetail();
    });
    await page.click('#send');
    await nextPaint(page);
    assert.equal(await page.$eval('#message', node => node.value), '/thinking high');
    assert.equal(await page.evaluate(() => __composerParityRequests.length), 1,
      'withdrawn thinking capability must not turn a command into a prompt');
    assert(await page.$eval('#toast', node => !node.hidden && node.classList.contains('error')));

    await page.evaluate(fixtureId => selectAgent(`${fixtureId}-second`, {pushHistory: false}), FIXTURE_ID);
    await page.type('#message', 'Another session draft');
    await page.evaluate(fixtureId => selectAgent(fixtureId, {pushHistory: false}), FIXTURE_ID);
    assert.equal(await page.$eval('#message', node => node.value), '/thinking high');
    await page.evaluate(fixtureId => selectAgent(`${fixtureId}-history`, {pushHistory: false}), FIXTURE_ID);
    assert.equal(await page.$eval('#composer', node => node.hidden), true, 'history has no dead composer');
    await page.evaluate(fixtureId => selectAgent(`${fixtureId}-second`, {pushHistory: false}), FIXTURE_ID);
    assert.equal(await page.$eval('#message', node => node.value), 'Another session draft');
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
      delete globalThis.__finishCommand;
      delete globalThis.__composerParityRestore;
    });
    await nextPaint(page);
  }
}

async function verifyAttachmentLayout(page) {
  const viewport = page.viewport();
  try {
    await page.evaluate(async () => {
      const files = new DataTransfer();
      for (let index = 0; index < 4; index += 1) {
        const canvas = document.createElement('canvas');
        canvas.width = index % 2 ? 1200 : 300;
        canvas.height = index % 2 ? 300 : 1200;
        const context = canvas.getContext('2d');
        context.fillStyle = index % 2 ? '#00aacc' : '#dd5577';
        context.fillRect(0, 0, canvas.width, canvas.height);
        const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
        files.items.add(new File([blob], `layout-${index}.png`, {type: 'image/png'}));
      }
      el.attach.files = files.files;
      el.attach.dispatchEvent(new Event('change', {bubbles: true}));
    });
    await page.waitForFunction(() => {
      const images = [...document.querySelectorAll('.attachment img')];
      return images.length === 4 && images.every(image => image.complete && image.naturalWidth);
    });
    for (const width of [1440, 390, 320]) {
      await page.setViewport({...viewport, width, height: 568});
      await nextPaint(page);
      const layout = await page.evaluate(() => {
        const strip = el.attachments.getBoundingClientRect();
        const shell = document.querySelector('.composer-shell').getBoundingClientRect();
        return {
          pageContained: document.documentElement.scrollWidth <= innerWidth,
          stripScrolls: el.attachments.scrollWidth > el.attachments.clientWidth,
          cards: [...document.querySelectorAll('.attachment')].map(card => {
            const bounds = card.getBoundingClientRect();
            const preview = card.querySelector('.attachment-preview').getBoundingClientRect();
            const remove = card.querySelector('[data-remove-attachment]').getBoundingClientRect();
            return {
              contained: preview.bottom <= bounds.bottom && remove.bottom <= bounds.bottom
                && remove.right <= bounds.right && bounds.bottom <= strip.bottom
                && bounds.bottom <= shell.top,
              touchTarget: remove.width >= 44 && remove.height >= 44,
              fit: getComputedStyle(card.querySelector('img')).objectFit,
            };
          }),
        };
      });
      assert(layout.pageContained, `attachments must not widen the page at ${width}px`);
      assert(layout.cards.every(card => card.contained && card.touchTarget && card.fit === 'contain'),
        `portrait and landscape previews and remove controls stay inside their cards at ${width}px`);
      if (width === 320) assert(layout.stripScrolls, 'four attachments scroll inside the phone composer');
    }
    await page.click('.attachment-preview');
    await page.waitForSelector('#image-viewer[open]');
    assert.deepEqual(await page.$eval('#image-viewer-image', image => ({
      width: image.naturalWidth, height: image.naturalHeight,
    })), {width: 300, height: 1200}, 'preview opens the original portrait image');
    await page.click('.image-viewer-close');
    await page.evaluate(() => {
      const remove = document.querySelector('[data-remove-attachment="3"]');
      remove.scrollIntoView({block: 'nearest', inline: 'nearest'});
      remove.focus();
    });
    assert(await page.$eval('[data-remove-attachment="3"]', remove => {
      const button = remove.getBoundingClientRect();
      const strip = el.attachments.getBoundingClientRect();
      return document.activeElement === remove && button.left >= strip.left && button.right <= strip.right;
    }), 'last remove control remains reachable by scrolling and keyboard focus');
    await page.keyboard.press('Enter');
    assert.deepEqual(await page.$$eval('.attachment img', images => images.map(image => image.alt)),
      ['layout-0.png', 'layout-1.png', 'layout-2.png'], 'remove affects only its selected attachment');
  } finally {
    await page.evaluate(() => {
      for (let index = 0; index < 4; index += 1) {
        document.querySelector('[data-remove-attachment]')?.click();
      }
    });
    await page.setViewport(viewport);
  }
}
