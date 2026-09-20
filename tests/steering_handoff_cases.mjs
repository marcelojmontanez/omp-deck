import assert from 'node:assert/strict';

const IDS = Object.freeze({
  primary: 'steering-handoff-primary',
  background: 'steering-handoff-background',
  duplicates: 'steering-handoff-duplicates',
  early: 'steering-handoff-early-echo',
  queue: 'steering-handoff-queue',
  image: 'steering-handoff-image',
  uncertain: 'steering-handoff-uncertain',
});

const PRIMARY_TEXT = 'Steering handoff continuity';
const DUPLICATE_TEXT = 'Repeated identical steering';
const EARLY_TEXT = 'Native echo precedes acknowledgement';
const QUEUED_TEXT = 'Queued confirmation stays explicit';
const UNCERTAIN_TEXT = 'A visible echo cannot prove acceptance';

async function installFixture(page) {
  await page.waitForFunction(() => !el.app.hidden && el.toast.hidden);
  await page.evaluate(({ids, primaryText}) => {
    const savedState = Object.fromEntries(Object.keys(state).map(key => [key, state[key]]));
    const savedUrl = location.href;
    const savedHistory = history.state;
    const originalRequest = ompDeckApi.request;
    const originalNow = Date.now;
    const fixtureIds = Object.values(ids);
    const response = (payload, status = 200) => new Response(JSON.stringify(payload), {
      status,
      headers: {'Content-Type': 'application/json'},
    });
    const makeAgent = (id, activity = [], working = true) => ({
      id,
      localId: id,
      machineId: 'local',
      machineName: 'Fixture',
      project: 'demo',
      label: id,
      lifecycle: 'ongoing',
      nodeAvailable: true,
      bridgeConnected: true,
      status: working ? 'working' : 'idle',
      executionState: working ? 'working' : 'idle',
      inboxState: 'read',
      capabilities: {sendMessage: true, followUp: true, attachments: true},
      attachmentLimits: {maxImages: 4, maxTotalBytes: 20 * 1024 * 1024},
      transport: {kind: 'extension', status: 'connected', label: 'Terminal'},
      pendingUiRequest: null,
      subagents: [],
      activitySummary: working ? 'Finishing the current response' : null,
      session: {
        model: 'fixture/model',
        thinking: 'high',
        todos: [],
        activity,
        archiveActivity: [],
        updatedMs: 1_800_000_000_000,
      },
    });
    const historical = {
      id: 'historical-identical-user',
      kind: 'user',
      title: 'You',
      detail: primaryText,
      attachments: [],
      time: '2024-01-01T00:00:00.000Z',
    };
    const agents = [
      makeAgent(ids.primary, [historical]),
      makeAgent(ids.background),
      makeAgent(ids.duplicates),
      makeAgent(ids.early),
      makeAgent(ids.queue, [], true),
      makeAgent(ids.image),
      makeAgent(ids.uncertain),
    ];
    const fixture = {
      savedState,
      savedUrl,
      savedHistory,
      savedChatOpen: [document.body, el.app, el.workspace].map(node => node.classList.contains('chat-open')),
      originalRequest,
      originalNow,
      ids: fixtureIds,
      agents,
      requests: [],
      now: 1_800_000_000_000,
    };
    Date.now = () => fixture.now;
    fixture.advance = () => { fixture.now += 1; };
    fixture.findAgent = id => fixture.agents.find(agent => agent.id === id);
    fixture.select = id => selectAgent(id, {pushHistory: false});
    fixture.addEvent = (id, event) => {
      const agent = fixture.findAgent(id);
      if (event.attachments === undefined) event.attachments = [];
      agent.session.activity.push(event);
      agent.session.updatedMs += 1;
      composer.reconcile(agent);
      touchRender();
      if (state.selectedId === id) renderDetail();
    };
    fixture.replay = id => {
      const agent = fixture.findAgent(id);
      agent.session.activity = agent.session.activity.map(event => ({
        ...event,
        ...(Array.isArray(event.attachments)
          ? {attachments: event.attachments.map(attachment => ({...attachment}))}
          : {}),
      }));
      agent.session.updatedMs += 1;
      composer.reconcile(agent);
      touchRender();
      if (state.selectedId === id) renderDetail();
    };
    fixture.settle = (index, accepted) => {
      const request = fixture.requests[index];
      if (!request || request.settled) throw new Error(`No held fixture request at ${index}`);
      request.settled = true;
      if (accepted) {
        request.resolve(response({
          requestId: request.body.requestId,
          status: 'accepted',
          acceptance: 'accepted',
        }, 202));
      } else {
        request.reject(new TypeError('Fixture lost the native response after dispatch'));
      }
    };
    window.steeringHandoffFixture = fixture;
    ompDeckApi.request = async (path, options = {}) => {
      const method = (options.method || 'GET').toUpperCase();
      if (path.endsWith('/deliveries')) return response({version: 1, deliveries: []});
      if (path.endsWith('/draft')) return response({
        enabled: false,
        revision: null,
        text: '',
        images: [],
        updatedAt: null,
        expiresAt: null,
      });
      if (method === 'POST' && path.endsWith('/dismiss')) return response({
        requestId: decodeURIComponent(path.split('/').at(-2)),
        status: 'dismissed',
      });
      const action = path.endsWith('/follow-ups') ? 'followUp'
        : path.endsWith('/messages') ? 'sendMessage' : null;
      if (method === 'POST' && action) {
        const body = JSON.parse(options.body);
        return await new Promise((resolve, reject) => {
          fixture.requests.push({path, action, body, resolve, reject, settled: false});
        });
      }
      throw new Error(`Unexpected steering handoff fixture request: ${method} ${path}`);
    };
    fixtureIds.forEach(id => composer.remove(id));
    state.agents = agents;
    state.selectedId = null;
    state.showArchive = false;
    state.showTodo = false;
    fixture.select(ids.primary);
  }, {ids: IDS, primaryText: PRIMARY_TEXT});
}

