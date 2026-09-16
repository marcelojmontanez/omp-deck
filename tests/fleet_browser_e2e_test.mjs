import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {mkdtemp, mkdir, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {spawn, spawnSync} from 'node:child_process';
import net from 'node:net';
import http from 'node:http';
import {once} from 'node:events';
import test from 'node:test';
import puppeteer from 'puppeteer';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const PYTHON = process.env.PYTHON || (
  existsSync(path.join(ROOT, '.venv', 'bin', 'python'))
    ? path.join(ROOT, '.venv', 'bin', 'python') : 'python3'
);

async function unusedPort() {
  const probe = net.createServer();
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  return port;
}

async function configure(root, name, coordinator = false) {
  const base = path.join(root, name);
  for (const child of ['home', 'config', 'state', 'data', 'projects/demo', 'profile']) {
    await mkdir(path.join(base, child), {recursive: true, mode: 0o700});
  }
  const port = await unusedPort();
  const origin = `http://${coordinator ? 'localhost' : '127.0.0.1'}:${port}`;
  const config = path.join(base, 'config', 'config.json');
  const values = {host: '127.0.0.1', port, state_root: path.join(base, 'state')};
  if (coordinator) values.origin = origin;
  else Object.assign(values, {
    project_root: path.join(base, 'projects'),
    session_root: path.join(base, 'sessions'),
    socket_root: path.join(base, 'state', 'sockets'),
    omp_bin: process.execPath,
  });
  await writeFile(config, JSON.stringify(values), {mode: 0o600});
  const env = {
    PATH: process.env.PATH,
    ...(process.env.SystemRoot ? {SystemRoot: process.env.SystemRoot} : {}),
    HOME: path.join(base, 'home'),
    PYTHONPATH: [path.join(ROOT, 'src'), ROOT].join(path.delimiter),
    OMP_DECK_CONFIG: config,
    OMP_CONFIG_DIR: path.join(base, 'profile'),
    XDG_CONFIG_HOME: path.join(base, 'config'),
    XDG_DATA_HOME: path.join(base, 'data'),
    XDG_STATE_HOME: path.join(base, 'state'),
  };
  return {base, config, origin, env, child: null, stderr: ''};
}

function command(server, args) {
  const result = spawnSync(PYTHON, ['-m', 'omp_deck', ...args], {
    cwd: ROOT, env: server.env, encoding: 'utf8', timeout: 15_000,
  });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout;
}

async function start(server, args) {
  server.child = spawn(PYTHON, args, {cwd: ROOT, env: server.env, stdio: ['ignore', 'pipe', 'pipe']});
  server.child.stderr.on('data', chunk => {server.stderr = (server.stderr + chunk).slice(-32_768);});
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (server.child.exitCode !== null) throw new Error(server.stderr);
    try {
      if ((await fetch(`${server.origin}/healthz`)).ok) return;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(`Fixture did not become healthy: ${server.stderr}`);
}

async function stop(server) {
  if (!server.child || server.child.exitCode !== null) return;
  const exited = once(server.child, 'exit');
  server.child.kill('SIGTERM');
  const timer = setTimeout(() => server.child.kill('SIGKILL'), 5000);
  try {await exited;} finally {clearTimeout(timer);}
}

async function tailscaleProxy(server) {
  const backendPort = new URL(server.origin).port;
  server.identity = 'owner@example.test';
  const proxy = http.createServer((request, response) => {
    const headers = {...request.headers};
    for (const name of Object.keys(headers)) {
      if (name.startsWith('tailscale-user-')) delete headers[name];
    }
    if (server.identity !== null) headers['tailscale-user-login'] = server.identity;
    const upstream = http.request({
      hostname: '127.0.0.1', port: backendPort, path: request.url, method: request.method, headers,
    }, reply => {
      if (server.retainCookies === false) delete reply.headers['set-cookie'];
      response.writeHead(reply.statusCode, reply.headers);
      reply.pipe(response);
    });
    upstream.on('error', () => {
      if (!response.headersSent) response.writeHead(502);
      response.end();
    });
    response.on('close', () => upstream.destroy());
    request.pipe(upstream);
  });
  await new Promise(resolve => proxy.listen(0, '127.0.0.1', resolve));
  server.origin = `http://localhost:${proxy.address().port}`;
  const values = JSON.parse(await readFile(server.config, 'utf8'));
  if (values.origin) values.origin = server.origin;
  Object.assign(values, {auth_mode: 'tailscale', tailscale_user: server.identity});
  await writeFile(server.config, JSON.stringify(values), {mode: 0o600});
  return async () => {
    const closed = new Promise(resolve => proxy.close(resolve));
    proxy.closeAllConnections();
    await closed;
  };
}

async function pair(page, coordinator) {
  const printed = command(coordinator, ['coordinator', 'pair', '--config', coordinator.config]);
  const token = /Pairing code:\s*(\d{6})/.exec(printed)?.[1];
  assert(token, 'Coordinator did not provide a pairing code');
  const intercept = request => {
    if (['/launcher.js', '/api/auth/session'].includes(new URL(request.url()).pathname)) {
      setTimeout(() => request.continue(), 600);
    } else void request.continue();
  };
  await page.setRequestInterception(true);
  page.on('request', intercept);
  try {
    await page.goto(coordinator.origin, {waitUntil: 'domcontentloaded'});
    await page.waitForSelector('#pair-token', {visible: true});
    await page.type('#pair-token', token);
    await page.click('#pair-submit');
    await page.waitForFunction(() => (
      !document.getElementById('app').hidden
      && document.querySelectorAll('#agent-list [data-id]').length === 2
    ));
  } catch (error) {
    const surface = await page.$eval('body', body => body.innerText.slice(0, 2000));
    throw new Error(`Fleet pairing did not reach both machines: ${surface}`, {cause: error});
  } finally {
    page.off('request', intercept);
    await page.setRequestInterception(false);
  }
}

async function selectMachineSession(page, name) {
  const handle = await page.waitForFunction(label => (
    [...document.querySelectorAll('#agent-list [data-id]')].find(card => card.textContent.includes(label))
  ), {}, name);
  const id = await handle.evaluate(card => card.dataset.id);
  await page.locator(`#agent-list [data-id=${JSON.stringify(id)}]`).click();
  await handle.dispose();
  await page.waitForFunction(label => document.getElementById('activity').textContent.includes(`${label} reply`), {}, name);
  return id;
}

test('independent viewers route duplicate session IDs directly and survive one machine going offline', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'deck-fleet-browser-'));
  const coordinator = await configure(root, 'coordinator', true);
  const desktop = await configure(root, 'desktop');
  const mac = await configure(root, 'mac');
  let chrome;
  t.after(async () => {
    await chrome?.close();
    await Promise.all([coordinator, desktop, mac].map(stop));
    await rm(root, {recursive: true, force: true});
  });
  for (const [name, machine] of [['Desktop', desktop], ['Mac', mac]]) {
    const bundle = path.join(coordinator.base, `${name}.json`);
    command(coordinator, ['coordinator', 'machine', 'add', name, '--origin', machine.origin,
      '--out', bundle, '--config', coordinator.config]);
    command(machine, ['fleet', 'join', '--file', bundle, '--config', machine.config]);
  }
  await start(coordinator, ['-m', 'omp_deck', 'coordinator', 'serve', '--config', coordinator.config]);
  await Promise.all([
    start(desktop, ['-m', 'tests.fixtures.fleet_browser_server', 'Desktop']),
    start(mac, ['-m', 'tests.fixtures.fleet_browser_server', 'Mac']),
  ]);
  chrome = await puppeteer.launch({headless: true, args: ['--no-sandbox']});
  const viewers = await Promise.all([chrome.createBrowserContext(), chrome.createBrowserContext()]);
  const [phone, computer] = await Promise.all(viewers.map(context => context.newPage()));
  await phone.setViewport({width: 390, height: 844});
  await computer.setViewport({width: 1440, height: 900});
  const nodeRequests = [];
  const coordinatorRequests = [];
  const pageErrors = [];
  for (const page of [phone, computer]) {
    page.on('pageerror', error => pageErrors.push(error.message));
    page.on('request', request => {
      const url = new URL(request.url());
      if (!url.pathname.startsWith('/api/')) return;
      if (url.origin === coordinator.origin) coordinatorRequests.push(url.pathname);
      else if ([desktop.origin, mac.origin].includes(url.origin)) {
        nodeRequests.push({origin: url.origin, path: url.pathname, method: request.method(),
          authorized: Boolean(request.headers().authorization), cookie: request.headers().cookie});
      }
    });
    await pair(page, coordinator);
  }
  for (const page of [phone, computer]) {
    assert.deepEqual(await page.evaluate(() => state.agents.map(agent => ({
      lifecycle: agent.lifecycle,
      historyError: agent.historyError,
    }))), [
      {lifecycle: 'ongoing', historyError: null},
      {lifecycle: 'ongoing', historyError: null},
    ]);
  }
  const desktopId = await selectMachineSession(phone, 'Desktop');
  const coldDeepLink = await phone.evaluate(selectedId => {
    const requested = 'cold-machine:session-before-snapshot';
    const machine = ompDeckApi.machines()[0];
    const node = nodeState(machine.id);
    node.hasSnapshot = false;
    ompDeckApi.setMachineStatus(machine.id, 'live');
    state.pendingSelection = requested;
    const url = new URL(location.href);
    url.searchParams.set('session', requested);
    history.replaceState({view: 'chat', session: requested}, '', url);
    renderFleet();
    const result = {
      pendingSelection: state.pendingSelection,
      urlSelection: new URL(location.href).searchParams.get('session'),
    };
    state.pendingSelection = null;
    selectAgent(selectedId, {pushHistory: false});
    return result;
  }, desktopId);
  assert.deepEqual(coldDeepLink, {
    pendingSelection: 'cold-machine:session-before-snapshot',
    urlSelection: 'cold-machine:session-before-snapshot',
  });
  await phone.type('#message', 'Keep this desktop draft');
  const attachment = await phone.$('#attachment-input');
  const image = path.join(root, 'draft.png');
  await writeFile(image, Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM1sAAAAASUVORK5CYII=', 'base64'));
  await attachment.uploadFile(image);
  await phone.waitForSelector('#attachments img');
  await phone.goBack();
  const macId = await selectMachineSession(phone, 'Mac');
  assert.notEqual(macId, desktopId);
  assert.equal(await phone.$eval('#message', input => input.value), '');
  await phone.waitForFunction(() => [...document.querySelectorAll('#activity img')].some(image => (
    image.src.startsWith('blob:') && image.complete && image.naturalWidth === 1
  )));
  await selectMachineSession(computer, 'Mac');
  await stop(desktop);
  await phone.waitForFunction(() => /offline|unavailable|reconnecting/i.test(
    document.querySelector('#agent-list [data-id]')?.parentElement.textContent || ''
  ));
  await phone.type('#message', 'Mac remains reachable');
  await phone.click('#send');
  await computer.waitForFunction(() => document.getElementById('activity').textContent.includes('Mac remains reachable'));
  assert.equal(await computer.$eval('#auth-screen', screen => screen.hidden), true);
  await phone.goBack();
  await selectMachineSession(phone, 'Desktop');
  assert.equal(await phone.$eval('#message', input => input.value), 'Keep this desktop draft');
  assert.equal(await phone.$$eval('#attachments img', images => images.length), 1);
  await start(desktop, ['-m', 'tests.fixtures.fleet_browser_server', 'Desktop']);
  await phone.waitForFunction(() => !document.getElementById('send').disabled, {timeout: 30_000});
  for (const page of [phone, computer]) {
    assert.deepEqual(await page.evaluate(() => ({
      local: localStorage.length, session: sessionStorage.length,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    })), {local: 0, session: 0, overflow: false});
  }
  assert(nodeRequests.some(request => request.origin === mac.origin && request.path === '/api/sessions/same-local-session/messages'));
  assert(nodeRequests.filter(request => request.method !== 'OPTIONS' && request.path !== '/api/client-config')
    .every(request => request.authorized && !request.cookie));
  assert(!coordinatorRequests.some(request => /^\/api\/(sessions|events|projects|blob|usage)/.test(request)));
  await phone.evaluate(() => {
    const fetchRequest = window.fetch;
    window.fetch = async (input, options) => {
      const response = await fetchRequest(input, options);
      if (input === '/api/fleet/access') {
        return new Promise(resolve => {
          window.releaseRenewal = () => {
            window.fetch = fetchRequest;
            resolve(response);
          };
        });
      }
      return response;
    };
    window.delayedRenewal = ompDeckApi.renewAccess().catch(() => {});
  });
  await phone.waitForFunction(() => typeof window.releaseRenewal === 'function');
  await phone.click('#overflow-button');
  await phone.click('#session-menu [data-deck-settings]');
  await phone.click('[data-session-action="logout"]');
  await phone.waitForFunction(() => !document.getElementById('auth-screen').hidden);
  const postLogout = await phone.evaluate(async machineId => {
    window.releaseRenewal();
    await window.delayedRenewal;
    let directStatus = null;
    try {
      directStatus = (await ompDeckApi.request('/api/sessions', {machineId})).status;
    } catch {}
    return {machines: ompDeckApi.machines(), directStatus};
  }, await computer.evaluate(() => state.selectedMachineId));
  assert.deepEqual(postLogout, {machines: [], directStatus: null});
  await computer.type('#message', 'Second device stays authorized');
  await computer.click('#send');
  await computer.waitForFunction(() => document.getElementById('activity').textContent.includes('Second device stays authorized'));
  assert.deepEqual(pageErrors, [], 'fleet lifecycles must not raise uncaught JavaScript errors');
});

