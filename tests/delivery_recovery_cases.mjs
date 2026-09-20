import assert from 'node:assert/strict';


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
    await page.waitForFunction(expected => (
      !document.querySelector('#activity .event.pending')
      && document.getElementById('message').value === expected
    ), {}, newerDraft);
    await page.waitForFunction(expected => selectedAgent().session.activity.filter(event => (
      event.kind === 'user' && event.detail === expected
    )).length === 1, {}, message);

    const accepted = await page.evaluate(expected => ({
      calls: deliveryRecoveryFixture.calls,
      responses: deliveryRecoveryFixture.responses,
      draft: document.getElementById('message').value,
      pending: composer.pendingFor(selectedAgent().id).length,
      acceptedCards: [...document.querySelectorAll('#activity .event-title')]
        .filter(title => /accepted/i.test(title.textContent)).length,
      nativeMessages: selectedAgent().session.activity.filter(event => (
        event.kind === 'user' && event.detail === expected
      )).length,
    }), message);
    assert.equal(accepted.calls.length, 2);
    assert.equal(accepted.calls[0].requestId, accepted.calls[1].requestId,
      'an explicit uncertain retry must retain its request identity');
    assert.equal(accepted.responses[1].requestId, requestId);
    assert.equal(accepted.draft, newerDraft, 'late acceptance must not clear a newer draft');
    assert.equal(accepted.pending, 0, 'accepted sends must leave no local receipt payload');
    assert.equal(accepted.acceptedCards, 0, 'accepted sends must not render receipt cards');
    assert.equal(accepted.nativeMessages, 1,
      'the native session must enqueue a stable requestId only once');

    await page.evaluate(() => {
      window.fetch = window.deliveryRecoveryFixture.originalFetch;
      delete window.deliveryRecoveryFixture;
    });
    const recoveryRead = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.origin === identity.machineOrigin
        && url.pathname === `/api/sessions/${encodeURIComponent(identity.localId)}/deliveries`
        && response.request().method() === 'GET';
    });
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.waitForFunction(id => (
      !document.getElementById('app').hidden && selectedAgent()?.id === id
    ), {}, expectedSessionId);
    const recoveredSnapshot = await (await recoveryRead).json();
    assert(recoveredSnapshot.deliveries.some(record => (
      record.requestId === requestId && record.state === 'accepted'
    )), 'reload must recover the authoritative accepted request from its owning node');

    const recoveredAccepted = await page.evaluate(id => ({
      card: Boolean(document.querySelector(`[data-delivery-id="${id}"]`)),
      pending: composer.pendingFor(selectedAgent().id).length,
      status: document.getElementById('send-state').textContent,
    }), requestId);
    assert.deepEqual(recoveredAccepted, {card: false, pending: 0, status: ''},
      'recovered acceptance must not appear as fresh composer feedback');
    const sendsAfterRecovery = routedRequests.filter(request => (
      request.method === 'POST' && request.path === `/api/sessions/${encodeURIComponent(identity.localId)}/messages`
    ));
    assert.equal(sendsAfterRecovery.length, 2,
      'authoritative accepted recovery must not send automatically');

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
    )).length, message), 1, 'hidden accepted recovery must preserve native request deduplication');

    const acceptedAttachmentRecovery = await page.evaluate(
      async ({origin, localId, recoveredRequestId, recoveredMessage}) => {
        const agent = selectedAgent();
        const originalFetch = window.fetch;
        const originalCreateObjectURL = URL.createObjectURL;
        const targetPath = `/api/sessions/${encodeURIComponent(localId)}/deliveries`;
        let createdObjectUrls = 0;
        URL.createObjectURL = blob => {
          createdObjectUrls += 1;
          return originalCreateObjectURL.call(URL, blob);
        };
        window.fetch = async (input, options = {}) => {
          const url = new URL(typeof input === 'string' ? input : input.url, location.href);
          const method = (options.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
          if (url.origin === origin && url.pathname === targetPath && method === 'GET') {
            const now = Date.now();
            return new Response(JSON.stringify({version: 1, deliveries: [{
              requestId: recoveredRequestId,
              sessionId: localId,
              action: 'sendMessage',
              message: recoveredMessage,
              state: 'accepted',
              attachments: [{name: 'accepted.png', type: 'image/png', data: 'data:image/png;base64,aQ=='}],
              createdAt: now - 10,
              updatedAt: now,
              expiresAt: now + 60_000,
            }]}), {status: 200, headers: {'Content-Type': 'application/json'}});
          }
          return originalFetch(input, options);
        };
        try {
          composer.remove(agent.id);
          composer.select(agent.id);
          await composer.recover(agent);
          return {
            createdObjectUrls,
            pending: composer.pendingFor(agent.id).length,
            card: Boolean(document.querySelector(`[data-delivery-id="${recoveredRequestId}"]`)),
          };
        } finally {
          window.fetch = originalFetch;
          URL.createObjectURL = originalCreateObjectURL;
          composer.remove(agent.id);
          composer.select(agent.id);
        }
      },
      {
        origin: identity.machineOrigin,
        localId: identity.localId,
        recoveredRequestId: `${requestId}-accepted-image`,
        recoveredMessage: message,
      },
    );
    assert.deepEqual(acceptedAttachmentRecovery, {
      createdObjectUrls: 0,
      pending: 0,
      card: false,
    });

    const acceptedFollowUpRecovery = await page.evaluate(
      async ({origin, localId, dismissRequestId, pruneRequestId}) => {
        const agent = selectedAgent();
        const originalFetch = window.fetch;
        const originalNow = Date.now;
        const targetPath = `/api/sessions/${encodeURIComponent(localId)}/deliveries`;
        const dismissedPath = `${targetPath}/${encodeURIComponent(dismissRequestId)}/dismiss`;
        let now = originalNow();
        let returnRecords = true;
        Date.now = () => now;
        window.fetch = async (input, options = {}) => {
          const url = new URL(typeof input === 'string' ? input : input.url, location.href);
          const method = (options.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
          if (url.origin === origin && url.pathname === targetPath && method === 'GET') {
            const deliveries = returnRecords ? [dismissRequestId, pruneRequestId].map(requestId => ({
              requestId, sessionId: localId, action: 'followUp', message: `Queued ${requestId}`,
              state: 'accepted',
              attachments: [{
                name: `${requestId}.png`, type: 'image/png',
                data: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM1sAAAAASUVORK5CYII=',
              }],
              createdAt: now - 10, updatedAt: now, expiresAt: now + 60_000,
            })) : [];
            return new Response(JSON.stringify({version: 1, deliveries}), {
              status: 200, headers: {'Content-Type': 'application/json'},
            });
          }
          if (url.origin === origin && url.pathname === dismissedPath && method === 'POST') {
            return new Response(JSON.stringify({requestId: dismissRequestId, status: 'dismissed'}),
              {status: 200, headers: {'Content-Type': 'application/json'}});
          }
          return originalFetch(input, options);
        };
        const readable = async url => {
          const image = new Image();
          image.src = url;
          try {
            await image.decode();
            return image.naturalWidth === 1;
          } catch {
            return false;
          } finally {
            image.removeAttribute('src');
          }
        };
        try {
          composer.remove(agent.id);
          composer.select(agent.id);
          await composer.recover(agent);
          const images = [dismissRequestId, pruneRequestId].map(id => el.activity.querySelector(
            `[data-delivery-action="dismiss"][data-delivery-id="${CSS.escape(id)}"]`,
          ).closest('.event').querySelector('img'));
          await Promise.all(images.map(image => image.decode()));
          const decodedWidths = images.map(image => image.naturalWidth);
          const urls = images.map(image => image.src);
          await composer.dismissDelivery(dismissRequestId);
          const afterDismiss = {
            dismissedGone: !images[0].isConnected,
            dismissedReleased: !await readable(urls[0]),
            remainingDecoded: images[1].isConnected && images[1].naturalWidth === 1,
            remainingReadable: await readable(urls[1]),
          };
          returnRecords = false;
          now += 2_001;
          await composer.recover(agent);
          return {
            decodedWidths, afterDismiss,
            prunedGone: !images[1].isConnected,
            prunedReleased: !await readable(urls[1]),
          };
        } finally {
          window.fetch = originalFetch;
          Date.now = originalNow;
          composer.remove(agent.id);
          composer.select(agent.id);
        }
      },
      {
        origin: identity.machineOrigin,
        localId: identity.localId,
        dismissRequestId: `${requestId}-accepted-follow-up-dismiss`,
        pruneRequestId: `${requestId}-accepted-follow-up-prune`,
      },
    );
    assert.deepEqual(acceptedFollowUpRecovery, {
      decodedWidths: [1, 1],
      afterDismiss: {
        dismissedGone: true, dismissedReleased: true,
        remainingDecoded: true, remainingReadable: true,
      },
      prunedGone: true,
      prunedReleased: true,
    }, 'Recovered queue images stay previewable until acknowledged dismissal or authoritative pruning releases them');

    const recoveredUncertain = await page.evaluate(
      async ({origin, localId, recoveredRequestId}) => {
        const agent = selectedAgent();
        const originalFetch = window.fetch;
        const targetPath = `/api/sessions/${encodeURIComponent(localId)}/deliveries`;
        let messagePosts = 0;
        window.fetch = async (input, options = {}) => {
          const url = new URL(typeof input === 'string' ? input : input.url, location.href);
          const method = (options.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
          if (url.origin === origin && url.pathname === targetPath && method === 'GET') {
            const now = Date.now();
            return new Response(JSON.stringify({version: 1, deliveries: [{
              requestId: recoveredRequestId,
              sessionId: localId,
              action: 'sendMessage',
              message: 'Still uncertain after reload',
              state: 'uncertain',
              attachments: [{name: 'uncertain.png', type: 'image/png', data: 'data:image/png;base64,aQ=='}],
              createdAt: now - 10,
              updatedAt: now,
              expiresAt: now + 60_000,
            }]}), {status: 200, headers: {'Content-Type': 'application/json'}});
          }
          if (url.origin === origin && url.pathname.endsWith('/messages') && method === 'POST') {
            messagePosts += 1;
          }
          return originalFetch(input, options);
        };
        try {
          composer.remove(agent.id);
          composer.select(agent.id);
          await composer.recover(agent);
          const card = document.querySelector(`[data-delivery-id="${recoveredRequestId}"]`)?.closest('.event');
          return {
            messagePosts,
            pending: composer.pendingFor(agent.id).length,
            title: card?.querySelector('.event-title')?.textContent,
            text: card?.querySelector('.event-detail')?.textContent,
            restore: Boolean(card?.querySelector('[data-delivery-action="restore"]')),
            dismiss: Boolean(card?.querySelector('[data-delivery-action="dismiss"]')),
            status: document.getElementById('send-state').textContent,
          };
        } finally {
          window.fetch = originalFetch;
          composer.remove(agent.id);
          composer.select(agent.id);
        }
      },
      {
        origin: identity.machineOrigin,
        localId: identity.localId,
        recoveredRequestId: `${requestId}-uncertain`,
      },
    );
    assert.deepEqual(recoveredUncertain, {
      messagePosts: 0,
      pending: 1,
      title: 'You · Delivery uncertain',
      text: 'Still uncertain after reload',
      restore: true,
      dismiss: true,
      status: 'Delivery uncertain',
    });

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
    assert.equal(routedRequests.some(request => request.path.includes('/dismiss')), false,
      'ordinary accepted send recovery must not issue a dismissal request');
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
