import assert from 'node:assert/strict';
import {mkdtemp, readFile, readdir, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import net from 'node:net';
import readline from 'node:readline';
import test from 'node:test';
import {pathToFileURL} from 'node:url';

const PLUGIN = pathToFileURL(
  path.join(import.meta.dirname, '../src/omp_deck/plugin/omp-deck.ts'),
);

async function connect(socketPath) {
  const socket = net.createConnection(socketPath);
  await new Promise((resolve, reject) => {
    socket.once('connect', resolve);
    socket.once('error', reject);
  });
  return socket;
}

test('plugin events preserve sequence and synchronize active history', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'od-'));
  const stateVariable = process.platform === 'win32' ? 'LOCALAPPDATA' : 'XDG_STATE_HOME';
  const previousStateHome = process.env[stateVariable];
  process.env[stateVariable] = root;
  const handlers = new Map();
  const timers = new Set();
  const warnings = [];
  const pi = {
    pi: {VERSION: 'test'},
    logger: {warn(message, detail) { warnings.push([message, detail]); }},
    on(name, handler) { handlers.set(name, handler); },
    async sendUserMessage() {},
  };
  let branchMessages = [
    {role: 'user', content: 'original question', timestamp: 1},
    {role: 'assistant', content: 'original answer', timestamp: 2},
  ];
  const context = {
    cwd: '/work/test',
    model: {provider: 'test', id: 'model'},
    sessionManager: {
      getSessionId: () => 'plugin-sequence-test',
      getSessionFile: () => null,
      getSessionName: () => 'Sequence test',
      getBranch: () => branchMessages.map((message, index) => ({
        type: 'message',
        id: `entry-${index}`,
        message,
      })),
    },
    isIdle: () => true,
    hasPendingMessages: () => false,
    getAsyncJobSnapshot: () => [],
    setTimeout(callback, delay) {
      const timer = setTimeout(callback, delay);
      timers.add(timer);
      return timer;
    },
    setInterval(callback, delay) {
      const timer = setInterval(callback, delay);
      timers.add(timer);
      return timer;
    },
    clearTimer(timer) {
      clearTimeout(timer);
      clearInterval(timer);
      timers.delete(timer);
    },
    abort() {},
    shutdown() {},
  };
  let socket;
  try {
    const {default: register} = await import(`${PLUGIN.href}?test=${Date.now()}`);
    register(pi);
    await handlers.get('session_start')({}, context);
    const socketDirectory = process.platform === 'win32'
      ? path.join(root, 'OMP Deck', 'state', 'sockets')
      : path.join(root, 'omp-deck', 'sockets');
    const suffix = process.platform === 'win32' ? '.pipe.json' : '.sock';
    const socketName = (await readdir(socketDirectory)).find(name => name.endsWith(suffix));
    assert(socketName, `plugin socket was not created: ${JSON.stringify(warnings)}`);
    const metadata = process.platform === 'win32'
      ? JSON.parse(await readFile(path.join(socketDirectory, socketName), 'utf8'))
      : null;
    const endpoint = metadata?.endpoint || path.join(socketDirectory, socketName);
    if (metadata) {
      const rejected = await connect(endpoint);
      rejected.write(`${JSON.stringify({
        protocol: 'omp-deck-fallback-v1',
        version: 1,
        type: 'connect',
        token: '0'.repeat(64),
      })}\n`);
      await new Promise(resolve => rejected.once('close', resolve));
    }
    socket = await connect(endpoint);
    if (metadata) {
      socket.write(`${JSON.stringify({
        protocol: 'omp-deck-fallback-v1',
        version: 1,
        type: 'connect',
        token: metadata.authToken,
      })}\n`);
    }
    const lines = readline.createInterface({input: socket, crlfDelay: Infinity});
    const frames = lines[Symbol.asyncIterator]();
    const registration = JSON.parse((await frames.next()).value);
    const snapshot = JSON.parse((await frames.next()).value);
    assert.equal(registration.type, 'registration');
    assert.equal(snapshot.kind, 'snapshot');
    assert.equal(snapshot.sequence, 1);

    handlers.get('message_end')({message: {role: 'assistant', content: 'x'.repeat(1024 * 1024)}});
    const degraded = JSON.parse((await frames.next()).value);
    assert.equal(degraded.kind, 'degraded');
    assert.equal(degraded.data.droppedKind, 'message');
    assert.equal(degraded.sequence, 2);

    handlers.get('agent_start')({turn: 1});
    const following = JSON.parse((await frames.next()).value);
    assert.equal(following.kind, 'lifecycle');
    assert.equal(following.sequence, 3);

    socket.write(`${JSON.stringify({
      protocol: 'omp-deck-fallback-v1',
      type: 'request',
      id: 'history-1',
      action: 'syncHistory',
    })}\n`);
    const initialHistory = JSON.parse((await frames.next()).value);
    assert.equal(initialHistory.kind, 'history');
    assert.equal(initialHistory.sequence, 4);
    assert.equal(initialHistory.data.complete, true);
    assert.deepEqual(initialHistory.data.messages.map(message => message.content), [
      'original question',
      'original answer',
    ]);
    const historyResponse = JSON.parse((await frames.next()).value);
    assert.equal(historyResponse.type, 'response');
    assert.equal(historyResponse.ok, true);

    branchMessages = [
      {role: 'user', content: 'selected branch question', timestamp: 3},
      {role: 'assistant', content: 'selected branch answer', timestamp: 4},
    ];
    await handlers.get('session_tree')({}, context);
    const treeHistory = JSON.parse((await frames.next()).value);
    assert.equal(treeHistory.kind, 'history');
    assert.equal(treeHistory.sequence, 5);
    assert.equal(treeHistory.data.complete, true);
    assert.deepEqual(treeHistory.data.messages.map(message => message.content), [
      'selected branch question',
      'selected branch answer',
    ]);
    lines.close();
  } finally {
    socket?.destroy();
    if (handlers.has('session_shutdown')) await handlers.get('session_shutdown')();
    for (const timer of timers) {
      clearTimeout(timer);
      clearInterval(timer);
    }
    if (previousStateHome === undefined) delete process.env[stateVariable];
    else process.env[stateVariable] = previousStateHome;
    await rm(root, {recursive: true, force: true});
  }
});
