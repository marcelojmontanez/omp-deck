import assert from 'node:assert/strict';

export async function verifyForegroundRecovery(page) {
  await page.bringToFront();
  await page.waitForFunction(() => ompDeckApi.machines().every(machine => machine.status === 'live')
    && [...state.nodes.values()].every(node => !node.refreshRequest));
  await page.focus('#message');
  await page.type('#message', 'Keep this draft across suspension');
  await page.$eval('#message', input => input.setSelectionRange(5, 15));

  const superseded = await page.evaluate(async () => {
    const originalFetch = window.fetch;
    let rejectHeld;
    let timer;
    let first = true;
    window.fetch = (input, options) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href);
      if (first && url.pathname === '/api/fleet/access') {
        first = false;
        return new Promise((_, reject) => { rejectHeld = reject; });
      }
      return originalFetch(input, options);
    };
    const prior = ompDeckApi.renewAccess().catch(() => {});
    const recovery = ompDeckApi.recoverAccess();
    try {
      const freshAccess = await Promise.race([
        recovery.then(() => true, () => false),
        new Promise(resolve => { timer = setTimeout(() => resolve(false), 2000); }),
      ]);
      rejectHeld?.(new TypeError('Suspended request failed late'));
      await prior;
      return {freshAccess, authenticated: ompDeckApi.authenticated(), draft: el.message.value};
    } finally {
      clearTimeout(timer);
      window.fetch = originalFetch;
      rejectHeld?.(new TypeError('Fixture released'));
      await Promise.allSettled([prior, recovery]);
      await recoverLiveConnections();
    }
  });
  assert.deepEqual(superseded, {
    freshAccess: true,
    authenticated: true,
    draft: 'Keep this draft across suspension',
  }, 'Foreground access must replace a renewal queued in the same turn, not join its stuck request');

  for (const trigger of ['focus', 'visibilitychange']) {
    const resumed = await page.evaluate(async trigger => {
      const originalFetch = window.fetch;
      const originalTimeout = window.setTimeout;
      const selectedId = state.selectedId;
      let rejectHeld;
      let timer;
      let held = false;
      let changed;
      if (trigger === 'visibilitychange') {
        window.setTimeout = (callback, delay, ...args) => originalTimeout(
          callback, delay === ompDeckNetwork.STARTUP_TIMEOUT_MS ? 200 : delay, ...args,
        );
        window.fetch = (input, options) => {
          const url = new URL(typeof input === 'string' ? input : input.url, location.href);
          if (!held && url.pathname === '/api/fleet/access') {
            held = true;
            // A suspended native operation may not settle when its signal is aborted.
            return new Promise((_, reject) => { rejectHeld = reject; });
          }
          return originalFetch(input, options);
        };
      }
      stopEventStreams();
      for (const machine of ompDeckApi.machines()) {
        ompDeckApi.setMachineStatus(machine.id, 'unavailable', 'Suspended connection');
      }
      const live = new Promise(resolve => {
        changed = () => {
          if (ompDeckApi.machines().every(machine => machine.status === 'live')) resolve(true);
        };
        window.addEventListener('omp-deck-node-status', changed);
      });
      try {
        (trigger === 'focus' ? window : document).dispatchEvent(new Event(trigger));
        const recovered = await Promise.race([
          live,
          new Promise(resolve => { timer = originalTimeout(() => resolve(false), 2000); }),
        ]);
        return {
          recovered,
          selected: state.selectedId === selectedId,
          draft: el.message.value,
          selection: [el.message.selectionStart, el.message.selectionEnd],
          authenticated: ompDeckApi.authenticated(),
        };
      } finally {
        clearTimeout(timer);
        window.removeEventListener('omp-deck-node-status', changed);
        window.fetch = originalFetch;
        window.setTimeout = originalTimeout;
        rejectHeld?.(new TypeError('Suspended request failed late'));
        await recoverLiveConnections();
      }
    }, trigger);
    assert.deepEqual(resumed, {
      recovered: true,
      selected: true,
      draft: 'Keep this draft across suspension',
      selection: [5, 15],
      authenticated: true,
    }, `${trigger} must recover real machine streams without reloading or losing the draft`);
    await page.waitForFunction(() => !document.getElementById('send').disabled);
  }
  await page.evaluate(() => {
    el.message.value = '';
    el.message.dispatchEvent(new Event('input', {bubbles: true}));
  });
}