async function restoreFixture(page) {
  await page.evaluate(async () => {
    const fixture = window.steeringHandoffFixture;
    if (!fixture) return;
    fixture.ids.forEach(id => {
      composer.remove(id);
      ompDeckImages.releaseOwner(id);
    });
    for (const request of fixture.requests) {
      if (!request.settled) {
        request.settled = true;
        request.resolve(new Response(JSON.stringify({error: 'Fixture finished'}), {
          status: 503,
          headers: {'Content-Type': 'application/json'},
        }));
      }
    }
    await new Promise(resolve => setTimeout(resolve, 0));
    ompDeckApi.request = fixture.originalRequest;
    Date.now = fixture.originalNow;
    Object.assign(state, fixture.savedState);
    history.replaceState(fixture.savedHistory, '', fixture.savedUrl);
    [document.body, el.app, el.workspace].forEach((node, index) => {
      node.classList.toggle('chat-open', fixture.savedChatOpen[index]);
    });
    composer.select(state.selectedId);
    delete window.steeringHandoffFixture;
    el.toast.hidden = true;
    touchRender();
    renderSummary();
    renderList();
    renderDetail();
  });
}

async function selectFixture(page, id) {
  await page.evaluate(sessionId => steeringHandoffFixture.select(sessionId), id);
  await page.waitForFunction(sessionId => selectedAgent()?.id === sessionId, {}, id);
}

async function submitText(page, text, expectedIndex) {
  await page.evaluate(value => {
    el.message.value = value;
    el.message.dispatchEvent(new Event('input', {bubbles: true}));
  }, text);
  await page.locator('#send').click();
  await page.waitForFunction(index => steeringHandoffFixture.requests.length > index, {}, expectedIndex);
  const request = await page.evaluate(index => {
    const {path, action, body} = steeringHandoffFixture.requests[index];
    return {path, action, body};
  }, expectedIndex);
  assert.equal(request.body.message, text);
  return request;
}

async function settle(page, index, accepted = true) {
  await page.evaluate(({requestIndex, wasAccepted}) => {
    steeringHandoffFixture.settle(requestIndex, wasAccepted);
  }, {requestIndex: index, wasAccepted: accepted});
}

async function addEvent(page, id, event) {
  await page.evaluate(({sessionId, nativeEvent}) => {
    steeringHandoffFixture.addEvent(sessionId, nativeEvent);
  }, {sessionId: id, nativeEvent: event});
}

async function replay(page, id) {
  await page.evaluate(sessionId => {
    steeringHandoffFixture.replay(sessionId);
    steeringHandoffFixture.replay(sessionId);
  }, id);
}