test('Tailscale sign-in protects coordinator and node cookies without weakening enrolled tickets', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'deck-tailscale-browser-'));
  const coordinator = await configure(root, 'coordinator', true);
  const node = await configure(root, 'node');
  const proxies = [];
  let chrome;
  t.after(async () => {
    await chrome?.close();
    await Promise.all(proxies.map(close => close()));
    await Promise.all([coordinator, node].map(stop));
    await rm(root, {recursive: true, force: true});
  });
  proxies.push(await tailscaleProxy(coordinator), await tailscaleProxy(node));
  const bundle = path.join(coordinator.base, 'node.json');
  command(coordinator, ['coordinator', 'machine', 'add', 'Node', '--origin', node.origin,
    '--out', bundle, '--config', coordinator.config]);
  command(node, ['fleet', 'join', '--file', bundle, '--config', node.config]);
  await start(coordinator, ['-m', 'omp_deck', 'coordinator', 'serve', '--config', coordinator.config]);
  await start(node, ['-m', 'tests.fixtures.fleet_browser_server', 'Node']);
  chrome = await puppeteer.launch({headless: true, args: ['--no-sandbox']});
  const [fleetContext, localContext] = await Promise.all([
    chrome.createBrowserContext(), chrome.createBrowserContext(),
  ]);
  const [fleet, local] = await Promise.all([fleetContext.newPage(), localContext.newPage()]);
  await fleet.setViewport({width: 390, height: 844});
  await local.setViewport({width: 1440, height: 900});
  const errors = [];
  const signIns = [];
  const directRequests = [];
  for (const page of [fleet, local]) {
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.pathname === '/api/auth/tailscale') signIns.push(url.origin);
      if (page === fleet && url.origin === node.origin && url.pathname.startsWith('/api/')) {
        directRequests.push({method: request.method(), headers: request.headers()});
      }
    });
    await page.goto(page === fleet ? coordinator.origin : node.origin, {waitUntil: 'domcontentloaded'});
    await page.waitForFunction(() => (
      document.getElementById('auth-screen')?.hidden
      && document.querySelectorAll('#agent-list [data-id]').length === 1
    ));
    assert.equal(await page.$eval('#pairing-fields', fields => fields.hidden), true);
    assert.equal(await page.evaluate(() => (
      document.documentElement.scrollWidth > document.documentElement.clientWidth
    )), false);
  }
  assert.deepEqual(signIns, [coordinator.origin, node.origin]);
  await fleet.reload({waitUntil: 'domcontentloaded'});
  await fleet.waitForFunction(() => (
    document.getElementById('auth-screen')?.hidden
    && document.querySelectorAll('#agent-list [data-id]').length === 1
  ));
  assert.deepEqual(signIns, [coordinator.origin, node.origin], 'valid cookies must survive navigation');
  assert(directRequests.some(request => request.method !== 'OPTIONS' && request.headers.authorization));
  assert(directRequests.every(request => !request.headers.cookie), 'fleet access must not send ambient cookies');

  coordinator.retainCookies = false;
  const blockedContext = await chrome.createBrowserContext();
  const blocked = await blockedContext.newPage();
  let blockedSignIns = 0;
  blocked.on('request', request => {
    if (new URL(request.url()).pathname === '/api/auth/tailscale') blockedSignIns += 1;
  });
  await blocked.goto(coordinator.origin, {waitUntil: 'domcontentloaded'});
  await blocked.waitForSelector('#tailscale-sign-in:not([hidden])');
  assert.equal(await blocked.$eval('#app', app => app.hidden), true);
  assert.equal(await blocked.$eval('#pairing-fields', fields => fields.hidden), true);
  await blocked.evaluate(() => {
    window.dispatchEvent(new Event('online'));
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await blocked.waitForNetworkIdle({idleTime: 300});
  assert.equal(blockedSignIns, 1, 'missing cookie retention must not cause an automatic sign-in loop');
  await blockedContext.close();
  coordinator.retainCookies = true;

  node.identity = null;
  assert.equal(await local.evaluate(async () => (await fetch('/api/sessions')).status), 401);
  assert.equal(await fleet.evaluate(async () => (
    await ompDeckApi.request('/api/sessions', {machineId: ompDeckApi.machines()[0].id})
  ).status), 200, 'enrolled tickets remain an independent authority');
  node.identity = 'owner@example.test';
  await fleet.evaluate(() => {
    const originalFetch = window.fetch;
    window.fetch = async (input, options) => {
      const response = await originalFetch(input, options);
      if (input === '/api/auth/tailscale') {
        return new Promise(resolve => {
          window.releaseSignIn = () => {
            window.fetch = originalFetch;
            resolve(response);
          };
        });
      }
      return response;
    };
    window.delayedSignIn = ompDeckApi.tailscaleLogin().then(
      () => 'accepted', error => error.name
    );
  });
  await fleet.waitForFunction(() => typeof window.releaseSignIn === 'function');
  await fleet.click('.dashboard-actions [data-deck-settings]');
  await fleet.click('[data-session-action="logout"]');
  await fleet.waitForFunction(() => !document.getElementById('auth-screen').hidden);
  assert.equal(await fleet.evaluate(async () => {
    window.releaseSignIn();
    return await window.delayedSignIn;
  }), 'AbortError', 'a delayed sign-in must not overwrite newer logout state');
  assert.equal(await fleet.$eval('#pairing-fields', fields => fields.hidden), true);
  assert.equal(await fleet.evaluate(async () => (
    await (await fetch('/api/auth/session')).json()
  ).authenticated), false, 'logout must not immediately auto-sign-in');
  await fleet.click('#tailscale-sign-in');
  await fleet.waitForFunction(() => (
    document.getElementById('auth-screen')?.hidden
    && document.querySelectorAll('#agent-list [data-id]').length === 1
  ));

  coordinator.identity = 'other@example.test';
  assert.equal(await fleet.evaluate(async () => (await fetch('/api/fleet')).status), 401);
  const spoofed = await fetch(`${coordinator.origin}/api/auth/tailscale`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', Origin: coordinator.origin,
      'Tailscale-User-Login': 'owner@example.test'},
    body: '{}',
  });
  assert.equal(spoofed.status, 403, 'the proxy must overwrite supplied identity headers');
  assert.equal(spoofed.headers.get('set-cookie'), null);
  await fleet.reload({waitUntil: 'domcontentloaded'});
  await fleet.waitForFunction(() => (
    !document.getElementById('auth-screen').hidden
    && !document.getElementById('auth-error').hidden
  ));
  assert.equal(await fleet.$eval('#pairing-fields', fields => fields.hidden), true);
  assert.equal(await fleet.$eval('#app', app => app.hidden), true);
  assert.deepEqual(errors, [], 'automatic login and denial must not raise page errors');
});
