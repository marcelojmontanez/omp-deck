import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {mkdtemp, mkdir, rm, stat, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {spawn, spawnSync} from 'node:child_process';
import net from 'node:net';
import test from 'node:test';
import puppeteer from 'puppeteer';
import {verifyComposerParity} from './composer_parity_cases.mjs';
import {verifySessionActions} from './session_action_cases.mjs';
import {verifyComposerDeliveryModes} from './composer_delivery_mode_cases.mjs';
import {verifyMachineSelector} from './machine_selector_cases.mjs';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const PYTHON = process.env.PYTHON || (
  existsSync(path.join(ROOT, '.venv', 'bin', 'python'))
    ? path.join(ROOT, '.venv', 'bin', 'python')
    : 'python3'
);

async function unusedPort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => server.listen(0, '127.0.0.1', resolve).once('error', reject));
  const {port} = server.address();
  await new Promise(resolve => server.close(resolve));
  return port;
}

async function waitForHealth(url, stderr) {
  let lastError;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetch(`${url}/healthz`);
      if (response.ok) return;
      lastError = new Error(`health returned ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(
    `${lastError?.message || 'service did not become healthy'}`
    + (stderr() ? `; service stderr: ${stderr()}` : '')
  );
}

function pairingCode(environment) {
  const result = spawnSync(PYTHON, ['-m', 'omp_deck', 'pair'], {
    cwd: ROOT,
    env: environment,
    encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr);
  const match = /^Pairing code: (.+)$/m.exec(result.stdout);
  assert(match, `pairing code missing from: ${result.stdout}`);
  return match[1];
}

test('pairs a real Chromium client and refreshes authenticated project data', async t => {
  const sandbox = await mkdtemp(path.join(tmpdir(), 'omp-deck-browser-'));
  const projects = path.join(sandbox, 'projects');
  const state = path.join(sandbox, 'state');
  const config = path.join(sandbox, 'config.json');
  const port = await unusedPort();
  for (const directory of ['home', 'profile']) {
    await mkdir(path.join(sandbox, directory), {recursive: true, mode: 0o700});
  }
  await mkdir(path.join(projects, 'demo'), {recursive: true});
  await writeFile(path.join(projects, 'demo', 'pyproject.toml'), '[project]\nname="demo"\nversion="0"\n');
  await mkdir(path.join(projects, 'group', 'nested'), {recursive: true});
  await writeFile(
    path.join(projects, 'group', 'nested', 'pyproject.toml'),
    '[project]\nname="nested"\nversion="0"\n',
  );
  await writeFile(config, JSON.stringify({
    host: '127.0.0.1',
    port,
    project_root: projects,
    project_discovery_depth: 2,
    session_root: path.join(sandbox, 'sessions'),
    socket_root: path.join(state, 'sockets'),
    state_root: state,
    omp_bin: process.execPath,
    show_history: true,
  }), {mode: 0o600});
  const environment = {
    PATH: process.env.PATH,
    ...(process.env.SystemRoot ? {SystemRoot: process.env.SystemRoot} : {}),
    HOME: path.join(sandbox, 'home'),
    OMP_CONFIG_DIR: path.join(sandbox, 'profile'),
    PYTHONPATH: path.join(ROOT, 'src'),
    OMP_DECK_CONFIG: config,
    XDG_CONFIG_HOME: path.join(sandbox, 'xdg-config'),
    XDG_DATA_HOME: path.join(sandbox, 'xdg-data'),
    XDG_STATE_HOME: path.join(sandbox, 'xdg-state'),
  };
  const service = spawn(PYTHON, ['-m', 'omp_deck', 'serve'], {
    cwd: ROOT,
    env: environment,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let serviceError = '';
  service.stderr.on('data', chunk => { serviceError += chunk; });
  t.after(async () => {
    if (service.exitCode === null) {
      await new Promise(resolve => {
        service.once('exit', resolve);
        service.kill('SIGTERM');
      });
    }
    await rm(sandbox, {recursive: true, force: true});
  });
  const url = `http://localhost:${port}`;
  await waitForHealth(url, () => serviceError);
  const token = pairingCode(environment);
  const profile = path.join(sandbox, 'chromium-profile');
  const pageErrors = [];
  const recordPageError = error => pageErrors.push(error.message);
  let browser = await puppeteer.launch({
    headless: true,
    args: ['--no-sandbox'],
    userDataDir: profile,
  });
  t.after(async () => {
    if (browser) await browser.close();
  });
  let page = await browser.newPage();
  page.on('pageerror', recordPageError);
  await page.setViewport({width: 390, height: 844, deviceScaleFactor: 1});
  await page.setRequestInterception(true);
  let failInitialSession = true;
  let heldClientConfig = null;
  let sessionStartedBeforeModeCompleted = false;
  const failSessionRequest = request => {
    const pathname = new URL(request.url()).pathname;
    if (pathname === '/api/client-config' && !heldClientConfig) {
      heldClientConfig = request;
      return;
    }
    if (failInitialSession && pathname === '/api/auth/session') {
      failInitialSession = false;
      sessionStartedBeforeModeCompleted = Boolean(heldClientConfig);
      void request.abort('failed');
      void heldClientConfig?.continue();
      heldClientConfig = null;
      return;
    }
    void request.continue();
  };
  page.on('request', failSessionRequest);
  await page.goto(url, {waitUntil: 'domcontentloaded'});
  await page.waitForSelector('#auth-retry:not([hidden])', {visible: true});
  const offlineBootstrap = await page.evaluate(() => ({
    retryVisible: !document.getElementById('auth-retry').hidden,
    pairingVisible: !document.getElementById('pairing-fields').hidden,
    appVisible: !document.getElementById('app').hidden,
  }));
  assert.deepEqual(offlineBootstrap, {
    retryVisible: true,
    pairingVisible: false,
    appVisible: false,
  });
  assert.equal(sessionStartedBeforeModeCompleted, true);
  page.off('request', failSessionRequest);
  await page.setRequestInterception(false);
  await page.click('#auth-retry');
  await page.waitForSelector('#pairing-fields:not([hidden]) #pair-token', {visible: true});
  await page.type('#pair-token', token);
  await page.click('#pair-form button[type="submit"]');
  await page.waitForFunction(() => (
    !document.getElementById('app').hidden
    && document.getElementById('project-select').options.length > 2
  ));
  const result = await page.evaluate(async () => {
    const projectsResponse = await fetch('/api/projects', {cache: 'no-store'});
    const projectsPayload = await projectsResponse.json();
    const cookiesEnabled = navigator.cookieEnabled;
    return {
      projectsStatus: projectsResponse.status,
      discoveryDepth: projectsPayload.discoveryDepth,
      projectValues: [...document.getElementById('project-select').options].map(option => option.value),
      projectGroups: [...document.querySelectorAll('#project-select optgroup')].map(group => group.label),
      pickerDisabled: document.getElementById('project-select').disabled,
      localStorageLength: localStorage.length,
      sessionStorageLength: sessionStorage.length,
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      cookiesEnabled,
      machineName: ompDeckApi.machine('local')?.name,
      browserHost: location.host,
      showHistory: ompDeckApi.showHistory(),
      build: ompDeckApi.build(),
      buildFrozen: Object.isFrozen(ompDeckApi.build()),
      absentBuild: ompDeckApi.parseBuild(undefined),
      invalidBuildRejected: (() => {
        try {
          ompDeckApi.parseBuild({version: '1.0.0'});
          return false;
        } catch {
          return true;
        }
      })(),
    };
  });
  assert.equal(result.projectsStatus, 200);
  assert(result.projectValues.includes('demo'));
  assert(result.projectValues.includes('group/nested'));
  assert(result.projectGroups.includes('group/'));
  assert.equal(result.discoveryDepth, 2);
  assert.equal(result.pickerDisabled, false);
  assert.equal(result.localStorageLength, 0);
  assert.equal(result.sessionStorageLength, 0);
  assert(result.overflow <= 0);
  assert.equal(result.cookiesEnabled, true);
  assert.equal(result.machineName, result.browserHost);
  assert.equal(result.showHistory, true);
  assert.equal(typeof result.build?.version, 'string');
  assert.equal(typeof result.build?.revision, 'string');
  assert.equal(result.buildFrozen, true);
  assert.equal(result.absentBuild, null);
  assert.equal(result.invalidBuildRejected, true);

  // Use the real creation route; this fixture deliberately cannot launch OMP.
  // Creation must survive that failure and remain selectable below discovery depth.
  for (const [parent, name] of [['', 'created root'], ['group/nested', 'created child']]) {
    await page.select('#project-select', `/new/${parent}`);
    await page.waitForSelector('#project-new', {visible: true});
    await page.$eval('#project-new', input => { input.focus(); input.select(); });
    await page.keyboard.press('Backspace');
    assert.equal(await page.$eval('#project-launch', button => button.disabled), true);
    await page.type('#project-new', name);
    const [createdResponse] = await Promise.all([
      page.waitForResponse(response => response.request().method() === 'POST'
        && new URL(response.url()).pathname.startsWith('/api/projects/')),
      page.click('#project-launch'),
    ]);
    assert.equal(createdResponse.status(), 502);
    assert.equal((await createdResponse.json()).created, true);
    const project = parent ? `${parent}/${name}` : name;
    assert.equal((await stat(path.join(projects, project))).isDirectory(), true);
    if (parent) assert.equal(existsSync(path.join(projects, name)), false);
    await page.waitForFunction(project => (
      document.getElementById('project-select').value === project
      && !document.getElementById('project-launch').disabled
      && document.getElementById('project-new').hidden
    ), {}, project);
    if (parent) {
      const [retryResponse] = await Promise.all([
        page.waitForResponse(response => response.request().method() === 'POST'
          && new URL(response.url()).pathname.startsWith('/api/projects/')),
        page.click('#project-launch'),
      ]);
      assert.equal(retryResponse.status(), 502);
      assert.equal((await retryResponse.json()).created, false);
      await page.waitForFunction(() => !document.getElementById('project-launch').disabled);
    }
  }
  await page.select('#project-select', '/new/group');
  await page.$eval('#project-new', input => { input.focus(); input.select(); });
  await page.keyboard.press('Backspace');
  await page.type('#project-new', '../escape');
  const [escapeResponse] = await Promise.all([
    page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname.startsWith('/api/projects/')),
    page.click('#project-launch'),
  ]);
  assert.equal(escapeResponse.status(), 400);
  assert.equal(existsSync(path.join(projects, 'escape')), false);
  await page.waitForFunction(() => !document.getElementById('project-launch').disabled);
  await page.select('#project-select', 'demo');
  const cookies = await page.cookies();
  const device = cookies.find(cookie => cookie.name === 'omp_deck_device');
  assert(device, `secure device cookie missing; service stderr: ${serviceError}`);
  await page.setRequestInterception(true);
  let failPairedSession = true;
  const failPairedSessionRequest = request => {
    if (failPairedSession && new URL(request.url()).pathname === '/api/auth/session') {
      failPairedSession = false;
      void request.abort('failed');
      return;
    }
    void request.continue();
  };
  page.on('request', failPairedSessionRequest);
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.waitForSelector('#auth-retry:not([hidden])', {visible: true});
  const pairedOffline = await page.evaluate(() => ({
    retryVisible: !document.getElementById('auth-retry').hidden,
    pinVisible: !document.getElementById('auth-screen').hidden
      && !document.getElementById('pairing-fields').hidden,
  }));
  assert.deepEqual(pairedOffline, {retryVisible: true, pinVisible: false});
  page.off('request', failPairedSessionRequest);
  await page.setRequestInterception(false);
  await page.click('#auth-retry');
  await page.waitForSelector('#app:not([hidden])', {visible: true});
  await page.waitForFunction(() => document.getElementById('project-select').options.length > 2);
  assert((await page.cookies()).some(cookie => cookie.name === 'omp_deck_device'));

  await page.setRequestInterception(true);
  const bootstrapPaths = new Set(['/api/client-config', '/api/auth/session']);
  const heldBootstrapRequests = [];
  const pendingBootstrap = Promise.all([...bootstrapPaths].map(pathname =>
    page.waitForRequest(request => new URL(request.url()).pathname === pathname)
  ));
  const holdBootstrap = request => {
    const pathname = new URL(request.url()).pathname;
    if (bootstrapPaths.delete(pathname)) heldBootstrapRequests.push(request);
    else void request.continue();
  };
  page.on('request', holdBootstrap);
  try {
    await page.reload({waitUntil: 'domcontentloaded'});
    await pendingBootstrap;
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await page.waitForSelector('#app:not([hidden])', {visible: true});
    await page.waitForFunction(() => document.getElementById('project-select').options.length > 2);
  } finally {
    page.off('request', holdBootstrap);
    await Promise.allSettled(heldBootstrapRequests.map(request => request.continue()));
    await page.setRequestInterception(false);
  }

  await verifyComposerParity(page);
  await verifyComposerDeliveryModes(page);
  await verifyMachineSelector(page);

  const forbiddenRecovery = await page.evaluate(async () => {
    const originalFetch = window.fetch;
    let calls = 0;
    document.getElementById('message').value = 'draft survives forbidden response';
    window.fetch = async (input, options) => {
      const pathname = new URL(
        typeof input === 'string' ? input : input.url,
        location.href,
      ).pathname;
      if (pathname === '/api/projects/demo/sessions' && options?.method === 'POST') {
        calls += 1;
        return new Response(JSON.stringify({error: 'forbidden'}), {
          status: 403,
          headers: {'Content-Type': 'application/json'},
        });
      }
      return originalFetch(input, options);
    };
    try {
      const response = await ompDeckApi.request('/api/projects/demo/sessions', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}',
      });
      await new Promise(resolve => setTimeout(resolve, 0));
      return {
        status: response.status,
        calls,
        authenticated: ompDeckApi.authenticated(),
        appVisible: !document.getElementById('app').hidden,
        pairingVisible: !document.getElementById('auth-screen').hidden
          && !document.getElementById('pairing-fields').hidden,
        draft: document.getElementById('message').value,
      };
    } finally {
      window.fetch = originalFetch;
      document.getElementById('message').value = '';
    }
  });
  assert.deepEqual(forbiddenRecovery, {
    status: 403,
    calls: 1,
    authenticated: true,
    appVisible: true,
    pairingVisible: false,
    draft: 'draft survives forbidden response',
  });

  const authOrdering = await page.evaluate(async () => {
    const originalFetch = window.fetch;
    let releaseStale;
    let sessionCalls = 0;
    window.fetch = (input, options) => {
      const pathname = new URL(
        typeof input === 'string' ? input : input.url,
        location.href,
      ).pathname;
      if (pathname !== '/api/auth/session') return originalFetch(input, options);
      sessionCalls += 1;
      if (sessionCalls === 1) {
        return new Promise(resolve => { releaseStale = resolve; });
      }
      if (sessionCalls === 3) {
        return Promise.resolve(new Response('{}', {headers: {'Content-Type': 'application/json'}}));
      }
      return Promise.resolve(new Response(JSON.stringify({
        authenticated: true,
        csrfToken: 'newest-session',
      }), {status: 200, headers: {'Content-Type': 'application/json'}}));
    };
    try {
      const stale = ompDeckApi.session().then(
        () => 'accepted',
        caught => caught.name,
      );
      while (!releaseStale) await new Promise(resolve => setTimeout(resolve, 0));
      const latest = await ompDeckApi.session();
      const invalidOutcome = await ompDeckApi.session().then(() => 'accepted', () => 'rejected');
      releaseStale(new Response(JSON.stringify({authenticated: false}), {
        status: 200,
        headers: {'Content-Type': 'application/json'},
      }));
      return {
        staleOutcome: await stale,
        latestAuthenticated: latest.authenticated,
        remainsAuthenticated: ompDeckApi.authenticated(),
        invalidOutcome,
      };
    } finally {
      window.fetch = originalFetch;
      await ompDeckApi.session();
    }
  });
  assert.deepEqual(authOrdering, {
    staleOutcome: 'AbortError',
    latestAuthenticated: true,
    remainsAuthenticated: true,
    invalidOutcome: 'rejected',
  });
  const rendering = await page.evaluate(() => {
    const markdown = document.createElement('div');
    markdown.innerHTML = renderMarkdown([
      '| A | B |',
      '| --- | --- |',
      '| one | two |',
    ].join('\n'));
    const session = {
      id: 'session-1',
      lifecycle: 'ongoing',
      project: {name: 'demo', path: '/demo'},
      title: 'Demo session',
      state: 'idle',
      unread: true,
      capabilities: {remove: true},
      transport: {kind: 'extension', status: 'connected'},
      subagents: [
        {id: 'working-child', label: 'Worker', status: 'running', activitySummary: 'Testing'},
        {id: 'idle-child', label: 'Sleeper', status: 'completed', activitySummary: 'Done'},
      ],
      messages: [], todos: [],
    };
    state.agents = [
      projectSession({...session, id: 'idle-session', unread: false, subagents: [], workingOn: 'Finished task'}),
      projectSession(session),
    ];
    renderSummary();
    renderList();
    showToast('Started OMP', 'success');
    const card = document.querySelector('[data-id=\"session-1\"]');
    const unreadCount = document.getElementById('unread-count').textContent;
    const cardOrder = [...document.querySelectorAll('.agent-card')].map(element => element.dataset.id);
    const idleSummary = document.querySelector('[data-id="idle-session"] .activity-summary').textContent;
    card.click();
    const remove = document.querySelector('[data-session-action="remove"]');
    const removeVisible = !remove.hidden && !remove.disabled;
    selectedAgent().capabilities.remove = false;
    touchRender();
    renderDetail();
    return {
      tableHeaders: [...markdown.querySelectorAll('th')].map(node => node.textContent),
      tableCells: [...markdown.querySelectorAll('td')].map(node => node.textContent),
      toastClass: document.getElementById('toast').className,
      unreadCount,
      cardText: card.textContent,
      selectedId: state.selectedId,
      cardOrder,
      idleSummary,
      removeVisible,
      removeDenied: remove.hidden,
      metricOverflow: [...document.querySelectorAll('.metric')].some(element => element.scrollWidth > element.clientWidth),
    };
  });
  assert.deepEqual(rendering.tableHeaders, ['A', 'B']);
  assert.deepEqual(rendering.tableCells, ['one', 'two']);
  assert.match(rendering.toastClass, /success/);
  assert.equal(rendering.unreadCount, '1');
  assert.match(rendering.cardText, /Worker/);
  assert.doesNotMatch(rendering.cardText, /Sleeper/);
  assert.equal(rendering.selectedId, 'session-1');
  assert.deepEqual(rendering.cardOrder, ['session-1', 'idle-session']);
  assert.equal(rendering.idleSummary, 'idle');
  assert.equal(rendering.removeVisible, true);
  assert.equal(rendering.removeDenied, true);
  assert.equal(rendering.metricOverflow, false);
  const historyLabels = await page.evaluate(() => {
    const other = state.agents.find(agent => agent.id !== state.selectedId);
    const original = {showHistory: state.showHistory, lifecycle: other.lifecycle};
    try {
      other.lifecycle = 'history';
      state.showHistory = true;
      renderList();
      const enabled = document.querySelectorAll('.agent-disambiguator').length;
      state.showHistory = false;
      renderList();
      return {
        enabled,
        disabled: document.querySelectorAll('.agent-disambiguator').length,
        visibleCards: document.querySelectorAll('.agent-card').length,
      };
    } finally {
      state.showHistory = original.showHistory;
      other.lifecycle = original.lifecycle;
      renderList();
    }
  });
  assert.deepEqual(historyLabels, {enabled: 1, disabled: 0, visibleCards: 1});
  const presentationUi = await page.evaluate(async () => {
    const originalRequest = ompDeckApi.request;
    const selected = selectedAgent();
    const other = state.agents.find(agent => agent.id !== selected.id);
    selected.pinned = false;
    other.pinned = false;
    renderList();
    selected.pinned = false;
    other.pinned = true;
    renderList();
    const pinnedOrder = [...document.querySelectorAll('.agent-card')].map(card => card.dataset.id);
    const trigger = document.querySelector('[data-session-action="rename"]');
    showSessionNameEditor(trigger);
    el['session-name-input'].value = 'Keep this edit';
    ompDeckApi.request = async () => new Response(
      JSON.stringify({error: 'Metadata unavailable'}),
      {status: 503, headers: {'Content-Type': 'application/json'}},
    );
    try {
      await saveSessionName();
      await saveSessionName({reset: true});
      return {
        pinnedOrder,
        dialogOpen: el['session-name-dialog'].open,
        value: el['session-name-input'].value,
        error: el['session-name-status'].textContent,
      };
    } finally {
      selected.pinned = false;
      other.pinned = false;
      renderList();
      ompDeckApi.request = originalRequest;
      closeDialog(el['session-name-dialog'], {restoreFocus: false});
      el.toast.hidden = true;
    }
  });
  assert.deepEqual(presentationUi, {
    pinnedOrder: ['idle-session', 'session-1'],
    dialogOpen: true,
    value: 'Keep this edit',
    error: 'Metadata unavailable',
  });
  await page.waitForFunction(() => [...state.nodes.values()].every(node => !node.refreshRequest));
  const recovery = await page.evaluate(async () => {
    stopEventStreams();
    const originalFetch = window.fetch;
    const baseGeneration = nodeState('local').snapshotGeneration + 10;
    let calls = 0;
    let releaseFirst;
    const response = payload => ({
      ok: true,
      status: 200,
      json: async () => payload,
    });
    const rawSession = title => ({
      id: 'recovery-session',
      lifecycle: 'ongoing',
      ompSessionId: 'omp-recovery',
      title,
      project: {name: 'demo', path: '/work/demo'},
      state: 'idle',
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      capabilities: {},
      messages: [],
      todos: [],
      subagents: [],
      pendingUiRequest: null,
      unread: false,
      updatedAt: '2026-08-20T12:34:56.789Z',
      workingOn: null,
      model: null,
      thinking: null,
    });
    window.fetch = (input, options) => {
      if (input !== '/api/sessions') return originalFetch(input, options);
      calls += 1;
      if (calls === 1) {
        return new Promise(resolve => {
          releaseFirst = () => resolve(response({
            snapshotGeneration: baseGeneration,
            sessions: [rawSession('Stale HTTP')],
          }));
        });
      }
      return Promise.resolve(response({
        snapshotGeneration: baseGeneration + 2,
        sessions: [rawSession('Recovered snapshot')],
      }));
    };
    try {
      const activeRefresh = refresh();
      while (!releaseFirst) await new Promise(resolve => setTimeout(resolve, 0));
      void refresh();
      applyFleetSession(rawSession('Live event'), baseGeneration + 1);
      const liveTitle = state.agents.find(agent => agent.id === 'recovery-session')?.label;
      releaseFirst();
      await activeRefresh;
      while (nodeState('local').refreshRequest) {
        await nodeState('local').refreshRequest.promise;
      }
      const recoveredTitle = state.agents.find(agent => agent.id === 'recovery-session')?.label;
      const staleRequestGeneration = nodeState('local').snapshotGeneration;
      applyFleetSession(rawSession('Newest event'), baseGeneration + 3);
      applyFleetSnapshot({
        snapshotGeneration: baseGeneration + 2,
        sessions: [rawSession('Late stale snapshot')],
      }, staleRequestGeneration);
      const staleIgnoredTitle = state.agents.find(
        agent => agent.id === 'recovery-session'
      )?.label;
      applyFleetSnapshot({
        snapshotGeneration: 1,
        sessions: [rawSession('Restarted server')],
      });
      return {
        calls,
        liveTitle,
        recoveredTitle,
        staleIgnoredTitle,
        restartedTitle: state.agents.find(
          agent => agent.id === 'recovery-session'
        )?.label,
        snapshotGeneration: nodeState('local').snapshotGeneration,
      };
    } finally {
      window.fetch = originalFetch;
    }
  });
  assert.equal(recovery.calls, 2);
  assert.equal(recovery.liveTitle, 'Live event');
  assert.equal(recovery.recoveredTitle, 'Recovered snapshot');
  assert.equal(recovery.staleIgnoredTitle, 'Newest event');
  assert.equal(recovery.restartedTitle, 'Restarted server');
  assert.equal(recovery.snapshotGeneration > 0, true);

  const resumedRecovery = await page.evaluate(async () => {
    stopEventStreams();
    const originalRequest = ompDeckApi.request;
    const node = nodeState('local');
    node.lastEventId = 'resume-cursor';
    const baseGeneration = node.snapshotGeneration + 10;
    let sessionCalls = 0;
    let eventCalls = 0;
    let resolveStaleRefresh;
    let resolveStaleStream;
    const sessionPayload = title => ({
      snapshotGeneration: baseGeneration,
      sessions: [{
        id: 'resume-session',
        lifecycle: 'ongoing',
        ompSessionId: 'omp-resume',
        title,
        project: {name: 'demo', path: '/work/demo'},
        state: 'idle',
        transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
        capabilities: {},
        messages: [],
        todos: [],
        subagents: [],
        pendingUiRequest: null,
        unread: false,
        updatedAt: '2026-08-20T12:34:56.789Z',
        workingOn: null,
        model: null,
        thinking: null,
      }],
    });
    const response = payload => ({
      ok: true,
      status: 200,
      json: async () => payload,
    });
    const eventHeaders = [];
    try {
      ompDeckApi.request = (path, options = {}) => {
        if (path === '/api/sessions') {
          sessionCalls += 1;
          if (sessionCalls === 1) {
            return new Promise(resolve => { resolveStaleRefresh = resolve; });
          }
          return Promise.resolve(response(sessionPayload('Resumed snapshot')));
        }
        if (path === '/api/events') {
          eventCalls += 1;
          eventHeaders.push(new Headers(options.headers).get('Last-Event-ID'));
          if (eventCalls === 1) {
            return new Promise(resolve => { resolveStaleStream = resolve; });
          }
          return new Promise((resolve, reject) => {
            options.signal.addEventListener('abort', () => {
              reject(new DOMException('Stream replaced', 'AbortError'));
            }, {once: true});
          });
        }
        return originalRequest(path, options);
      };
      const staleRefresh = refresh();
      startNodeEventStream('local');
      while (!resolveStaleRefresh || !resolveStaleStream) {
        await new Promise(resolve => setTimeout(resolve, 0));
      }
      document.getElementById('message').value = 'resume draft';
      document.dispatchEvent(new Event('visibilitychange'));
      while (sessionCalls < 2 || eventCalls < 2 || node.refreshRequest) {
        await new Promise(resolve => setTimeout(resolve, 0));
      }
      const titleAfterResume = state.agents.find(agent => agent.id === 'resume-session')?.label;
      resolveStaleRefresh(response({
        ...sessionPayload('Obsolete snapshot'),
        snapshotGeneration: baseGeneration + 1,
      }));
      const staleFrame = [
        'id: obsolete-event',
        'event: fleet',
        `data: ${JSON.stringify({
          type: 'snapshot',
          payload: {
            ...sessionPayload('Obsolete stream'),
            snapshotGeneration: baseGeneration + 2,
          },
        })}`,
        '',
        '',
      ].join('\n');
      resolveStaleStream(new Response(staleFrame, {
        status: 200,
        headers: {'Content-Type': 'text/event-stream'},
      }));
      await staleRefresh;
      await new Promise(resolve => setTimeout(resolve, 0));
      return {
        titleAfterResume,
        titleAfterStale: state.agents.find(agent => agent.id === 'resume-session')?.label,
        eventHeaders,
        draft: document.getElementById('message').value,
      };
    } finally {
      stopEventStreams();
      ompDeckApi.request = originalRequest;
      document.getElementById('message').value = '';
    }
  });
  assert.deepEqual(resumedRecovery, {
    titleAfterResume: 'Resumed snapshot',
    titleAfterStale: 'Resumed snapshot',
    eventHeaders: ['resume-cursor', 'resume-cursor'],
    draft: 'resume draft',
  });

  const normalizedEventOrdering = await page.evaluate(async () => {
    const machineId = 'local';
    const sessionId = 'normalized-ordering-session';
    const projectedId = ompDeckApi.sessionKey(machineId, sessionId);
    const node = nodeState(machineId);
    clearTimeout(node.recoveryTimer);
    node.recoveryTimer = null;
    const baseGeneration = node.snapshotGeneration + 20;
    const rawSession = (title, messages = []) => ({
      lifecycle: 'ongoing',
      id: sessionId,
      ompSessionId: 'omp-normalized-ordering',
      title,
      project: {name: 'demo', path: '/work/demo'},
      state: 'idle',
      transport: {
        kind: 'rpc',
        status: 'connected',
        label: 'RPC',
        instanceId: 'normalized-instance',
      },
      capabilities: {},
      messages,
      todos: [],
      subagents: [],
      pendingUiRequest: null,
      unread: false,
      updatedAt: '2026-08-20T12:34:56.789Z',
      workingOn: null,
      model: null,
      thinking: null,
    });
    const normalizedEvent = (snapshotGeneration, title) => JSON.stringify({
      id: snapshotGeneration,
      snapshotGeneration,
      sessionId,
      instanceId: 'normalized-instance',
      generation: 7,
      sequence: 41,
      type: 'session_read',
      payload: {session: rawSession(title)},
    });
    const visibleTitle = () => (
      document.querySelector(`[data-id="${projectedId}"]`)?.textContent || ''
    );

    applyFleetSnapshot({
      snapshotGeneration: baseGeneration,
      sessions: [rawSession('Authoritative snapshot')],
    }, null, machineId);
    applyNodeEvent(machineId, normalizedEvent(baseGeneration + 1, 'First normalized update'));
    const firstVisible = visibleTitle();
    applyNodeEvent(machineId, normalizedEvent(baseGeneration + 2, 'Second normalized update'));
    const secondVisible = visibleTitle();
    applyNodeEvent(machineId, normalizedEvent(baseGeneration + 1, 'Stale normalized update'));
    const afterStale = visibleTitle();
    applyNodeEvent(machineId, normalizedEvent(baseGeneration + 4, 'Gapped normalized update'));
    const afterGap = visibleTitle();
    clearTimeout(node.recoveryTimer);
    node.recoveryTimer = null;

    const largeText = `supported-snapshot-start 雪 ${'x'.repeat(224 * 1024)} supported-snapshot-end`;
    const snapshotData = JSON.stringify({
      id: baseGeneration + 3,
      type: 'snapshot',
      payload: {
        snapshotGeneration: baseGeneration + 3,
        sessions: [rawSession('Large 雪 transcript', [{
          id: 'large-message',
          role: 'assistant',
          content: [{type: 'text', text: largeText}],
          createdAt: '2026-08-20T12:34:56.789Z',
        }])],
      },
    });
    const frame = `id: browser-regression:${baseGeneration + 3}\r\nevent: fleet\r\ndata:\r\ndata: ${snapshotData}\r\n\r\n`;
    const encodedFrame = new TextEncoder().encode(frame);
    const fragmentedBody = new ReadableStream({
      start(controller) {
        let offset = 0;
        while (offset < encodedFrame.length) {
          const width = offset < 1024 ? 1 : 2048;
          controller.enqueue(encodedFrame.slice(offset, offset + width));
          offset += width;
        }
        controller.close();
      },
    });
    const stream = {
      controller: new AbortController(),
      failures: 4,
      lastEventId: '',
    };
    const previousStream = node.stream;
    node.stream = stream;
    try {
      await consumeSse(new Response(fragmentedBody, {
        status: 200,
        headers: {'Content-Type': 'text/event-stream'},
      }), machineId, stream);
    } finally {
      node.stream = previousStream;
    }
    selectAgent(projectedId, {pushHistory: false});

    return {
      firstVisible,
      secondVisible,
      afterStale,
      afterGap,
      largeTitleVisible: visibleTitle(),
      largeTranscriptVisible: document.getElementById('activity').textContent.includes(
        'supported-snapshot-end'
      ),
      replayCursor: stream.lastEventId,
    };
  });
  assert.match(normalizedEventOrdering.firstVisible, /First normalized update/);
  assert.match(normalizedEventOrdering.secondVisible, /Second normalized update/);
  assert.match(normalizedEventOrdering.afterStale, /Second normalized update/);
  assert.match(normalizedEventOrdering.afterGap, /Second normalized update/);
  assert.match(normalizedEventOrdering.largeTitleVisible, /Large 雪 transcript/);
  assert.equal(normalizedEventOrdering.largeTranscriptVisible, true);
  assert.match(normalizedEventOrdering.replayCursor, /^browser-regression:\d+$/);

  const lifecycleDashboard = await page.evaluate(async () => {
    const makeAgent = (id, lifecycle, executionState, overrides = {}) => ({
      id, lifecycle, executionState, status: executionState,
      project: 'deck', label: `${id} conversation`, machineName: 'Workstation',
      machineId: 'local', nodeAvailable: true, bridgeConnected: true,
      inboxState: 'read', capabilities: {sendMessage: true, attachments: true},
      attachmentLimits: {maxImages: 4, maxTotalBytes: 20 * 1024 * 1024},
      transport: {status: 'connected', label: 'Session connected'},
      subagents: [], pendingUiRequest: null,
      session: {activity: [], archiveActivity: [], updatedMs: Date.now()},
      ...overrides,
    });
    const active = makeAgent('active', 'ongoing', 'working', {
      historyError: '<history stream unavailable>',
    });
    const recovering = makeAgent('recovering', 'ongoing', 'recovering', {
      bridgeConnected: false,
      transport: {status: 'reconnecting', label: 'Reconnecting'},
    });
    const offline = makeAgent('offline', 'ongoing', 'idle', {nodeAvailable: false});
    const unknown = makeAgent('unknown', 'unknown', 'idle');
    const saved = makeAgent('saved', 'history', 'disconnected', {
      bridgeConnected: false,
      capabilities: {},
      session: {
        activity: [{id: 'image-only', kind: 'assistant', title: 'OMP', detail: '', attachments: [{unavailable: true}]}],
        archiveActivity: [],
        updatedMs: Date.now(),
      },
    });
    state.agents = [active, recovering, offline, unknown, saved];
    state.sessionScope = 'ongoing';
    state.filter = 'all';
    renderSummary();
    renderList();
    const ongoing = [...el['agent-list'].querySelectorAll('.agent-card')].map(card => card.dataset.id);
    const counts = {
      working: el['working-count'].textContent,
      attention: el['attention-count'].textContent,
      idle: el['idle-count'].textContent,
      history: el['history-count'].textContent,
    };
    selectAgent(active.id, {pushHistory: false});
    const originalRequest = ompDeckApi.request;
    let composerPath = null;
    ompDeckApi.request = async (path, options) => {
      if (path.endsWith('/deliveries')) return {ok: true, json: async () => ({version: 1, deliveries: []})};
      composerPath = path;
      return {ok: true, status: 202, json: async () => ({requestId: JSON.parse(options.body).requestId, status: 'accepted', acceptance: 'accepted'})};
    };
    el.message.value = 'Control channel still works';
    el.composer.requestSubmit();
    await new Promise(resolve => setTimeout(resolve, 0));
    ompDeckApi.request = originalRequest;
    const historyFailure = {
      sendDisabled: el.send.disabled,
      messageDisabled: el.message.disabled,
      attachDisabled: el.attach.disabled,
      reasonVisible: el.detail.querySelector('.history-warning')?.textContent
        .includes('<history stream unavailable>'),
      reasonEscaped: el.detail.querySelector('.history-warning')?.innerHTML
        .includes('&lt;history stream unavailable&gt;'),
      composerPath,
    };
    active.historyError = null;
    active.session.updatedMs += 1;
    touchRender();
    renderSummary();
    renderDetail();
    const authoritativeHistory = {
      attention: el['attention-count'].textContent,
      warningPresent: Boolean(el.detail.querySelector('.history-warning')),
      sendDisabled: el.send.disabled,
    };
    selectAgent(active.id, {pushHistory: false});
    el.message.value = 'draft survives hidden history';
    el.message.dispatchEvent(new Event('input', {bubbles: true}));
    state.showArchive = true;
    state.showHistory = false;
    renderSummary();
    renderList();
    const rejectedHiddenSelection = selectAgent(saved.id, {pushHistory: false});
    const hiddenPresentation = {
      toggleHidden: el['history-toggle'].hidden,
      savedListed: Boolean(el['agent-list'].querySelector('[data-id="saved"]')),
      rejectedHiddenSelection,
      selectedAfterRejection: state.selectedId,
      archiveAfterRejection: state.showArchive,
      draftAfterRejection: el.message.value,
    };

    let url = new URL(location.href);
    url.searchParams.set('session', saved.id);
    history.replaceState({view: 'chat', session: saved.id}, '', url);
    state.pendingSelection = saved.id;
    renderFleet();
    const hiddenDeepLink = {
      selected: state.selectedId,
      pending: state.pendingSelection,
      session: new URL(location.href).searchParams.get('session'),
    };

    selectAgent(active.id, {pushHistory: false});
    url = new URL(location.href);
    url.searchParams.set('session', saved.id);
    history.replaceState({view: 'chat', session: saved.id}, '', url);
    window.dispatchEvent(new PopStateEvent('popstate', {
      state: {view: 'chat', session: saved.id},
    }));
    const hiddenPopstate = {
      selected: state.selectedId,
      session: new URL(location.href).searchParams.get('session'),
    };

    selectAgent(active.id, {pushHistory: false});
    active.lifecycle = 'history';
    touchRender();
    renderFleet();
    const hiddenTransitionSelection = state.selectedId;
    active.lifecycle = 'ongoing';
    touchRender();
    renderFleet();
    selectAgent(active.id, {pushHistory: false});
    const hiddenTransitionDraft = el.message.value;

    state.showHistory = true;
    state.showArchive = false;
    renderSummary();
    renderList();
    state.sessionScope = 'history';
    renderSummary();
    renderList();
    const historySessions = [...el['agent-list'].querySelectorAll('.agent-card')]
      .map(card => card.dataset.id);
    const ongoingReturnCount = el['history-count'].textContent;
    selectAgent(active.id, {pushHistory: false});
    el.message.value = 'draft survives history';
    el.message.dispatchEvent(new Event('input', {bubbles: true}));
    selectAgent(saved.id, {pushHistory: false});
    selectAgent(active.id, {pushHistory: false});
    const restoredDraft = el.message.value;
    selectAgent(saved.id, {pushHistory: false});
    const historyDetail = {
      sendDisabled: el.send.disabled,
      placeholder: el.message.placeholder,
      status: el['transport-status'].title,
      imageUnavailable: el.activity.textContent.includes('Image unavailable'),
    };
    el.detail.querySelector('[data-back-to-sessions]').click();
    state.sessionScope = 'ongoing';
    renderSummary();
    renderList();
    return {
      ongoing, history: historySessions, counts, ongoingReturnCount, historyFailure, authoritativeHistory,
      hiddenPresentation, hiddenDeepLink, hiddenPopstate, hiddenTransitionSelection,
      hiddenTransitionDraft, historyDetail, restoredDraft, selectedAfterReturn: state.selectedId,
    };
  });
  assert.deepEqual(lifecycleDashboard.ongoing.sort(), ['active', 'offline', 'recovering', 'unknown']);
  assert.deepEqual(lifecycleDashboard.history, ['saved']);
  assert.equal(lifecycleDashboard.ongoingReturnCount, '4');
  assert.deepEqual(lifecycleDashboard.counts, {
    working: '1', attention: '4', idle: '1', history: '1',
  });
  assert.deepEqual(lifecycleDashboard.historyFailure, {
    sendDisabled: false,
    messageDisabled: false,
    attachDisabled: false,
    reasonVisible: true,
    reasonEscaped: true,
    composerPath: '/api/sessions/active/messages',
  });
  assert.deepEqual(lifecycleDashboard.authoritativeHistory, {
    attention: '3',
    warningPresent: false,
    sendDisabled: false,
  });
  assert.deepEqual(lifecycleDashboard.hiddenPresentation, {
    toggleHidden: true,
    savedListed: false,
    rejectedHiddenSelection: false,
    selectedAfterRejection: 'active',
    archiveAfterRejection: true,
    draftAfterRejection: 'draft survives hidden history',
  });
  assert.deepEqual(lifecycleDashboard.hiddenDeepLink, {
    selected: null,
    pending: null,
    session: null,
  });
  assert.deepEqual(lifecycleDashboard.hiddenPopstate, {
    selected: null,
    session: null,
  });
  assert.equal(lifecycleDashboard.hiddenTransitionSelection, null);
  assert.equal(lifecycleDashboard.hiddenTransitionDraft, 'draft survives hidden history');
  assert.deepEqual(lifecycleDashboard.historyDetail, {
    sendDisabled: true,
    placeholder: 'History is read-only',
    status: 'History · no live session connection',
    imageUnavailable: true,
  });
  assert.equal(lifecycleDashboard.restoredDraft, 'draft survives history');
  assert.equal(lifecycleDashboard.selectedAfterReturn, null);
  const queueBehavior = await page.evaluate(async () => {

    const originalRequest = ompDeckApi.request;
    const agent = {
      id: 'queue-session',
      project: 'demo',
      label: 'Queue session',
      lifecycle: 'ongoing',
      status: 'idle',
      executionState: 'idle',
      inboxState: 'read',
      bridgeConnected: true,
      capabilities: {sendMessage: true, followUp: true, attachments: true},
      attachmentLimits: {maxImages: 4, maxTotalBytes: 20 * 1024 * 1024},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      subagents: [],
      session: {activity: [], todo: '', updatedMs: Date.now()},
    };
    state.agents = [agent];
    selectAgent(agent.id, {pushHistory: false});
    const response = options => ({ok: true, status: 202, json: async () => ({requestId: JSON.parse(options.body).requestId, status: 'accepted', acceptance: 'accepted'})});
    const submit = message => {
      document.getElementById('message').value = message;
      document.getElementById('composer').requestSubmit();
    };
    const waitUntil = async (predicate, label) => {
      const deadline = Date.now() + 1000;
      while (!predicate()) {
        if (Date.now() >= deadline) {
          throw new Error(`composer queue timed out: ${label}; ${JSON.stringify({
            selectedId: state.selectedId,
            selectedAgent: selectedAgent()?.id,
            message: document.getElementById('message').value,
            sendDisabled: document.getElementById('send').disabled,
            toast: document.getElementById('toast').textContent,
          })}`);
        }
        await new Promise(resolve => setTimeout(resolve, 0));
      }
    };

    try {
      const routes = [];
      let releaseFirst;
      let releaseSecond;
      ompDeckApi.request = (path, options) => {
        if (path.endsWith('/deliveries')) return Promise.resolve({ok: true, json: async () => ({version: 1, deliveries: []})});
        routes.push(path);
        if (routes.length <= 2) {
          return new Promise(resolve => {
            const release = () => resolve(response(options));
            if (routes.length === 1) releaseFirst = release;
            else releaseSecond = release;
          });
        }
        return Promise.resolve(response(options));
      };
      submit('first');
      await waitUntil(() => Boolean(releaseFirst), 'first request');
      submit('second');
      agent.status = 'working';
      agent.executionState = 'working';
      releaseFirst();
      await waitUntil(() => routes.length === 2, 'follow-up request');
      const statusWhileSecondPending = document.getElementById('send-state').textContent;
      releaseSecond();
      await waitUntil(() => composer.pendingFor(agent.id).length === 0, 'follow-up acceptance');

      agent.status = 'idle';
      agent.executionState = 'idle';
      const failedRequests = [];
      let rejectFirst;
      ompDeckApi.request = (path, options) => {
        if (path.endsWith('/deliveries')) return Promise.resolve({ok: true, json: async () => ({version: 1, deliveries: []})});
        failedRequests.push({path, body: JSON.parse(options.body)});
        return new Promise((resolve, reject) => {
          rejectFirst = reject;
        });
      };
      submit('failed first');
      await waitUntil(() => Boolean(rejectFirst), 'failed request');
      submit('preserved second');
      rejectFirst(new Error('offline'));
      await waitUntil(
        () => document.getElementById('toast').textContent.includes('offline'),
        'failure restoration',
      );
      const restoredDraft = document.getElementById('message').value;
      const retryRequests = [];
      ompDeckApi.request = (path, options) => {
        if (path.endsWith('/deliveries')) return Promise.resolve({ok: true, json: async () => ({version: 1, deliveries: []})});
        retryRequests.push({path, body: JSON.parse(options.body)});
        return Promise.resolve(response(options));
      };
      submit(restoredDraft);
      await waitUntil(() => retryRequests.length === 2, 'safe request retry');

      return {
        routes,
        failedRequests,
        retryRequests,
        restoredDraft,
        statusWhileSecondPending,
      };
    } finally {
      ompDeckApi.request = originalRequest;
    }
  });
  assert.deepEqual(queueBehavior.routes, [
    '/api/sessions/queue-session/messages',
    '/api/sessions/queue-session/messages',
  ]);
  assert.equal(queueBehavior.statusWhileSecondPending, 'Submitting',
    'an older acceptance must not replace a newer submitting status');
  assert.deepEqual(
    queueBehavior.failedRequests.map(request => request.path),
    ['/api/sessions/queue-session/messages'],
  );
  assert.equal(queueBehavior.restoredDraft, 'failed first\npreserved second');
  assert.deepEqual(
    queueBehavior.retryRequests.map(request => request.body.message),
    ['failed first', 'preserved second'],
  );
  assert.equal(
    queueBehavior.retryRequests[0].body.requestId,
    queueBehavior.failedRequests[0].body.requestId,
  );

  const acceptedReceiptProjection = await page.evaluate(async () => {
    const originalRequest = ompDeckApi.request;
    const previousAgents = state.agents;
    const previousSelectedId = state.selectedId;
    const agent = {
      id: 'accepted-receipt-session', project: 'demo', label: 'Accepted receipt session',
      lifecycle: 'ongoing',
      status: 'idle', executionState: 'idle', inboxState: 'read',
      bridgeConnected: true, capabilities: {sendMessage: true},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      pendingUiRequest: null, subagents: [],
      session: {activity: [], todo: '', updatedMs: Date.now()},
    };
    let requestCount = 0;
    const waitUntil = async predicate => {
      const deadline = Date.now() + 2_000;
      while (!predicate()) {
        if (Date.now() >= deadline) throw new Error('accepted receipt projection timed out');
        await new Promise(resolve => setTimeout(resolve, 0));
      }
    };
    try {
      ompDeckApi.request = async (path, options = {}) => {
        if (path.endsWith('/deliveries')) {
          return {ok: true, json: async () => ({version: 1, deliveries: []})};
        }
        requestCount += 1;
        const body = JSON.parse(options.body);
        return {ok: true, status: 202, json: async () => ({
          requestId: body.requestId, status: 'accepted', acceptance: 'accepted',
        })};
      };
      state.agents = [agent];
      selectAgent(agent.id, {pushHistory: false});
      for (let index = 0; index < 1_001; index += 1) {
        el.message.value = `accepted ${index}`;
        el.composer.requestSubmit();
        await waitUntil(() => composer.pendingFor(agent.id).length === 0);
      }
      return {
        requestCount,
        pending: composer.pendingFor(agent.id).length,
        acceptedCards: [...el.activity.querySelectorAll('.event-title')]
          .filter(title => /accepted/i.test(title.textContent)).length,
        deliveryControls: el.activity.querySelectorAll('[data-delivery-action]').length,
        status: el['send-state'].textContent,
      };
    } finally {
      composer.remove(agent.id);
      ompDeckApi.request = originalRequest;
      state.agents = previousAgents;
      state.selectedId = previousSelectedId;
      composer.select(previousSelectedId);
    }
  });
  assert.deepEqual(acceptedReceiptProjection, {
    requestCount: 1_001,
    pending: 0,
    acceptedCards: 0,
    deliveryControls: 0,
    status: 'Accepted',
  });

  const clearedQueue = await page.evaluate(async () => {
    const originalRequest = ompDeckApi.request;
    const agent = state.agents.find(item => item.id === 'queue-session');
    state.agents = [agent];
    state.selectedId = agent.id;
    agent.status = 'idle';
    agent.executionState = 'idle';
    composer.select(agent.id);
    const routes = [];
    let releaseFirst;
    const response = options => ({ok: true, status: 202, json: async () => ({requestId: JSON.parse(options.body).requestId, status: 'accepted', acceptance: 'accepted'})});
    const waitUntil = async predicate => {
      const deadline = Date.now() + 1000;
      while (!predicate()) {
        if (Date.now() >= deadline) throw new Error('composer clear timed out');
        await new Promise(resolve => setTimeout(resolve, 0));
      }
    };
    try {
      ompDeckApi.request = (path, options) => {
        if (path.endsWith('/deliveries')) return Promise.resolve({ok: true, json: async () => ({version: 1, deliveries: []})});
        routes.push(path);
        if (routes.length === 1) {
          return new Promise(resolve => {
            releaseFirst = () => resolve(response(options));
          });
        }
        return Promise.resolve(response(options));
      };
      document.getElementById('message').value = 'in flight';
      document.getElementById('composer').requestSubmit();
      await waitUntil(() => Boolean(releaseFirst));
      document.getElementById('message').value = 'must be cancelled';
      document.getElementById('composer').requestSubmit();
      composer.clearLocalData();
      releaseFirst();
      await new Promise(resolve => setTimeout(resolve, 25));
      return routes;
    } finally {
      ompDeckApi.request = originalRequest;
    }
  });
  assert.deepEqual(clearedQueue, ['/api/sessions/queue-session/messages']);
  const clearedAttachmentRead = await page.evaluate(async () => {
    const OriginalFileReader = globalThis.FileReader;
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    const readers = [];
    const revoked = [];
    class ControlledFileReader {
      constructor() {
        this.result = null;
        this.aborted = false;
        readers.push(this);
      }
      readAsDataURL() {}
      abort() {
        this.aborted = true;
        this.onabort?.();
      }
    }
    const input = document.getElementById('attach');
    const choose = file => {
      Object.defineProperty(input, 'files', {
        configurable: true,
        value: [file],
      });
      input.dispatchEvent(new Event('change'));
    };
    try {
      globalThis.FileReader = ControlledFileReader;
      URL.createObjectURL = () => 'blob:audit';
      URL.revokeObjectURL = url => revoked.push(url);

      choose(new File(['first'], 'first.png', {type: 'image/png'}));
      await new Promise(resolve => setTimeout(resolve, 0));
      composer.clearLocalData();
      const firstAborted = readers[0].aborted;

      choose(new File(['second'], 'second.png', {type: 'image/png'}));
      await new Promise(resolve => setTimeout(resolve, 0));
      readers[1].result = 'data:image/png;base64,c2Vjb25k';
      readers[1].onload();
      composer.clearLocalData();
      await new Promise(resolve => setTimeout(resolve, 0));

      return {
        firstAborted,
        revoked,
        rendered: document.getElementById('attachments').children.length,
      };
    } finally {
      globalThis.FileReader = OriginalFileReader;
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
    }
  });
  assert.equal(clearedAttachmentRead.firstAborted, true);
  assert.deepEqual(clearedAttachmentRead.revoked, ['blob:audit']);
  assert.equal(clearedAttachmentRead.rendered, 0);

  const concurrentAttachments = await page.evaluate(async () => {
    const originalAgents = state.agents;
    const originalSelection = state.selectedId;
    state.agents = [...originalAgents, ...['attachment-limit-session', 'other-attachment-session'].map(id => ({
      id, lifecycle: 'ongoing', capabilities: {attachments: true},
      attachmentLimits: {maxImages: 4, maxTotalBytes: 20 * 1024 * 1024},
    }))];
    const OriginalFileReader = globalThis.FileReader;
    const originalCreateObjectURL = URL.createObjectURL;
    const readers = [];
    let nextUrl = 0;
    class ControlledFileReader {
      constructor() {
        this.result = null;
        readers.push(this);
      }
      readAsDataURL() {}
      abort() {
        this.onabort?.();
      }
    }
    const input = document.getElementById('attach');
    const choose = files => {
      Object.defineProperty(input, 'files', {
        configurable: true,
        value: files,
      });
      input.dispatchEvent(new Event('change'));
    };
    try {
      composer.clearLocalData();
      composer.select('attachment-limit-session');
      globalThis.FileReader = ControlledFileReader;
      URL.createObjectURL = () => `blob:limit-${++nextUrl}`;
      const batch = prefix => [0, 1, 2].map(index => new File(
        ['image'],
        `${prefix}-${index}.png`,
        {type: 'image/png'},
      ));

      choose(batch('first'));
      choose(batch('second'));
      composer.select('other-attachment-session');
      for (const reader of readers) {
        reader.result = 'data:image/png;base64,aW1hZ2U=';
        reader.onload();
      }
      await new Promise(resolve => setTimeout(resolve, 0));
      const otherRendered = document.getElementById('attachments').children.length;
      composer.select('attachment-limit-session');
      const countReads = readers.length;
      const countRendered = document.getElementById('attachments').children.length;

      composer.clearLocalData();
      readers.length = 0;
      state.agents.find(agent => agent.id === 'attachment-limit-session').attachmentLimits.maxTotalBytes = 10 * 1024 * 1024;
      const largeBatch = prefix => [0, 1].map(index => new File(
        [new Uint8Array(3 * 1024 * 1024)],
        `${prefix}-${index}.png`,
        {type: 'image/png'},
      ));
      choose(largeBatch('large-first'));
      choose(largeBatch('large-second'));
      for (const reader of readers) {
        reader.result = 'data:image/png;base64,aW1hZ2U=';
        reader.onload();
      }
      await new Promise(resolve => setTimeout(resolve, 0));
      return {
        countReads,
        countRendered,
        otherRendered,
        byteReads: readers.length,
        byteRendered: document.getElementById('attachments').children.length,
      };
    } finally {
      composer.clearLocalData();
      state.agents = originalAgents;
      composer.select(originalSelection);
      globalThis.FileReader = OriginalFileReader;
      URL.createObjectURL = originalCreateObjectURL;
    }
  });
  assert.deepEqual(concurrentAttachments, {
    countReads: 3,
    countRendered: 3,
    otherRendered: 0,
    byteReads: 2,
    byteRendered: 2,
  });


  const thinkingChanges = await page.evaluate(async () => {
    const originalRequest = ompDeckApi.request;
    const requests = [];
    const advertised = ['off', 'low', 'medium', 'high', 'xhigh'];
    let authoritative = 'xhigh';
    let settable = true;
    let transportReason = null;
    const visibleLevel = chip => chip.textContent.match(/\b(off|minimal|low|medium|high|xhigh|max|auto)\b/)?.[1] ?? null;
    const rawSession = () => ({
      id: 'thinking-session', title: 'Thinking session',
      project: {name: 'demo', path: '/demo'}, state: 'idle', unread: false,
      lifecycle: 'ongoing',
      transport: {kind: 'extension', status: 'connected', reason: transportReason},
      capabilities: {setThinking: settable}, thinking: authoritative,
      thinkingLevels: settable ? advertised : [],
      messages: [], todos: [], subagents: [], updatedAt: new Date().toISOString(),
    });
    const waitUntil = async predicate => {
      const deadline = Date.now() + 1000;
      while (!predicate()) {
        if (Date.now() > deadline) throw new Error('Thinking queue did not settle');
        await new Promise(resolve => setTimeout(resolve, 0));
      }
    };
    const cycleTwice = async secondSucceeds => {
      const start = requests.length;
      document.querySelector('[data-thinking-level]').click();
      document.querySelector('[data-thinking-level]').click();
      await new Promise(resolve => setTimeout(resolve, 0));
      const simultaneous = requests.length - start;
      requests[start].resolve({ok: false, status: 500});
      await waitUntil(() => requests.length === start + 2);
      if (secondSucceeds) authoritative = requests[start + 1].level;
      requests[start + 1].resolve({ok: secondSucceeds, status: secondSucceeds ? 200 : 500});
      await waitUntil(() => !thinkingOverrides.has('thinking-session'));
      return {
        simultaneous,
        levels: requests.slice(start, start + 2).map(request => request.level),
        level: visibleLevel(document.querySelector('[data-thinking-level]')),
      };
    };
    try {
      ompDeckApi.request = (path, options) => {
        if (path.endsWith('/thinking')) {
          return new Promise(resolve => requests.push({resolve, level: JSON.parse(options.body).level}));
        }
        if (path === '/api/sessions') {
          return Promise.resolve({ok: true, json: async () => ({
            snapshotGeneration: nodeState('local').snapshotGeneration + 1,
            sessions: [rawSession()],
          })});
        }
        return originalRequest(path, options);
      };
      await refresh('local');
      selectAgent('thinking-session');
      const accepted = await cycleTwice(true);
      authoritative = 'high';
      await refresh('local');
      const external = visibleLevel(document.querySelector('[data-thinking-level]'));
      authoritative = 'auto';
      await refresh('local');
      const reportedAuto = visibleLevel(document.querySelector('[data-thinking-level]'));
      authoritative = null;
      await refresh('local');
      const unknownButton = document.querySelector('[data-thinking-level]');
      const chipContainer = document.getElementById('detail-chips');
      const chipRect = unknownButton.getBoundingClientRect();
      const containerRect = chipContainer.getBoundingClientRect();
      const unavailable = {
        level: visibleLevel(unknownButton),
        editable: unknownButton.tagName === 'BUTTON',
        visible: chipRect.width > 0 && containerRect.width > 0
          && chipRect.left >= containerRect.left && chipRect.right <= containerRect.right,
      };
      const unknownStart = requests.length;
      unknownButton.click();
      await waitUntil(() => requests.length === unknownStart + 1);
      const selectedFromUnavailable = requests[unknownStart].level;
      requests[unknownStart].resolve({ok: false, status: 500});
      await waitUntil(() => !thinkingOverrides.has('thinking-session'));
      settable = false;
      authoritative = 'high';
      transportReason = 'Requires <terminal restart>';
      await refresh('local');
      const readOnlyChip = [...document.querySelectorAll('#detail-chips .chip')]
        .find(chip => chip.textContent.startsWith('thinking:'));
      document.querySelector('[data-session-info]').click();
      const details = document.getElementById('session-metadata');
      const readOnly = {
        level: visibleLevel(readOnlyChip),
        editable: Boolean(document.querySelector('[data-thinking-level]')),
        reasonVisible: details.textContent.includes(transportReason),
        reasonEscaped: details.querySelector('terminal') === null,
      };
      document.querySelector('#session-info [data-close-dialog]').click();
      settable = true;
      transportReason = null;
      authoritative = 'xhigh';
      await refresh('local');
      const failed = await cycleTwice(false);
      return {accepted, external, reportedAuto, unavailable, selectedFromUnavailable, readOnly, failed};
    } finally {
      ompDeckApi.request = originalRequest;
    }
  });
  assert.deepEqual(thinkingChanges, {
    accepted: {simultaneous: 1, levels: ['off', 'low'], level: 'low'},
    external: 'high',
    reportedAuto: 'auto',
    unavailable: {level: null, editable: true, visible: true},
    selectedFromUnavailable: 'off',
    readOnly: {
      level: 'high',
      editable: false,
      reasonVisible: true,
      reasonEscaped: true,
    },
    failed: {simultaneous: 1, levels: ['off', 'low'], level: 'xhigh'},
  });

  const multiAsk = await page.evaluate(async () => {
    const originalRequest = ompDeckApi.request;
    const pending = {
      id: 'ask-1',
      kind: 'select',
      title: 'Choose',
      questionIndex: 0,
      questions: [{
        prompt: 'Pick values',
        multi: true,
        checkedIndices: [0],
        options: [
          {label: 'First', value: 'first'},
          {label: 'Second', value: 'second'},
        ],
      }],
    };
    const agent = {
      id: 'ask-session',
      bridgeConnected: false,
      capabilities: {answerUiRequest: true},
      pendingUiRequest: pending,
    };
    state.agents = [agent];
    state.selectedId = agent.id;
    renderPendingRequest(agent);
    const disconnectedHasForm = Boolean(
      el['pending-request'].querySelector('[data-pending-form]'),
    );
    agent.bridgeConnected = true;
    renderPendingRequest(agent);
    const choices = [
      ...el['pending-request'].querySelectorAll('[name="pending-choice"]'),
    ];
    choices[1].checked = true;
    let body;
    try {
      ompDeckApi.request = async (_path, options) => {
        body = JSON.parse(options.body);
        return {ok: true, status: 202, json: async () => ({})};
      };
      await settlePendingRequest();
    } finally {
      ompDeckApi.request = originalRequest;
    }
    return {
      disconnectedHasForm,
      inputTypes: choices.map(choice => choice.type),
      value: body?.value,
    };
  });
  assert.equal(multiAsk.disconnectedHasForm, false);
  assert.deepEqual(multiAsk.inputTypes, ['checkbox', 'checkbox']);
  assert.deepEqual(multiAsk.value, ['first', 'second']);

  const staleAsk = await page.evaluate(async () => {
    const originalRequest = ompDeckApi.request;
    const makePending = (id, label) => ({
      id, kind: 'select', title: label, questionIndex: 0,
      questions: [{prompt: label, options: [{label, value: id}]}],
    });
    const agent = {
      id: 'stale-ask-session', bridgeConnected: true,
      capabilities: {answerUiRequest: true},
      pendingUiRequest: makePending('ask-a', 'Question A'),
    };
    state.agents = [agent];
    state.selectedId = agent.id;
    renderPendingRequest(agent);
    el['pending-request'].querySelector('[name="pending-choice"]').checked = true;
    agent.pendingUiRequest = makePending('ask-b', 'Question B');
    const calls = [];
    try {
      ompDeckApi.request = async (path, options) => {
        calls.push({path, body: JSON.parse(options.body)});
        return {ok: true, status: 202, json: async () => ({})};
      };
      await settlePendingRequest();
      renderPendingRequest(agent);
      return {
        calls,
        heading: el['pending-request'].querySelector('strong')?.textContent,
      };
    } finally {
      ompDeckApi.request = originalRequest;
    }
  });
  assert.deepEqual(staleAsk.calls, []);
  assert.equal(staleAsk.heading, 'Question B');

  const cspProgress = await page.evaluate(() => {
    const host = document.createElement('div');
    host.innerHTML = TodoUI.render(
      '  Build:\n    - [X] First\n    - [ ] Second\n    - [ ] Third\n    - [ ] Fourth',
      escapeHtml,
    );
    document.body.append(host);
    renderUsage({reports: [{
      provider: 'test',
      limits: [
        {label: 'Fresh', amount: {usedFraction: 0}},
        {label: 'Window', amount: {usedFraction: 0.25}},
        {label: 'Spent', amount: {usedFraction: 1}},
      ],
    }]});
    const todo = host.querySelector('progress');
    const usage = [...document.querySelectorAll('#usage-list progress')];
    const result = {
      todo: {value: todo.value, max: todo.max, style: todo.getAttribute('style')},
      usage: usage.map(item => ({value: item.value, max: item.max, style: item.getAttribute('style')})),
    };
    host.remove();
    return result;
  });

  const disconnectedShutdown = await page.evaluate(() => {
    const agent = {
      id: 'disconnected-rpc-shutdown', project: 'demo', label: 'Stopped RPC',
      lifecycle: 'ongoing',
      bridgeConnected: false, nodeAvailable: true, executionState: 'disconnected',
      inboxState: 'read', capabilities: {shutdown: true},
      transport: {kind: 'rpc', status: 'disconnected', label: 'Disconnected'},
      pendingUiRequest: null, subagents: [],
      session: {activity: [], archiveActivity: [], updatedMs: Date.now()},
    };
    state.agents = [agent];
    state.selectedId = agent.id;
    renderDetail({selectionChanged: true});
    const quit = el['session-menu'].querySelector('[data-session-action="quit"]');
    return {disabled: quit.disabled, hidden: quit.hidden};
  });
  assert.deepEqual(disconnectedShutdown, {disabled: false, hidden: false});
  assert.deepEqual(cspProgress, {
    todo: {value: 1, max: 4, style: null},
    usage: [
      {value: 0, max: 100, style: null},
      {value: 25, max: 100, style: null},
      {value: 100, max: 100, style: null},
    ],
  });

  const askRetryIds = await page.evaluate(async () => {
    const originalRequest = ompDeckApi.request;
    const pending = {
      id: 'ask-retry',
      kind: 'confirm',
      title: 'Retry confirmation',
      questionIndex: 0,
      questions: [{}],
    };
    const agent = {
      id: 'ask-retry-session',
      bridgeConnected: true,
      capabilities: {answerUiRequest: true},
      pendingUiRequest: pending,
    };
    state.agents = [agent];
    state.selectedId = agent.id;
    renderPendingRequest(agent);
    el['pending-request'].querySelector('[data-confirm="true"]').checked = true;
    const ids = [];
    try {
      ompDeckApi.request = async (path, options) => {
        if (!path.includes('/ui-requests/')) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              snapshotGeneration: nodeState('local').snapshotGeneration,
              sessions: [],
            }),
          };
        }
        ids.push(JSON.parse(options.body).requestId);
        if (ids.length === 1) throw new Error('response lost');
        return {ok: true, status: 202, json: async () => ({})};
      };
      await settlePendingRequest();
      await settlePendingRequest();
      return ids;
    } finally {
      ompDeckApi.request = originalRequest;
    }
  });
  assert.equal(askRetryIds[0], askRetryIds[1]);

  await verifySessionActions(page);

  const retryClassification = await page.evaluate(() => ({
    unknownServerError: ompDeckApi.shouldReuseRequestId(
      {ok: false, status: 502},
      {error: 'transport failed'},
    ),
    knownFailure: ompDeckApi.shouldReuseRequestId(
      {ok: false, status: 502},
      {status: 'failed', errorCode: 'command'},
    ),
    invalidRequest: ompDeckApi.shouldReuseRequestId(
      {ok: false, status: 400},
      {error: 'invalid request'},
    ),
  }));
  assert.deepEqual(retryClassification, {
    unknownServerError: true,
    knownFailure: false,
    invalidRequest: false,
  });

  const launchRetry = await page.evaluate(async () => {
    const originalRequest = ompDeckApi.request;
    const select = document.getElementById('project-select');
    const button = document.getElementById('project-launch');
    select.value = 'demo';
    select.dispatchEvent(new Event('change'));
    const requests = [];
    let rejectFirst;
    const waitUntil = async predicate => {
      const deadline = Date.now() + 1000;
      while (!predicate()) {
        if (Date.now() >= deadline) throw new Error('launcher retry timed out');
        await new Promise(resolve => setTimeout(resolve, 0));
      }
    };
    try {
      ompDeckApi.request = (path, options) => {
        requests.push({path, body: JSON.parse(options.body)});
        if (requests.length === 1) {
          return new Promise((resolve, reject) => {
            rejectFirst = reject;
          });
        }
        return Promise.resolve({
          ok: false,
          status: 502,
          json: async () => ({
            status: 'uncertain',
            errorCode: 'outcome_unknown',
          }),
        });
      };
      button.click();
      await waitUntil(() => Boolean(rejectFirst));
      rejectFirst(new Error('offline'));
      await waitUntil(() => !button.disabled);
      button.click();
      await waitUntil(() => requests.length === 2);
      return requests;
    } finally {
      ompDeckApi.request = originalRequest;
    }
  });
  assert.deepEqual(
    launchRetry.map(request => request.path),
    ['/api/projects/demo/sessions', '/api/projects/demo/sessions'],
  );
  assert.equal(launchRetry[0].body.requestId, launchRetry[1].body.requestId);

  const resetIdentity = await page.evaluate(async () => {
    const originalFetch = window.fetch;
    window.fetch = async (input, options) => (
      input === '/api/projects' ? new Response('', {status: 401}) : originalFetch(input, options)
    );
    try {
      const response = await ompDeckApi.request('/api/projects');
      return {
        status: response.status,
        machineName: ompDeckApi.machine('local')?.name,
        browserHost: location.host,
      };
    } finally {
      window.fetch = originalFetch;
    }
  });
  assert.deepEqual(resetIdentity, {
    status: 401,
    machineName: `localhost:${port}`,
    browserHost: `localhost:${port}`,
  });

  assert.equal(device.httpOnly, true);
  assert.equal(device.secure, true);
  assert.equal(device.sameSite, 'Strict');

  assert(device.expires > Date.now() / 1000);
  await browser.close();
  browser = null;
  browser = await puppeteer.launch({
    headless: true,
    args: ['--no-sandbox'],
    userDataDir: profile,
  });
  page = await browser.newPage();
  page.on('pageerror', recordPageError);
  await page.setViewport({width: 390, height: 844, deviceScaleFactor: 1});
  await page.goto(url, {waitUntil: 'domcontentloaded'});
  await page.waitForSelector('#app:not([hidden])', {visible: true});
  await page.waitForFunction(() => document.getElementById('project-select').options.length > 2);
  const relaunched = await page.evaluate(() => ({
    authenticated: ompDeckApi.authenticated(),
    appVisible: !document.getElementById('app').hidden,
    pairingVisible: !document.getElementById('auth-screen').hidden
      && !document.getElementById('pairing-fields').hidden,
    projects: [...document.getElementById('project-select').options].map(option => option.value),
  }));
  assert.equal(relaunched.authenticated, true);
  assert.equal(relaunched.appVisible, true);
  assert.equal(relaunched.pairingVisible, false);
  assert(relaunched.projects.includes('demo'));
  assert.deepEqual(pageErrors, [], 'browser recovery must not raise uncaught JavaScript errors');
});

