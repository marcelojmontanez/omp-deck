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
  }));
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
  assert.equal(device.httpOnly, true);
  assert.equal(device.secure, true);
  assert.equal(device.sameSite, 'Strict');
});
