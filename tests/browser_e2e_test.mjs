import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {mkdtemp, mkdir, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {spawn, spawnSync} from 'node:child_process';
import net from 'node:net';
import test from 'node:test';
import puppeteer from 'puppeteer';

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
  }), {mode: 0o600});
  const environment = {
    ...process.env,
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
  const browser = await puppeteer.launch({headless: true, args: ['--no-sandbox']});
  t.after(() => browser.close());
  const page = await browser.newPage();
  await page.setViewport({width: 390, height: 844, deviceScaleFactor: 1});
  await page.goto(url, {waitUntil: 'domcontentloaded'});
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
  const cookies = await page.cookies();
  const device = cookies.find(cookie => cookie.name === 'omp_deck_device');
  assert(device, `secure device cookie missing; service stderr: ${serviceError}`);
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
  await page.waitForFunction(() => [...state.nodes.values()].every(node => !node.refreshPromise));
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

    const largeText = `supported-snapshot-start ${'x'.repeat(224 * 1024)} supported-snapshot-end`;
    const snapshotData = JSON.stringify({
      id: baseGeneration + 3,
      type: 'snapshot',
      payload: {
        snapshotGeneration: baseGeneration + 3,
        sessions: [rawSession('Large transcript', [{
          id: 'large-message',
          role: 'assistant',
          content: [{type: 'text', text: largeText}],
          createdAt: '2026-08-20T12:34:56.789Z',
        }])],
      },
    });
    const frame = `id: browser-regression:${baseGeneration + 3}\nevent: fleet\ndata: ${snapshotData}\n\n`;
    const stream = {
      controller: new AbortController(),
      failures: 4,
      lastEventId: '',
    };
    await consumeSse(new Response(new Blob([frame]).stream(), {
      status: 200,
      headers: {'Content-Type': 'text/event-stream'},
    }), machineId, stream);
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
    };
  });
  assert.match(normalizedEventOrdering.firstVisible, /First normalized update/);
  assert.match(normalizedEventOrdering.secondVisible, /Second normalized update/);
  assert.match(normalizedEventOrdering.afterStale, /Second normalized update/);
  assert.match(normalizedEventOrdering.afterGap, /Second normalized update/);
  assert.match(normalizedEventOrdering.largeTitleVisible, /Large transcript/);
  assert.equal(normalizedEventOrdering.largeTranscriptVisible, true);

  const lifecycleDashboard = await page.evaluate(async () => {
    const makeAgent = (id, lifecycle, executionState, overrides = {}) => ({
      id, lifecycle, executionState, status: executionState,
      project: 'deck', label: `${id} conversation`, machineName: 'Workstation',
      machineId: 'local', nodeAvailable: true, bridgeConnected: true,
      inboxState: 'read', capabilities: {sendMessage: true, attachments: true},
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
    ompDeckApi.request = async path => {
      composerPath = path;
      return {ok: true, status: 202, json: async () => ({})};
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
    state.sessionScope = 'history';
    renderSummary();
    renderList();
    const history = [...el['agent-list'].querySelectorAll('.agent-card')].map(card => card.dataset.id);
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
      ongoing, history, counts, ongoingReturnCount, historyFailure, authoritativeHistory,
      historyDetail, restoredDraft, selectedAfterReturn: state.selectedId,
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
      capabilities: {sendMessage: true, followUp: true},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      subagents: [],
      session: {activity: [], todo: '', updatedMs: Date.now()},
    };
    state.agents = [agent];
    state.selectedId = agent.id;
    composer.select(agent.id);
    const response = {ok: true, status: 202, json: async () => ({})};
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
      ompDeckApi.request = path => {
        routes.push(path);
        if (routes.length === 1) {
          return new Promise(resolve => {
            releaseFirst = () => resolve(response);
          });
        }
        return Promise.resolve(response);
      };
      submit('first');
      await waitUntil(() => Boolean(releaseFirst), 'first request');
      submit('second');
      agent.status = 'working';
      agent.executionState = 'working';
      releaseFirst();
      await waitUntil(() => routes.length === 2, 'follow-up request');

      agent.status = 'idle';
      agent.executionState = 'idle';
      const failedRequests = [];
      let rejectFirst;
      ompDeckApi.request = (path, options) => {
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
        retryRequests.push({path, body: JSON.parse(options.body)});
        return Promise.resolve(response);
      };
      submit(restoredDraft);
      await waitUntil(() => retryRequests.length === 2, 'safe request retry');

      return {
        routes,
        failedRequests,
        retryRequests,
        restoredDraft,
      };
    } finally {
      ompDeckApi.request = originalRequest;
    }
  });
  assert.deepEqual(queueBehavior.routes, [
    '/api/sessions/queue-session/messages',
    '/api/sessions/queue-session/follow-ups',
  ]);
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

  const echoReconciliation = await page.evaluate(async () => {
    const originalRequest = ompDeckApi.request;
    const previousAgents = state.agents;
    const previousSelectedId = state.selectedId;
    const agent = {
      id: 'echo-session', project: 'demo', label: 'Echo session',
      lifecycle: 'ongoing',
      status: 'idle', executionState: 'idle', inboxState: 'read',
      bridgeConnected: true, capabilities: {sendMessage: true},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      pendingUiRequest: null, subagents: [],
      session: {
        activity: [{
          id: 'old-identical', kind: 'user', detail: 'duplicate',
          time: '2099-01-01T00:00:00Z',
        }],
        todo: '', updatedMs: Date.now(),
      },
    };
    state.agents = [agent];
    state.selectedId = agent.id;
    composer.select(agent.id);
    const submit = message => {
      el.message.value = message;
      el.composer.requestSubmit();
    };
    try {
      ompDeckApi.request = async () => (
        {ok: true, status: 202, json: async () => ({})}
      );
      submit('duplicate');
      submit('duplicate');
      await new Promise(resolve => setTimeout(resolve, 0));
      composer.reconcile(agent);
      const afterOld = composer.pendingFor(agent.id).length;
      agent.session.activity.push({
        id: 'new-one', kind: 'user', detail: 'duplicate',
        time: '1999-01-01T00:00:00Z',
      });
      composer.reconcile(agent);
      const afterOne = composer.pendingFor(agent.id).length;
      composer.reconcile(agent);
      const afterRepeatedSnapshot = composer.pendingFor(agent.id).length;
      agent.session.activity.push({
        id: 'new-two', kind: 'user', detail: 'duplicate',
        time: '1998-01-01T00:00:00Z',
      });
      composer.reconcile(agent);
      const afterTwo = composer.pendingFor(agent.id).length;

      submit('lost after truncation');
      await new Promise(resolve => setTimeout(resolve, 0));
      agent.session.activity = Array.from({length: 500}, (_, index) => ({
        id: index === 0 ? 'old-identical' : `replacement-${index}`,
        kind: 'user',
        detail: index === 0 ? 'lost after truncation' : `post-submit-${index}`,
        time: '1997-01-01T00:00:00Z',
      }));
      composer.reconcile(agent);
      const afterTruncationWithoutEcho = composer.pendingFor(agent.id).length;
      agent.session.activity.shift();
      agent.session.activity.push({
        id: 'delivered-after-truncation',
        kind: 'user',
        detail: 'lost after truncation',
        time: '1996-01-01T00:00:00Z',
      });
      composer.reconcile(agent);
      return {
        afterOld,
        afterOne,
        afterRepeatedSnapshot,
        afterTwo,
        afterTruncationWithoutEcho,
        afterDeliveredEcho: composer.pendingFor(agent.id).length,
      };
    } finally {
      ompDeckApi.request = originalRequest;
      state.agents = previousAgents;
      state.selectedId = previousSelectedId;
      composer.select(previousSelectedId);
    }
  });
  assert.deepEqual(echoReconciliation, {
    afterOld: 2,
    afterOne: 1,
    afterRepeatedSnapshot: 1,
    afterTwo: 0,
    afterTruncationWithoutEcho: 1,
    afterDeliveredEcho: 0,
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
    const response = {ok: true, status: 202, json: async () => ({})};
    const waitUntil = async predicate => {
      const deadline = Date.now() + 1000;
      while (!predicate()) {
        if (Date.now() >= deadline) throw new Error('composer clear timed out');
        await new Promise(resolve => setTimeout(resolve, 0));
      }
    };
    try {
      ompDeckApi.request = path => {
        routes.push(path);
        if (routes.length === 1) {
          return new Promise(resolve => {
            releaseFirst = () => resolve(response);
          });
        }
        return Promise.resolve(response);
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
    const input = document.getElementById('attachment-input');
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
    const input = document.getElementById('attachment-input');
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
      const largeBatch = prefix => [0, 1].map(index => new File(
        [new Uint8Array(200 * 1024)],
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
    let authoritative = 'medium';
    const rawSession = () => ({
      id: 'thinking-session', title: 'Thinking session',
      project: {name: 'demo', path: '/demo'}, state: 'idle', unread: false,
      lifecycle: 'ongoing',
      transport: {kind: 'rpc', status: 'connected'},
      capabilities: {setThinking: true}, thinking: authoritative,
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
      await new Promise(resolve => setTimeout(resolve, 0));
      return {simultaneous, display: document.querySelector('[data-thinking-level]').textContent};
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
      authoritative = 'max';
      await refresh('local');
      const external = document.querySelector('[data-thinking-level]').textContent;
      authoritative = 'medium';
      await refresh('local');
      const failed = await cycleTwice(false);
      return {accepted, external, failed};
    } finally {
      ompDeckApi.request = originalRequest;
    }
  });
  assert.deepEqual(thinkingChanges, {
    accepted: {simultaneous: 1, display: 'thinking: xhigh'},
    external: 'thinking: max',
    failed: {simultaneous: 1, display: 'thinking: medium'},
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
    return {disabled: quit.disabled, title: quit.title};
  });
  assert.deepEqual(disconnectedShutdown, {disabled: false, title: ''});
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

  const controlRetryIds = await page.evaluate(async () => {
    const originalRequest = ompDeckApi.request;
    const originalConfirm = globalThis.confirm;
    const agent = {
      id: 'control-retry-session',
      project: 'demo',
      label: 'Control retry',
      lifecycle: 'ongoing',
      bridgeConnected: true,
      executionState: 'idle',
      inboxState: 'read',
      capabilities: {compact: true},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      pendingUiRequest: null,
      subagents: [],
      session: {activity: [], archiveActivity: [], updatedMs: Date.now()},
    };
    state.agents = [agent];
    state.selectedId = agent.id;
    renderDetail({selectionChanged: true});
    const button = el['session-menu'].querySelector('[data-session-action="clear"]');
    const ids = [];
    const waitForAttempt = async count => {
      const deadline = Date.now() + 1000;
      while (ids.length < count) {
        if (Date.now() >= deadline) throw new Error('control retry timed out');
        await new Promise(resolve => setTimeout(resolve, 0));
      }
      await new Promise(resolve => setTimeout(resolve, 0));
    };
    try {
      globalThis.confirm = () => true;
      ompDeckApi.request = async (_path, options) => {
        ids.push(JSON.parse(options.body).requestId);
        if (ids.length === 1) {
          return {
            ok: false,
            status: 502,
            json: async () => ({error: 'response lost'}),
          };
        }
        return {ok: true, status: 202, json: async () => ({})};
      };
      button.click();
      await waitForAttempt(1);
      button.click();
      await waitForAttempt(2);
      return ids;
    } finally {
      ompDeckApi.request = originalRequest;
      globalThis.confirm = originalConfirm;
    }
  });
  assert.equal(controlRetryIds[0], controlRetryIds[1]);

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

  assert.equal(device.httpOnly, true);
  assert.equal(device.secure, true);
  assert.equal(device.sameSite, 'Strict');
});
