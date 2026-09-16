import assert from 'node:assert/strict';

export async function verifyConnectionRecovery(page) {
  await page.focus('#message');
  await page.type('#message', 'Keep this draft and selection during recovery');
  await page.$eval('#message', input => input.setSelectionRange(5, 15));
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
