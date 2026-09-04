import assert from 'node:assert/strict';
import {mkdtemp, readdir, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import net from 'node:net';
import readline from 'node:readline';
import test from 'node:test';
import {pathToFileURL} from 'node:url';

const PLUGIN = pathToFileURL(
  path.join(import.meta.dirname, 'src/omp_deck/plugin/omp-deck.ts'),
);

async function connect(socketPath) {
  const socket = net.createConnection(socketPath);
  await new Promise((resolve, reject) => {
    socket.once('connect', resolve);
    socket.once('error', reject);
  });
  return socket;
}

test('oversized plugin events preserve sequence continuity', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'od-'));
  const previousStateHome = process.env.XDG_STATE_HOME;
  process.env.XDG_STATE_HOME = root;
  const handlers = new Map();
  const timers = new Set();
  const warnings = [];
  const pi = {
    pi: {VERSION: 'test'},
    logger: {warn(message, detail) { warnings.push([message, detail]); }},
    on(name, handler) { handlers.set(name, handler); },
    async sendUserMessage() {},
  };
  const context = {
    cwd: '/work/test',
    model: {provider: 'test', id: 'model'},
    sessionManager: {
      getSessionId: () => 'plugin-sequence-test',
      getSessionFile: () => null,
      getSessionName: () => 'Sequence test',
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
    const socketDirectory = path.join(root, 'omp-deck', 'sockets');
    const socketName = (await readdir(socketDirectory)).find(name => name.endsWith('.sock'));
    assert(socketName, `plugin socket was not created: ${JSON.stringify(warnings)}`);
    socket = await connect(path.join(socketDirectory, socketName));
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
    lines.close();
  } finally {
    socket?.destroy();
    if (handlers.has('session_shutdown')) await handlers.get('session_shutdown')();
    for (const timer of timers) {
      clearTimeout(timer);
      clearInterval(timer);
    }
    if (previousStateHome === undefined) delete process.env.XDG_STATE_HOME;
    else process.env.XDG_STATE_HOME = previousStateHome;
    await rm(root, {recursive: true, force: true});
  }
});
