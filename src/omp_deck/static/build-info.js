(() => {
  'use strict';

  const disclosure = document.getElementById('deck-builds');
  const list = document.getElementById('deck-build-list');
  const status = document.getElementById('deck-build-status');
  let active = null;

  function clear() {
    active?.abort();
    active = null;
    list.replaceChildren();
    status.textContent = '';
  }

  async function refresh() {
    clear();
    if (!disclosure.open || !ompDeckApi.authenticated()) return;
    const controller = new AbortController();
    active = controller;
    status.textContent = 'Checking installed versions…';
    const targets = [{name: ompDeckApi.mode() === 'coordinator' ? 'Dashboard' : 'Deck', id: null}];
    if (ompDeckApi.mode() === 'coordinator') targets.push(...ompDeckApi.machines());
    const results = await Promise.all(targets.map(async target => {
      const label = document.createElement('dt');
      const value = document.createElement('dd');
      label.textContent = target.name;
      value.textContent = 'Checking…';
      list.append(label, value);
      try {
        const request = target.id === null ? fetch : ompDeckApi.request;
        const options = {cache: 'no-store', signal: controller.signal};
        if (target.id === null) options.credentials = 'same-origin';
        else options.machineId = target.id;
        const {response, payload} = await ompDeckNetwork.fetchJsonBounded(
          '/api/client-config', options, ompDeckNetwork.STARTUP_TIMEOUT_MS, request
        );
        if (!response.ok) throw new Error('Version request failed');
        const build = ompDeckApi.parseBuild(payload?.build);
        if (active !== controller) return null;
        if (!build) {
          value.textContent = 'This installation does not report its build.';
          return null;
        }
        value.textContent = `${build.version} · ${build.revision}`;
        return build.revision === 'unavailable' ? null : `${build.version}\0${build.revision}`;
      } catch {
        if (active === controller) value.textContent = 'Version information unavailable.';
        return null;
      }
    }));
    if (active !== controller) return;
    const distinct = new Set(results.filter(Boolean));
    status.textContent = distinct.size > 1
      ? 'Different Deck builds are installed. This does not by itself mean they are incompatible.'
      : results.includes(null)
        ? 'Some builds could not be identified.'
        : targets.length > 1 ? 'All installations report the same Deck build.' : 'Installed Deck build.';
  }

  disclosure.addEventListener('toggle', () => {
    if (disclosure.open) void refresh();
    else clear();
  });
  document.getElementById('deck-build-refresh').addEventListener('click', () => void refresh());
  document.getElementById('deck-settings').addEventListener('close', () => {
    disclosure.open = false;
    clear();
  });
  window.addEventListener('omp-deck-auth-required', clear);
  window.addEventListener('omp-deck-machines-changed', event => {
    if (event.detail.removedIds?.length || event.detail.addedIds?.length) void refresh();
  });
})();