async function snapshot(page) {
  return page.evaluate(() => ({
    events: [...el.activity.querySelectorAll('.event')].map(event => ({
      title: event.querySelector('.event-title')?.textContent.trim() || '',
      detail: event.querySelector('.event-detail')?.textContent.trim() || '',
      pending: event.classList.contains('pending'),
      queued: event.classList.contains('queued'),
      images: event.querySelectorAll('.message-attachments img').length,
      restore: Boolean(event.querySelector('[data-delivery-action="restore"]')),
      dismiss: Boolean(event.querySelector('[data-delivery-action="dismiss"]')),
    })),
    feedback: el['send-state'].textContent.trim(),
  }));
}

function deliveries(view, title, detail) {
  return view.events.filter(event => event.title === title
    && (detail === undefined || event.detail === detail));
}

export async function verifySteeringHandoff(page, {capture = async () => {}} = {}) {
  try {
    await installFixture(page);

    const first = await submitText(page, PRIMARY_TEXT, 0);
    assert(first.path.endsWith(`/${encodeURIComponent(IDS.primary)}/messages`));
    await settle(page, 0);
    await page.waitForFunction(text => [...el.activity.querySelectorAll('.event')].some(event => (
      event.querySelector('.event-title')?.textContent.trim() === 'You · Accepted'
      && event.querySelector('.event-detail')?.textContent.trim() === text
    )), {}, PRIMARY_TEXT);
    let view = await snapshot(page);
    const acceptedWaiting = deliveries(view, 'You · Accepted', PRIMARY_TEXT);
    assert.equal(acceptedWaiting.length, 1,
      'HTTP acceptance must leave an honest visible handoff until a new native user event appears');
    assert.deepEqual(acceptedWaiting.map(event => ({pending: event.pending, restore: event.restore, dismiss: event.dismiss})),
      [{pending: false, restore: false, dismiss: false}],
      'accepted steering is no longer a grey pending bubble or an invitation to resend');
    assert.equal(deliveries(view, 'You', PRIMARY_TEXT).length, 1,
      'the older identical native message remains visible without satisfying this send');
    await page.evaluate(async id => {
      steeringHandoffFixture.now += 2_001;
      await composer.recover(steeringHandoffFixture.findAgent(id));
    }, IDS.primary);
    view = await snapshot(page);
    assert.equal(deliveries(view, 'You · Accepted', PRIMARY_TEXT).length, 1,
      'an authoritative recovery read cannot reopen the accepted-before-echo visibility gap');
    await capture('accepted-waiting');

    await selectFixture(page, IDS.background);
    await addEvent(page, IDS.background, {
      id: 'background-identical-user', kind: 'user', title: 'You', detail: PRIMARY_TEXT, time: null,
    });
    await selectFixture(page, IDS.primary);
    assert.equal(deliveries(await snapshot(page), 'You · Accepted', PRIMARY_TEXT).length, 1,
      'an identical event in another session cannot consume the source session handoff');
    await addEvent(page, IDS.primary, {
      id: 'same-text-assistant', kind: 'assistant', title: 'OMP', detail: PRIMARY_TEXT, time: null,
    });
    await addEvent(page, IDS.primary, {
      id: 'unrelated-new-user', kind: 'user', title: 'You', detail: 'Unrelated native activity', time: null,
    });
    assert.equal(deliveries(await snapshot(page), 'You · Accepted', PRIMARY_TEXT).length, 1,
      'assistant and unrelated user updates cannot consume an accepted handoff');
    await addEvent(page, IDS.primary, {
      id: 'new-corresponding-user', kind: 'user', title: 'You', detail: PRIMARY_TEXT, time: null,
    });
    await replay(page, IDS.primary);
    view = await snapshot(page);
    assert.equal(deliveries(view, 'You · Accepted', PRIMARY_TEXT).length, 0);
    assert.equal(deliveries(view, 'You', PRIMARY_TEXT).length, 2,
      'the new native event replaces only the local representation and render replay stays idempotent');
    await capture('native-reconciled');

    await selectFixture(page, IDS.duplicates);
    await page.evaluate(() => steeringHandoffFixture.advance());
    const firstDuplicate = await submitText(page, DUPLICATE_TEXT, 1);
    await settle(page, 1);
    await page.waitForFunction(text => [...el.activity.querySelectorAll('.event')].filter(event => (
      event.querySelector('.event-title')?.textContent.trim() === 'You · Accepted'
      && event.querySelector('.event-detail')?.textContent.trim() === text
    )).length === 1, {}, DUPLICATE_TEXT);
    await page.evaluate(() => steeringHandoffFixture.advance());
    const secondDuplicate = await submitText(page, DUPLICATE_TEXT, 2);
    assert.notEqual(firstDuplicate.body.requestId, secondDuplicate.body.requestId,
      'identical sends retain distinct delivery identities');
    await settle(page, 2);
    await page.waitForFunction(text => [...el.activity.querySelectorAll('.event')].filter(event => (
      event.querySelector('.event-title')?.textContent.trim() === 'You · Accepted'
      && event.querySelector('.event-detail')?.textContent.trim() === text
    )).length === 2, {}, DUPLICATE_TEXT);
    await addEvent(page, IDS.duplicates, {
      id: 'duplicate-echo-one', kind: 'user', title: 'You', detail: DUPLICATE_TEXT, time: null,
    });
    await replay(page, IDS.duplicates);
    view = await snapshot(page);
    assert.equal(deliveries(view, 'You · Accepted', DUPLICATE_TEXT).length, 1,
      'one native event can consume only one of two identical accepted sends');
    assert.equal(deliveries(view, 'You', DUPLICATE_TEXT).length, 1);
    await addEvent(page, IDS.duplicates, {
      id: 'duplicate-echo-two', kind: 'user', title: 'You', detail: DUPLICATE_TEXT, time: null,
    });
    await replay(page, IDS.duplicates);
    view = await snapshot(page);
    assert.equal(deliveries(view, 'You · Accepted', DUPLICATE_TEXT).length, 0);
    assert.equal(deliveries(view, 'You', DUPLICATE_TEXT).length, 2);

    await selectFixture(page, IDS.early);
    await page.evaluate(() => steeringHandoffFixture.advance());
    await submitText(page, EARLY_TEXT, 3);
    await addEvent(page, IDS.early, {
      id: 'early-native-echo', kind: 'user', title: 'You', detail: EARLY_TEXT, time: null,
    });
    view = await snapshot(page);
    assert.equal(view.events.filter(event => event.detail === EARLY_TEXT).length, 2,
      'a native echo cannot erase the local preview before independent HTTP acceptance');
    assert.equal(deliveries(view, 'You · Accepted', EARLY_TEXT).length, 0);
    await settle(page, 3);
    await page.waitForFunction(text => [...el.activity.querySelectorAll('.event-detail')]
      .filter(node => node.textContent.trim() === text).length === 1, {}, EARLY_TEXT);
    view = await snapshot(page);
    assert.equal(deliveries(view, 'You', EARLY_TEXT).length, 1);
    assert.equal(view.events.filter(event => event.detail === EARLY_TEXT).length, 1,
      'acknowledgement coalesces an already-visible corresponding native echo without a gap');

    await selectFixture(page, IDS.queue);
    await page.locator('[data-delivery-mode="queue"]').click();
    assert.equal(await page.$eval('[data-delivery-mode="queue"]', button => button.getAttribute('aria-pressed')), 'true');
    await page.evaluate(() => steeringHandoffFixture.advance());
    const queued = await submitText(page, QUEUED_TEXT, 4);
    assert.equal(queued.action, 'followUp');
    await settle(page, 4);
    await page.waitForFunction(text => el.activity.textContent.includes(text)
      && el.activity.textContent.includes('Queue accepted'), {}, QUEUED_TEXT);
    await addEvent(page, IDS.queue, {
      id: 'queued-native-echo', kind: 'user', title: 'You', detail: QUEUED_TEXT, time: null,
    });
    view = await snapshot(page);
    const queueReceipt = deliveries(view, 'You · Queue accepted', QUEUED_TEXT);
    assert.equal(queueReceipt.length, 1,
      'queued follow-up acceptance remains a distinct pinned confirmation after matching native activity');
    assert.deepEqual(queueReceipt.map(event => ({queued: event.queued, pending: event.pending, dismiss: event.dismiss})),
      [{queued: true, pending: false, dismiss: true}]);
    assert.equal(view.events[0].title, 'You · Queue accepted',
      'the queued confirmation stays pinned above native conversation activity');
    assert.equal(deliveries(view, 'You', QUEUED_TEXT).length, 1);

    await selectFixture(page, IDS.image);
    await page.evaluate(async () => {
      const bytes = Uint8Array.from(atob(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jBuoAAAAASUVORK5CYII='
      ), value => value.charCodeAt(0));
      const transfer = new DataTransfer();
      transfer.items.add(new File([bytes], 'handoff.png', {type: 'image/png'}));
      const event = new Event('paste', {bubbles: true, cancelable: true});
      Object.defineProperty(event, 'clipboardData', {value: transfer});
      el.message.dispatchEvent(event);
    });
    await page.waitForSelector('#attachments img');
    await page.evaluate(() => steeringHandoffFixture.advance());
    await page.locator('#send').click();
    await page.waitForFunction(() => steeringHandoffFixture.requests.length === 6);
    await settle(page, 5);
    await page.waitForFunction(() => [...el.activity.querySelectorAll('.event')].some(event => (
      event.querySelector('.event-title')?.textContent.trim() === 'You · Accepted'
      && event.querySelectorAll('.message-attachments img').length === 1
    )));
    await addEvent(page, IDS.image, {
      id: 'processed-image-native', kind: 'user', title: 'You', detail: '',
      attachments: [{url: null, unavailable: true, machineId: 'local'}], time: null,
    });
    view = await snapshot(page);
    const imageReceipt = deliveries(view, 'You · Accepted');
    assert.equal(imageReceipt.length, 1,
      'accepted image content stays as an honest receipt when native content identity is unprovable');
    assert.equal(imageReceipt[0].images, 1);
    assert.deepEqual({
      restore: imageReceipt[0].restore,
      dismiss: imageReceipt[0].dismiss,
      pending: imageReceipt[0].pending,
    }, {restore: false, dismiss: true, pending: false});
    assert.equal(deliveries(view, 'You').length, 1);
    const imageUrl = await page.$eval('#activity .message-attachments img', async image => {
      await image.decode();
      return image.currentSrc;
    });
    await page.locator('[data-delivery-action="dismiss"]').click();
    await page.waitForFunction(() => !el.activity.querySelector('[data-delivery-action="dismiss"]'));
    assert.equal(deliveries(await snapshot(page), 'You').length, 1,
      'dismissing an unmatchable image receipt preserves the native conversation');
    assert.equal(await page.evaluate(async url => {
      const image = new Image();
      try {
        image.src = url;
        await image.decode();
        return true;
      } catch {
        return false;
      } finally {
        image.src = '';
      }
    }, imageUrl), false, 'dismissal releases the retained local image rather than leaking its blob');

    await selectFixture(page, IDS.uncertain);
    await page.evaluate(() => steeringHandoffFixture.advance());
    await submitText(page, UNCERTAIN_TEXT, 6);
    await addEvent(page, IDS.uncertain, {
      id: 'uncertain-native-echo', kind: 'user', title: 'You', detail: UNCERTAIN_TEXT, time: null,
    });
    await settle(page, 6, false);
    await page.waitForFunction(text => [...el.activity.querySelectorAll('.event')].some(event => (
      /delivery uncertain/i.test(event.querySelector('.event-title')?.textContent || '')
      && event.querySelector('.event-detail')?.textContent.trim() === text
    )), {}, UNCERTAIN_TEXT);
    view = await snapshot(page);
    assert.equal(deliveries(view, 'You · Accepted', UNCERTAIN_TEXT).length, 0,
      'matching text cannot manufacture acceptance after a lost response');
    const uncertain = deliveries(view, 'You · Delivery uncertain', UNCERTAIN_TEXT);
    assert.deepEqual(uncertain.map(event => ({restore: event.restore, dismiss: event.dismiss, pending: event.pending})),
      [{restore: true, dismiss: true, pending: true}]);
    assert.equal(deliveries(view, 'You', UNCERTAIN_TEXT).length, 1);
    assert.equal(view.events.filter(event => event.detail === UNCERTAIN_TEXT).length, 2,
      'native visibility and uncertain delivery remain explicit instead of inferring completion');
  } finally {
    await restoreFixture(page);
  }
}
