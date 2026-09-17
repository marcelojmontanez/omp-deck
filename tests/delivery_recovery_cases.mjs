import assert from 'node:assert/strict';

function receiptSelector(action, requestId) {
  return `#activity [data-delivery-action="${action}"][data-delivery-id="${requestId}"]`;
}

export async function verifyDeliveryRecovery(page, expectedSessionId) {
  const message = 'Accepted while the browser loses its response';
  const newerDraft = 'Keep this newer draft';
  const identity = await page.evaluate(() => {
    const agent = selectedAgent();
    return {
      id: agent.id,
      localId: agent.localId,
      machineId: agent.machineId,
      machineOrigin: agent.machineOrigin,
      split: ompDeckApi.splitSessionKey(agent.id),
    };
  });
  assert.equal(identity.id, expectedSessionId);
  assert.match(identity.id, /^fleet-/);
  assert.equal(identity.localId, 'same-local-session');
  assert.deepEqual(identity.split, {machineId: identity.machineId, localId: identity.localId});

  const routedRequests = [];
  const observeRequest = request => {
    const url = new URL(request.url());
    if (
      url.origin !== identity.machineOrigin
      || (!url.pathname.includes('/deliveries') && !url.pathname.endsWith('/messages'))
    ) return;
    routedRequests.push({
      method: request.method(),
      path: url.pathname,
      body: request.postData() || '',
    });
  };
  page.on('request', observeRequest);

  let requestId;
  let originalPayload;
  try {
    await page.evaluate(({origin, localId}) => {
      const originalFetch = window.fetch;
      const targetPath = `/api/sessions/${encodeURIComponent(localId)}/messages`;
      const fixture = {
        originalFetch,
        calls: [],
        responses: [],
        releaseRetry: null,
      };
      window.deliveryRecoveryFixture = fixture;
      window.fetch = async (input, options = {}) => {
        const url = new URL(typeof input === 'string' ? input : input.url, location.href);
        const method = (options.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
        if (url.origin !== origin || url.pathname !== targetPath || method !== 'POST') {
          return originalFetch(input, options);
        }
        fixture.calls.push(JSON.parse(options.body));
        const response = await originalFetch(input, options);
        fixture.responses.push(await response.clone().json());
        if (fixture.calls.length === 1) {
          throw new TypeError('Fixture discarded the accepted response');
        }
        if (fixture.calls.length === 2) {
          return new Promise(resolve => {
            fixture.releaseRetry = () => resolve(response);
          });
        }
        return response;
      };
    }, {origin: identity.machineOrigin, localId: identity.localId});

    await page.type('#message', message);
    await page.click('#send');
    await page.waitForFunction(expected => {
      const receipt = [...document.querySelectorAll('#activity .event')].find(event => (
        /uncertain/i.test(event.querySelector('.event-title')?.textContent || '')
        && event.textContent.includes(expected)
      ));
      return receipt && document.getElementById('message').value === expected
        && window.deliveryRecoveryFixture?.responses.length === 1;
    }, {}, message);

    const uncertain = await page.evaluate(() => ({
      calls: deliveryRecoveryFixture.calls,
      responses: deliveryRecoveryFixture.responses,
      draft: document.getElementById('message').value,
      title: document.querySelector('#activity .event.pending .event-title')?.textContent,
    }));
    assert.equal(uncertain.calls.length, 1, 'an uncertain outcome must not resend automatically');
    assert.equal(uncertain.draft, message);
    assert.match(uncertain.title, /uncertain/i);
    assert.deepEqual(uncertain.responses[0], {
      requestId: uncertain.calls[0].requestId,
      status: 'accepted',
      acceptance: 'accepted',
    });
    requestId = uncertain.calls[0].requestId;
    originalPayload = uncertain.calls[0];

    await page.click('#send');
    await page.waitForFunction(() => (
      window.deliveryRecoveryFixture?.calls.length === 2
      && typeof window.deliveryRecoveryFixture.releaseRetry === 'function'
    ));
    await page.type('#message', newerDraft);
    await page.evaluate(() => window.deliveryRecoveryFixture.releaseRetry());
    await page.waitForFunction((id, expected) => {
      const restore = document.querySelector(`[data-delivery-action="restore"][data-delivery-id="${id}"]`);
      return /accepted/i.test(restore?.closest('.event')?.querySelector('.event-title')?.textContent || '')
        && document.getElementById('message').value === expected;
    }, {}, requestId, newerDraft);
    await page.waitForFunction(expected => selectedAgent().session.activity.filter(event => (
      event.kind === 'user' && event.detail === expected
    )).length === 1, {}, message);

    const accepted = await page.evaluate(expected => ({
      calls: deliveryRecoveryFixture.calls,
      responses: deliveryRecoveryFixture.responses,
      draft: document.getElementById('message').value,
      nativeMessages: selectedAgent().session.activity.filter(event => (
        event.kind === 'user' && event.detail === expected
      )).length,
    }), message);
    assert.equal(accepted.calls.length, 2);
    assert.equal(accepted.calls[0].requestId, accepted.calls[1].requestId,
      'an explicit uncertain retry must retain its request identity');
    assert.equal(accepted.responses[1].requestId, requestId);
    assert.equal(accepted.draft, newerDraft, 'late acceptance must not clear a newer draft');
    assert.equal(accepted.nativeMessages, 1,
      'the native session must enqueue a stable requestId only once');

    await page.evaluate(() => {
      window.fetch = window.deliveryRecoveryFixture.originalFetch;
      delete window.deliveryRecoveryFixture;
    });
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.waitForFunction(id => (
      !document.getElementById('app').hidden && selectedAgent()?.id === id
    ), {}, expectedSessionId);
    await page.waitForSelector(receiptSelector('restore', requestId), {visible: true});

    const recovered = await page.$eval(
      receiptSelector('restore', requestId),
      button => ({
        title: button.closest('.event').querySelector('.event-title').textContent,
        text: button.closest('.event').querySelector('.event-detail').textContent,
      }),
    );
    assert.match(recovered.title, /accepted/i);
    assert.equal(recovered.text, message);
    const sendsAfterRecovery = routedRequests.filter(request => (
      request.method === 'POST' && request.path === `/api/sessions/${encodeURIComponent(identity.localId)}/messages`
    ));
    assert.equal(sendsAfterRecovery.length, 2,
      'authoritative receipt recovery after reload must not send automatically');


    await page.type('#message', newerDraft);
    await page.click(receiptSelector('restore', requestId));
    assert.equal(await page.$eval('#message', input => input.value), `${newerDraft}\n${message}`,
      'restoring a receipt must merge its text without replacing the current draft');
    const restoredDraft = await page.$eval('#message', input => input.value);
    await page.click(receiptSelector('dismiss', requestId));
    await page.waitForFunction(id => !document.querySelector(`[data-delivery-id="${id}"]`), {}, requestId);
    assert.equal(await page.$eval('#message', input => input.value), restoredDraft,
      'dismissing a receipt must not modify the draft');

    const replay = await page.evaluate(async ({sessionId, payload}) => {
      const response = await ompDeckApi.request(
        `/api/sessions/${encodeURIComponent(sessionId)}/messages`,
        {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        },
      );
      return {status: response.status, body: await response.json()};
    }, {sessionId: expectedSessionId, payload: originalPayload});
    assert.equal(replay.status, 202);
    assert.deepEqual(replay.body, {requestId, status: 'accepted', acceptance: 'accepted'});
    assert.equal(await page.evaluate(expected => selectedAgent().session.activity.filter(event => (
      event.kind === 'user' && event.detail === expected
    )).length, message), 1, 'dismissing a receipt must preserve native request deduplication');

    const localPrefix = `/api/sessions/${encodeURIComponent(identity.localId)}`;
    const messageRequests = routedRequests.filter(request => (
      request.method === 'POST' && request.path === `${localPrefix}/messages`
    ));
    assert.equal(messageRequests.length, 3,
      'only the initial send and two explicit identity-preserving retries should reach the node');
    assert(messageRequests.every(request => JSON.parse(request.body).requestId === requestId));
    assert(routedRequests.some(request => (
      request.method === 'GET' && request.path === `${localPrefix}/deliveries`
    )), 'reload recovery must route the composite fleet session to its local session ID');
    assert(routedRequests.some(request => (
      request.method === 'POST'
      && request.path === `${localPrefix}/deliveries/${encodeURIComponent(requestId)}/dismiss`
    )), 'receipt dismissal must route to the local session ID');

    await page.$eval('#message', input => {
      input.value = '';
      input.dispatchEvent(new Event('input', {bubbles: true}));
    });
  } finally {
    page.off('request', observeRequest);
    await page.evaluate(() => {
      const fixture = window.deliveryRecoveryFixture;
      if (!fixture) return;
      fixture.releaseRetry?.();
      window.fetch = fixture.originalFetch;
      delete window.deliveryRecoveryFixture;
    }).catch(() => {});
  }
}
