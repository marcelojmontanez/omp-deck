import assert from 'node:assert/strict';

export async function verifyMachineSelector(page) {
  const result = await page.evaluate(async () => {
    const originalMachines = ompDeckApi.machines;
    const originalMachine = ompDeckApi.machine;
    const originalMode = ompDeckApi.mode;
    const originalRequest = ompDeckApi.request;
    const originalSelection = state.selectedMachineId;
    const originalOptions = [...el['machine-select'].options].map(option => ({
      value: option.value,
      text: option.textContent,
    }));
    const requests = [];
    let machines = [
      {id: 'selector-a', name: 'Alpha', origin: 'https://alpha.invalid', status: 'live'},
      {id: 'selector-b', name: 'Beta', origin: 'https://beta.invalid', status: 'live'},
    ];
    const response = (ok, status, payload) => ({
      ok, status, json: async () => payload,
    });

    try {
      ompDeckApi.mode = () => 'coordinator';
      ompDeckApi.machines = () => machines.map(machine => ({...machine}));
      ompDeckApi.machine = id => {
        const machine = machines.find(item => item.id === id);
        return machine ? {...machine} : null;
      };
      ompDeckApi.request = async (path, options = {}) => {
        requests.push({path, machineId: options.machineId, method: options.method || 'GET'});
        if (path === '/api/projects') {
          return response(true, 200, {rootLabel: 'test root', projects: ['demo']});
        }
        if (path === '/api/usage') return response(true, 200, {reports: []});
        if (path.endsWith('/sessions') && options.method === 'POST') {
          return response(false, 400, {error: 'Expected selector regression rejection'});
        }
        return response(true, 200, {});
      };

      state.selectedMachineId = 'selector-b';
      renderMachineControls();
      const select = el['machine-select'];
      select.hidden = false;
      select.focus();
      const alpha = select.options[0];
      const beta = select.options[1];
      const statusList = el['machine-status-list'];
      const statusChild = statusList.firstElementChild;
      let optionChildListMutations = 0;
      let statusChildListMutations = 0;
      const optionObserver = new MutationObserver(records => {
        optionChildListMutations += records.filter(record => record.type === 'childList').length;
      });
      const statusObserver = new MutationObserver(records => {
        statusChildListMutations += records.filter(record => record.type === 'childList').length;
      });
      optionObserver.observe(select, {childList: true, subtree: true});
      statusObserver.observe(statusList, {childList: true, subtree: true});

      for (let index = 0; index < 20; index += 1) renderMachineControls();
      await Promise.resolve();
      const unchanged = {
        alphaStable: select.options[0] === alpha,
        betaStable: select.options[1] === beta,
        statusStable: statusList.firstElementChild === statusChild,
        selected: select.value,
        focused: document.activeElement === select,
        optionChildListMutations,
        statusChildListMutations,
      };

      machines = [
        {...machines[0], status: 'offline'},
        machines[1],
        {id: 'selector-c', name: 'Gamma', origin: 'https://gamma.invalid', status: 'connecting'},
      ];
      renderMachineControls();
      await Promise.resolve();
      const updated = {
        alphaStable: select.options[0] === alpha,
        betaStable: select.options[1] === beta,
        alphaLabel: alpha.textContent,
        ids: [...select.options].map(option => option.value),
        selected: select.value,
        focused: document.activeElement === select,
      };

      machines = [machines[0], machines[2]];
      renderMachineControls();
      await Promise.resolve();
      const removed = {
        ids: [...select.options].map(option => option.value),
        selected: select.value,
        focused: document.activeElement === select,
      };
      optionObserver.disconnect();
      statusObserver.disconnect();

      select.value = 'selector-c';
      select.dispatchEvent(new Event('change', {bubbles: true}));
      await new Promise(resolve => setTimeout(resolve, 0));
      el['project-select'].value = 'demo';
      el['project-select'].dispatchEvent(new Event('change', {bubbles: true}));
      el['project-launch'].click();
      await new Promise(resolve => setTimeout(resolve, 0));

      return {unchanged, updated, removed, requests};
    } finally {
      ompDeckApi.machines = originalMachines;
      ompDeckApi.machine = originalMachine;
      ompDeckApi.mode = originalMode;
      ompDeckApi.request = originalRequest;
      state.selectedMachineId = originalSelection;
      el['machine-select'].replaceChildren(...originalOptions.map(option =>
        new Option(option.text, option.value)
      ));
      renderMachineControls();
      await loadLaunchableProjects();
    }
  });

  assert.deepEqual(result.unchanged, {
    alphaStable: true,
    betaStable: true,
    statusStable: true,
    selected: 'selector-b',
    focused: true,
    optionChildListMutations: 0,
    statusChildListMutations: 0,
  });
  assert.deepEqual(result.updated, {
    alphaStable: true,
    betaStable: true,
    alphaLabel: 'Alpha — Offline',
    ids: ['selector-a', 'selector-b', 'selector-c'],
    selected: 'selector-b',
    focused: true,
  });
  assert.deepEqual(result.removed, {
    ids: ['selector-a', 'selector-c'],
    selected: 'selector-a',
    focused: true,
  });
  const targeted = result.requests.filter(call =>
    call.path === '/api/projects' || call.path === '/api/usage' || call.method === 'POST'
  );
  assert.ok(targeted.some(call => call.path === '/api/projects' && call.machineId === 'selector-c'));
  assert.ok(targeted.some(call => call.path === '/api/usage' && call.machineId === 'selector-c'));
  assert.ok(targeted.some(call => call.method === 'POST' && call.machineId === 'selector-c'));
}
