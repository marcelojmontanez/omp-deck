import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {mkdtemp, mkdir, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {spawn, spawnSync} from 'node:child_process';
import net from 'node:net';
import test from 'node:test';
import puppeteer from 'puppeteer';

const ROOT = path.dirname(new URL(import.meta.url).pathname);
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
  await writeFile(config, JSON.stringify({
    host: '127.0.0.1',
    port,
    project_root: projects,
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
    const cookiesEnabled = navigator.cookieEnabled;
    return {
      projectsStatus: projectsResponse.status,
      projectValues: [...document.getElementById('project-select').options].map(option => option.value),
      launchState: document.getElementById('launch-state').textContent,
      localStorageLength: localStorage.length,
      sessionStorageLength: sessionStorage.length,
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      cookiesEnabled,
    };
  });
  assert.equal(result.projectsStatus, 200);
  assert(result.projectValues.includes('demo'));
  assert.equal(result.launchState, '');
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
    state.agents = [{
      id: 'session-1',
      project: 'demo',
      label: 'Demo session',
      status: 'idle',
      executionState: 'idle',
      inboxState: 'unread',
      bridgeConnected: true,
      capabilities: {},
      transport: {kind: 'rpc', status: 'connected', label: 'RPC'},
      subagents: [
        {id: 'working-child', label: 'Worker', executionState: 'working', activitySummary: 'Testing'},
        {id: 'idle-child', label: 'Sleeper', executionState: 'idle', activitySummary: 'Done'},
      ],
      session: {activity: [], todo: '', updatedMs: Date.now()},
    }];
    renderSummary();
    renderList();
    showToast('Started OMP', 'success');
    const card = document.querySelector('[data-id=\"session-1\"]');
    const unreadCount = document.getElementById('unread-count').textContent;
    card.click();
    return {
      tableHeaders: [...markdown.querySelectorAll('th')].map(node => node.textContent),
      tableCells: [...markdown.querySelectorAll('td')].map(node => node.textContent),
      toastClass: document.getElementById('toast').className,
      unreadCount,
      cardText: card.textContent,
      selectedId: state.selectedId,
    };
  });
  assert.deepEqual(rendering.tableHeaders, ['A', 'B']);
  assert.deepEqual(rendering.tableCells, ['one', 'two']);
  assert.match(rendering.toastClass, /success/);
  assert.equal(rendering.unreadCount, '1');
  assert.match(rendering.cardText, /Worker/);
  assert.doesNotMatch(rendering.cardText, /Sleeper/);
  assert.equal(rendering.selectedId, 'session-1');
  await page.waitForFunction(() => !state.refreshing);
  const recovery = await page.evaluate(async () => {
    state.eventSource?.close();
    const originalFetch = window.fetch;
    const baseGeneration = state.snapshotGeneration + 10;
    let calls = 0;
    let releaseFirst;
    const response = payload => ({
      ok: true,
      status: 200,
      json: async () => payload,
    });
    const rawSession = title => ({
      id: 'recovery-session',
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
      const staleRequestGeneration = state.snapshotGeneration;
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
        snapshotGeneration: state.snapshotGeneration,
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

  const queueBehavior = await page.evaluate(async () => {
    const originalRequest = ompDeckApi.request;
    const agent = {
      id: 'queue-session',
      project: 'demo',
      label: 'Queue session',
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
              snapshotGeneration: state.snapshotGeneration,
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