test('renders quoted Markdown attributes without creating injected controls or handlers', async t => {
  const browser = await puppeteer.launch({headless: true, args: ['--no-sandbox']});
  t.after(() => browser.close());
  const page = await browser.newPage();
  await page.addScriptTag({path: path.join(ROOT, 'src/omp_deck/static/markdown.js')});
  const result = await page.evaluate(() => {
    const language = 'text" data-audit-injected="yes';
    const url = 'https://example.test/"onclick="alert';
    const host = document.createElement('div');
    host.innerHTML = renderMarkdown([
      `\`\`\`${language}`, 'const text = "<tag> & \\"quoted\\"";', '```',
      '', `[Open](${url})`, '', url,
    ].join('\n'));
    const code = host.querySelector('code');
    return {
      language: code.getAttribute('data-language'),
      code: code.textContent,
      injected: host.querySelector('[data-audit-injected], [onclick]') !== null,
      links: [...host.querySelectorAll('a')].map(link => ({
        href: link.getAttribute('href'),
        target: link.target,
        rel: link.rel,
      })),
    };
  });
  assert.equal(result.language, 'text" data-audit-injected="yes');
  assert.equal(result.code, 'const text = "<tag> & \\"quoted\\"";');
  assert.equal(result.injected, false);
  assert.deepEqual(result.links, Array(2).fill({
    href: 'https://example.test/"onclick="alert',
    target: '_blank',
    rel: 'noopener noreferrer',
  }));
});
