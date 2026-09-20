import assert from 'node:assert/strict';

const SOURCE = 'queue-visibility-source';
const OTHER = 'queue-visibility-other';
const FIRST = 'First queued follow-up';
const SECOND = 'Newest queued follow-up';
const OLDER = 'Older recovered queue';

async function installConversation(page) {
  // Earlier scenarios deliberately raise timed error alerts over the composer.
  await page.waitForFunction(() => !el.app.hidden && nodeState('local').hasSnapshot && el.toast.hidden);
  await page.evaluate(({source, other}) => {
    const savedState = Object.fromEntries(Object.keys(state).map(key => [key, state[key]]));
    const makeAgent = id => ({
      id, localId: id, machineId: 'local', machineName: 'Fixture', project: 'demo', label: id,
      lifecycle: 'ongoing', nodeAvailable: true, bridgeConnected: true,
      status: 'working', executionState: 'working', inboxState: 'read',
      capabilities: {sendMessage: true, followUp: true},
      transport: {kind: 'extension', status: 'connected', label: 'Terminal'},
      pendingUiRequest: null, subagents: [], activitySummary: 'Finishing the current response',
      session: {
        model: 'fixture/model', thinking: 'high', todos: [], archiveActivity: [], updatedMs: Date.now(),
        activity: [{id: 'native-response', kind: 'assistant', title: 'OMP',
          detail: 'The current native conversation stays below queued deliveries.', time: null}],
      },
    });
    window.queueVisibilityFixture = {savedState};
    state.agents = [makeAgent(source), makeAgent(other)];
    state.selectedId = null;
    state.showArchive = false;
    state.showTodo = false;
    selectAgent(source, {pushHistory: false});
  }, {source: SOURCE, other: OTHER});
}

async function restoreConversation(page) {
  await page.evaluate(({source, other}) => {
    const fixture = window.queueVisibilityFixture;
    if (!fixture) return;
    composer.remove(source);
    composer.remove(other);
    Object.assign(state, fixture.savedState);
    delete window.queueVisibilityFixture;
    touchRender();
    renderSummary();
    renderList();
    renderDetail({selectionChanged: true});
  }, {source: SOURCE, other: OTHER});
}

async function chatSnapshot(page) {
  return page.evaluate(() => {
    const events = [...el.activity.querySelectorAll('.event')];
    return {
      first: events[0]?.textContent || '',
      titles: events.map(event => event.querySelector('.event-title')?.textContent || ''),
      messages: events.map(event => event.querySelector('.event-detail')?.textContent || ''),
      workingIndex: events.findIndex(event => event.classList.contains('thinking')),
      restores: el.activity.querySelectorAll('[data-delivery-action="restore"]').length,
      dismisses: el.activity.querySelectorAll('[data-delivery-action="dismiss"]').length,
    };
  });
}

