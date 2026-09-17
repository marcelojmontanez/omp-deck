import assert from 'node:assert/strict';

export async function verifyConnectionRecovery(page) {
  await page.focus('#message');
  await page.type('#message', 'Keep this draft and selection during recovery');
  await page.$eval('#message', input => input.setSelectionRange(5, 15));
  await page.setOfflineMode(true);
  await page.evaluate(async () => {
    const id = selectedAgent().machineId;
    stopNodeEventStream(id);
    await refresh(id, {replace: true});
    ompDeckApi.setMachineStatus(id, 'unavailable', 'Connection temporarily unavailable');
  });
  let unavailableTap;
  try {
    await page.tap('#send');
    unavailableTap = await page.evaluate(() => ({
      focused: document.activeElement === el.message,
      draft: el.message.value,
      selection: [el.message.selectionStart, el.message.selectionEnd],
    }));
  } finally {
    await page.setOfflineMode(false);
    await page.evaluate(async () => {
      const id = selectedAgent().machineId;
      await refresh(id);
      startNodeEventStream(id);
    });
  }
  assert.deepEqual(unavailableTap, {
    focused: true,
    draft: 'Keep this draft and selection during recovery',
    selection: [5, 15],
  }, 'Tapping temporarily unavailable Send must not dismiss the composer or its selection');
  const renewal = await page.evaluate(async () => {
    const originalFetch = window.fetch;
    const id = selectedAgent().machineId;
    const origin = ompDeckApi.machine(id).origin;
    let intercepted = false;
    let expirePreviousStream;
    let opened;
    let openedTimer;
    const previousOpened = new Promise(resolve => { opened = resolve; });
    window.fetch = async (input, options) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href);
      const intercept = !intercepted && url.origin === origin && url.pathname === '/api/events';
      if (intercept) intercepted = true;
      const response = await originalFetch(input, options);
      if (!intercept) return response;
      const reader = response.body.getReader();
      let closed = false;
      const body = new ReadableStream({
        start(controller) {
          expirePreviousStream = () => {
            if (closed) return;
            closed = true;
            controller.close();
            void reader.cancel().catch(() => {});
          };
          void (async () => {
            try {
              while (!closed) {
                const {value, done} = await reader.read();
                if (closed) return;
                if (done) { closed = true; controller.close(); return; }
                controller.enqueue(value);
              }
            } catch (error) {
              if (!closed) { closed = true; controller.error(error); }
            } finally {
              reader.releaseLock();
            }
          })();
          opened();
        },
        cancel() { closed = true; return reader.cancel(); },
      });
      return new Response(body, {status: response.status, headers: response.headers});
    };
    try {
      stopNodeEventStream(id);
      startNodeEventStream(id);
      await Promise.race([
        previousOpened,
        new Promise((_, reject) => {
          openedTimer = setTimeout(() => reject(new Error('Event stream did not open')), 10_000);
        }),
      ]);
      // Signed tickets use whole seconds; the next issue must be a new credential.
      await new Promise(resolve => setTimeout(resolve, 1100));
      await ompDeckApi.renewAccess();
      expirePreviousStream();
      await new Promise(resolve => setTimeout(resolve, 100));
      return {
        available: ompDeckApi.machine(id).status === 'live',
        canSend: !el.send.disabled,
        focused: document.activeElement === el.message,
        draft: el.message.value,
        selection: [el.message.selectionStart, el.message.selectionEnd],
      };
    } finally {
      clearTimeout(openedTimer);
      window.fetch = originalFetch;
      stopNodeEventStream(id);
      await refresh(id);
      startNodeEventStream(id);
    }
  });
  assert.deepEqual(renewal, {
    available: true,
    canSend: true,
    focused: true,
    draft: 'Keep this draft and selection during recovery',
    selection: [5, 15],
  }, 'Expiry of a replaced ticket must not interrupt a healthy composer');
  for (const stage of ['access-headers', 'access-body', 'sessions-body', 'projects-body', 'stream-headers']) {
    const outcome = await page.evaluate(async stage => {
      const originalFetch = window.fetch;
      const originalTimeout = window.setTimeout;
      const machineId = selectedAgent().machineId;
      const target = stage.startsWith('access') ? '/api/fleet/access'
        : stage.startsWith('sessions') ? '/api/sessions'
          : stage.startsWith('projects') ? '/api/projects' : '/api/events';
      let abortHeld = () => {};
      let removeAbortListener = () => {};
      let removeStatusListener = () => {};
      let outerTimer;
      let operation;
      window.setTimeout = (callback, delay, ...args) => originalTimeout(
        callback, delay === ompDeckNetwork.STARTUP_TIMEOUT_MS ? 40 : delay, ...args,
      );
      window.fetch = (input, options = {}) => {
        const pathname = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
        if (pathname !== target) return originalFetch(input, options);
        const reason = () => options.signal?.reason || new DOMException('Fixture released', 'AbortError');
        const attachAbort = callback => {
          abortHeld = callback;
          options.signal?.addEventListener('abort', callback, {once: true});
          removeAbortListener = () => options.signal?.removeEventListener('abort', callback);
          if (options.signal?.aborted) callback();
        };
        if (stage.endsWith('headers')) {
          return new Promise((_, reject) => attachAbort(() => reject(reason())));
        }
        return Promise.resolve(new Response(new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode('{"unfinished":'));
            attachAbort(() => controller.error(reason()));
          },
        }), {headers: {'Content-Type': 'application/json'}}));
      };
      try {
        if (target === '/api/fleet/access') operation = ompDeckApi.renewAccess();
        else if (target === '/api/sessions') operation = refresh(machineId);
        else if (target === '/api/projects') operation = loadLaunchableProjects();
        else {
          stopNodeEventStream(machineId);
          operation = new Promise(resolve => {
            const changed = event => {
              if (event.detail?.machine?.id === machineId && event.detail.machine.status === 'offline') resolve();
            };
            window.addEventListener('omp-deck-node-status', changed);
            removeStatusListener = () => window.removeEventListener('omp-deck-node-status', changed);
          });
          startNodeEventStream(machineId);
        }
        const settled = await Promise.race([
          operation.then(() => true, () => true),
          new Promise(resolve => { outerTimer = originalTimeout(() => resolve(false), 1000); }),
        ]);
        return {
          settled,
          focused: document.activeElement === el.message,
          draft: el.message.value,
          selection: [el.message.selectionStart, el.message.selectionEnd],
          projectPickerReleased: stage !== 'projects-body' || !el['project-select'].disabled,
          authenticated: ompDeckApi.authenticated(),
        };
      } finally {
        clearTimeout(outerTimer);
        abortHeld();
        await operation?.catch(() => {});
        removeAbortListener();
        removeStatusListener();
        window.fetch = originalFetch;
        window.setTimeout = originalTimeout;
        if (target === '/api/events') stopNodeEventStream(machineId);
        await ompDeckApi.renewAccess();
        await refresh(machineId);
        await loadLaunchableProjects();
        startNodeEventStream(machineId);
      }
    }, stage);
    assert.deepEqual(outcome, {
      settled: true,
      focused: true,
      draft: 'Keep this draft and selection during recovery',
      selection: [5, 15],
      projectPickerReleased: true,
      authenticated: true,
    }, `Stalled ${stage} must settle without stealing focus or losing the draft`);
    await page.waitForFunction(() => !document.getElementById('send').disabled);
  }
  await page.evaluate(async () => {
    const id = selectedAgent().machineId;
    el.message.value = '';
    el.message.dispatchEvent(new Event('input', {bubbles: true}));
    // A healthy stream must outlive its connection deadline.
    const originalTimeout = window.setTimeout;
    window.setTimeout = (callback, delay, ...args) => originalTimeout(
      callback, delay === ompDeckNetwork.STARTUP_TIMEOUT_MS ? 40 : delay, ...args,
    );
    try {
      stopNodeEventStream(id);
      startNodeEventStream(id);
      await new Promise(resolve => originalTimeout(resolve, 100));
      if (ompDeckApi.machine(id).status !== 'live') throw new Error('Healthy event stream was timed out');
    } finally {
      window.setTimeout = originalTimeout;
    }
  });
}