export async function verifyQueueVisibility(page, {capture = async () => {}} = {}) {
  const receipts = new Map();
  const submissions = [];
  const errors = [];
  const held = new Set();
  const createdAt = Date.now();
  let pendingSubmission;
  let pendingDismissal;
  let initialRecovery = true;
  let pendingRecovery;
  const respond = (request, payload, status = 200) => {
    held.delete(request);
    return request.respond({status, contentType: 'application/json', body: JSON.stringify(payload)});
  };
  const handleRequest = async request => {
    const pathname = new URL(request.url()).pathname;
    const prefix = `/api/sessions/${SOURCE}/`;
    const otherPrefix = `/api/sessions/${OTHER}/`;
    if (!pathname.startsWith(prefix) && !pathname.startsWith(otherPrefix)) return request.continue();
    if (pathname.endsWith('/draft')) return respond(request, {
      enabled: false, revision: null, text: '', images: [], updatedAt: null, expiresAt: null,
    });
    if (pathname.endsWith('/deliveries')) {
      if (pathname.startsWith(prefix) && initialRecovery) {
        initialRecovery = false;
        pendingRecovery = request;
        held.add(request);
        return;
      }
      return respond(request, {version: 1, deliveries: pathname.startsWith(prefix) ? [...receipts.values()] : []});
    }
    if (pathname.endsWith('/follow-ups') && request.method() === 'POST') {
      const body = JSON.parse(request.postData());
      submissions.push(body);
      const time = createdAt + submissions.length;
      receipts.set(body.requestId, {
        requestId: body.requestId, sessionId: SOURCE, action: 'followUp', state: 'submitting',
        message: body.message, attachments: [], createdAt: time, updatedAt: time,
        expiresAt: time + 7 * 86_400_000,
      });
      pendingSubmission = request;
      held.add(request);
      return;
    }
    if (pathname.endsWith('/dismiss') && request.method() === 'POST') {
      pendingDismissal = request;
      held.add(request);
      return;
    }
    errors.push(`${request.method()} ${pathname}`);
    return respond(request, {error: 'Unexpected queue fixture action'}, 404);
  };
  const observeRequest = request => {
    void handleRequest(request).catch(error => {
      errors.push(error.message);
      if (!request.isInterceptResolutionHandled()) void request.abort().catch(() => {});
    });
  };
  const acceptSubmission = async () => {
    const request = pendingSubmission;
    pendingSubmission = null;
    const {requestId} = JSON.parse(request.postData());
    receipts.get(requestId).state = 'accepted';
    await respond(request, {requestId, status: 'accepted', acceptance: 'accepted'}, 202);
    await page.waitForFunction(message => [...el.activity.querySelectorAll('.event')].some(event => (
      event.textContent.includes(message) && /queue accepted/i.test(event.querySelector('.event-title')?.textContent)
    )), {}, receipts.get(requestId).message);
    return requestId;
  };
  const submit = async text => {
    await page.locator('[data-delivery-mode="queue"]').click();
    assert.equal(await page.$eval('[data-delivery-mode="queue"]', button => button.getAttribute('aria-pressed')),
      'true', 'Queue must be selected before composing the follow-up');
    await page.type('#message', text);
    await page.click('#send');
    await page.waitForFunction(expected => el.activity.textContent.includes(expected), {}, text);
    for (let attempt = 0; !pendingSubmission && attempt < 100; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    assert(pendingSubmission, `Queue submission must reach its advertised native action: ${JSON.stringify({
      errors, chat: await chatSnapshot(page),
      feedback: await page.$eval('#send-state', node => node.textContent),
    })}`);
  };
  const reload = async () => {
    await restoreConversation(page);
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.waitForFunction(() => !el.app.hidden);
    await installConversation(page);
    await page.waitForFunction(expected => el.activity.textContent.includes(expected), {}, FIRST);
  };

  await page.setRequestInterception(true);
  page.on('request', observeRequest);
  try {
    await installConversation(page);
    await submit(FIRST);
    const submitting = await chatSnapshot(page);
    await capture('submitting');
    assert(submitting.first.includes(FIRST), 'Queued messages must lead the chat above Working while submitting');
    assert.equal(submitting.workingIndex, 1);
    assert(!/accepted/i.test(submitting.titles[0]), 'Dispatch alone must not claim queue acceptance');
    await acceptSubmission();
    assert(pendingRecovery, 'The old recovery snapshot must overlap queue acknowledgement');
    const oldResponse = page.waitForResponse(response => response.request() === pendingRecovery);
    await respond(pendingRecovery, {version: 1, deliveries: []});
    await oldResponse;
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const accepted = await chatSnapshot(page);
    assert(accepted.first.includes(FIRST), 'An older empty recovery response must not erase acknowledged queue acceptance');
    assert.equal(accepted.restores, 0, 'Accepted queue confirmations must not offer a duplicate resend');
    assert.equal(accepted.dismisses, 1);

    await submit(SECOND);
    const secondId = await acceptSubmission();
    const ordered = await chatSnapshot(page);
    assert.deepEqual(ordered.messages.slice(0, 2), [SECOND, FIRST]);
    assert.equal(ordered.workingIndex, 2);
    await capture('accepted');

    await page.evaluate(other => selectAgent(other, {pushHistory: false}), OTHER);
    assert(!(await chatSnapshot(page)).messages.includes(FIRST), 'Queue confirmations must not leak into another session');
    await page.evaluate(source => selectAgent(source, {pushHistory: false}), SOURCE);
    assert.deepEqual((await chatSnapshot(page)).messages.slice(0, 2), [SECOND, FIRST]);

    await page.evaluate(() => {
      const agent = selectedAgent();
      agent.status = 'idle';
      agent.executionState = 'idle';
      agent.session.activity.push({id: 'later-native-message', kind: 'user', title: 'You',
        detail: 'First queued follow-up', time: null});
      touchRender();
      renderDetail();
    });
    const idle = await chatSnapshot(page);
    assert.deepEqual(idle.messages.slice(0, 2), [SECOND, FIRST],
      'Native activity and matching text cannot establish per-message completion or erase acceptance');
    assert.equal(idle.workingIndex, -1);
    assert(idle.titles.slice(0, 2).every(title => /queue accepted/i.test(title)));

    const retained = receipts.values().next().value;
    receipts.set('ordinary-accepted', {...retained, requestId: 'ordinary-accepted', action: 'sendMessage', message: 'Ordinary accepted send'});
    receipts.set('older-queued', {...retained, requestId: 'older-queued', message: OLDER,
      createdAt: createdAt - 1_000, updatedAt: createdAt - 1_000});
    await reload();
    const recovered = await chatSnapshot(page);
    assert.deepEqual(recovered.messages.slice(0, 3), [SECOND, FIRST, OLDER],
      'Reloaded queue confirmations must be newest-first even when older records arrive later');
    assert.equal(recovered.workingIndex, 3);
    assert(!recovered.messages.includes('Ordinary accepted send'));
    assert.equal(submissions.length, 2, 'Recovery must never resend queued messages');
    await capture('recovered');

    await page.click(`[data-delivery-action="dismiss"][data-delivery-id="${secondId}"]`);
    for (let attempt = 0; !pendingDismissal && attempt < 100; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    assert(pendingDismissal);
    assert((await chatSnapshot(page)).messages.includes(SECOND), 'Dismissal must await its authoritative acknowledgement');
    receipts.delete(secondId);
    await respond(pendingDismissal, {requestId: secondId, status: 'dismissed'});
    await page.waitForFunction(text => !el.activity.textContent.includes(text), {}, SECOND);
    await reload();
    assert.deepEqual((await chatSnapshot(page)).messages.slice(0, 2), [FIRST, OLDER]);
    assert.equal(submissions.length, 2, 'Dismissal hides a confirmation without sending or cancelling native work');
    assert.deepEqual(errors, []);
  } finally {
    await Promise.allSettled([...held].map(request => respond(request, {error: 'Fixture finished'}, 503)));
    await restoreConversation(page);
    page.off('request', observeRequest);
    await page.setRequestInterception(false);
  }
}
