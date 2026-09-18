import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdir, mkdtemp, readFile, readdir, rm, writeFile} from 'node:fs/promises';
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
  const root = await mkdtemp(path.join(process.platform === 'darwin' ? '/tmp' : tmpdir(), 'od-'));
  const stateVariable = process.platform === 'win32' ? 'LOCALAPPDATA' : 'XDG_STATE_HOME';
  const previousStateHome = process.env[stateVariable];
  process.env[stateVariable] = root;
  const handlers = new Map();
  const timers = new Set();
  const intervalCallbacks = [];
  const warnings = [];
  let thinking = 'high';
  const sentUserMessages = [];
  let idle = true;
  let shutdownCalls = 0;
  let shutdownPromise;
  const pi = {
    pi: {VERSION: 'test'},
    logger: {warn(message, detail) { warnings.push([message, detail]); }},
    on(name, handler) { handlers.set(name, handler); },
    sendUserMessage(content, options) { sentUserMessages.push({content, options}); },
    getThinkingLevel() { return thinking; },
    setThinkingLevel(level) { thinking = level; },
  };
  let branchMessages = [
    {role: 'user', content: 'original question', timestamp: 1},
    {role: 'assistant', content: 'original answer', timestamp: 2},
  ];
  const context = {
    mode: 'tui',
    cwd: '/work/test',
    model: {
      provider: 'test', id: 'model', reasoning: true,
      thinking: {mode: 'effort', efforts: ['low', 'medium', 'high', 'xhigh']},
    },
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
    isIdle: () => idle,
    hasPendingMessages: () => false,
    getAsyncJobSnapshot: () => [],
    setTimeout(callback, delay) {
      const timer = setTimeout(callback, delay);
      timers.add(timer);
      return timer;
    },
    setInterval(callback, delay) {
      intervalCallbacks.push(callback);
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
    shutdown() {
      shutdownCalls++;
      shutdownPromise = handlers.get('session_shutdown')();
    },
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
    const metadataPath = process.platform === 'win32'
      ? path.join(socketDirectory, socketName)
      : path.join(socketDirectory, `${socketName}.json`);
    const metadata = JSON.parse(await readFile(metadataPath, 'utf8'));
    const endpoint = process.platform === 'win32' ? metadata.endpoint : path.join(socketDirectory, socketName);
    if (process.platform !== 'win32') {
      const expectedName = `${createHash('sha256')
        .update(metadata.sessionId).update('\0').update(metadata.connectionId).digest('hex').slice(0, 24)}.sock`;
      assert.equal(socketName, expectedName);
      const socketPathBytes = Buffer.byteLength(endpoint) + 1;
      const socketPathLimit = process.platform === 'linux' ? 108 : 104;
      assert(socketPathBytes <= socketPathLimit, `${socketPathBytes} > ${socketPathLimit}`);
    }
    if (process.platform === 'win32') {
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
    if (process.platform === 'win32') {
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
    assert.equal(registration.capabilities.setThinking, true);
    assert.deepEqual(registration.thinkingLevels, ['off', 'low', 'medium', 'high', 'xhigh']);
    assert.equal(snapshot.data.thinking, 'high');
    assert.deepEqual(snapshot.data.thinkingLevels, registration.thinkingLevels);

    assert.equal(registration.capabilities.attachments, true);
    const png = {type: 'image', data: 'iVBORw0KGgo=', mimeType: 'image/png'};
    socket.write(`${JSON.stringify({
      protocol: 'omp-deck-fallback-v1',
      type: 'request',
      id: 'image-only',
      action: 'sendMessage',
      message: '',
      images: [png],
    })}\n`);
    assert.deepEqual(JSON.parse((await frames.next()).value), {
      protocol: 'omp-deck-fallback-v1',
      type: 'response',
      id: 'image-only',
      ok: true,
      result: {acceptance: 'accepted'},
    });
    assert.deepEqual(sentUserMessages.shift(), {content: [png], options: undefined});

    socket.write(`${JSON.stringify({
      protocol: 'omp-deck-fallback-v1',
      type: 'request',
      id: 'idle-follow-up',
      action: 'followUp',
      message: 'inspect',
      images: [png],
    })}\n`);
    assert.equal(JSON.parse((await frames.next()).value).ok, true);
    assert.deepEqual(sentUserMessages.shift(), {
      content: [{type: 'text', text: 'inspect'}, png],
      options: undefined,
    });

    idle = false;
    socket.write(`${JSON.stringify({
      protocol: 'omp-deck-fallback-v1',
      type: 'request',
      id: 'active-follow-up',
      action: 'followUp',
      message: 'later',
    })}\n`);
    assert.equal(JSON.parse((await frames.next()).value).ok, true);
    assert.deepEqual(sentUserMessages.shift(), {
      content: 'later',
      options: {deliverAs: 'followUp'},
    });

    socket.write(`${JSON.stringify({
      protocol: 'omp-deck-fallback-v1',
      type: 'request',
      id: 'invalid-image',
      action: 'sendMessage',
      message: '',
      images: [{type: 'image', data: '%%%', mimeType: 'image/png'}],
    })}\n`);
    assert.equal(JSON.parse((await frames.next()).value).ok, false);
    assert.equal(sentUserMessages.length, 0);

    socket.write(`${JSON.stringify({
      protocol: 'omp-deck-fallback-v1',
      type: 'request',
      id: 'thinking-1',
      action: 'setThinking',
      level: 'xhigh',
    })}\n`);
    const thinkingFrames = [JSON.parse((await frames.next()).value), JSON.parse((await frames.next()).value)];
    const thinkingEvent = thinkingFrames.find(frame => frame.type === 'event');
    const thinkingResponse = thinkingFrames.find(frame => frame.type === 'response');
    assert.equal(thinkingEvent.kind, 'thinking');
    assert.equal(thinkingEvent.data.thinking, 'xhigh');
    assert.equal(thinkingResponse.result.thinking, thinkingEvent.data.thinking);
    assert.deepEqual(thinkingResponse.result.thinkingLevels, thinkingEvent.data.thinkingLevels);
    assert.equal(thinkingResponse.result.eventSequence, 1);

    socket.write(`${JSON.stringify({
      protocol: 'omp-deck-fallback-v1',
      type: 'request',
      id: 'unsupported-thinking',
      action: 'setThinking',
      level: 'max',
    })}\n`);
    const rejectedThinking = JSON.parse((await frames.next()).value);
    assert.equal(rejectedThinking.ok, false);
    assert.equal(thinking, 'xhigh');

    thinking = 'low';
    intervalCallbacks[0]();
    const heartbeat = JSON.parse((await frames.next()).value);
    assert.equal(heartbeat.kind, 'heartbeat');
    assert.equal(heartbeat.data.thinking, 'low');
    assert.deepEqual(heartbeat.data.thinkingLevels, ['off', 'low', 'medium', 'high', 'xhigh']);

    await handlers.get('message_end')(
      {message: {role: 'assistant', content: 'x'.repeat(1024 * 1024)}},
      context,
    );
    const degraded = JSON.parse((await frames.next()).value);
    assert.equal(degraded.kind, 'degraded');
    assert.equal(degraded.data.droppedKind, 'message');
    assert.equal(degraded.sequence, 4);

    handlers.get('agent_start')({turn: 1});
    const following = JSON.parse((await frames.next()).value);
    assert.equal(following.kind, 'lifecycle');
    assert.equal(following.sequence, 5);

    socket.write(`${JSON.stringify({
      protocol: 'omp-deck-fallback-v1',
      type: 'request',
      id: 'history-1',
      action: 'syncHistory',
    })}\n`);
    const initialHistory = JSON.parse((await frames.next()).value);
    assert.equal(initialHistory.kind, 'history');
    assert.equal(initialHistory.sequence, 6);
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
    assert.equal(treeHistory.sequence, 7);
    assert.equal(treeHistory.data.complete, true);
    assert.deepEqual(treeHistory.data.messages.map(message => message.content), [
      'selected branch question',
      'selected branch answer',
    ]);
    socket.write(`${JSON.stringify({
      protocol: 'omp-deck-fallback-v1',
      type: 'request',
      id: 'shutdown-1',
      action: 'shutdown',
    })}\n`);
    const shutdownResponse = JSON.parse((await frames.next()).value);
    assert.deepEqual(shutdownResponse, {
      protocol: 'omp-deck-fallback-v1',
      type: 'response',
      id: 'shutdown-1',
      ok: true,
    });
    assert.equal(shutdownCalls, 1);
    await shutdownPromise;
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


test('RPC hosts never expose a terminal fallback through lifecycle changes', async () => {
  const root = await mkdtemp(path.join(process.platform === 'darwin' ? '/tmp' : tmpdir(), 'od-rpc-'));
  const stateVariable = process.platform === 'win32' ? 'LOCALAPPDATA' : 'XDG_STATE_HOME';
  const previousStateHome = process.env[stateVariable];
  process.env[stateVariable] = root;
  const handlers = new Map();
  const timers = new Set();
  const context = {
    mode: 'rpc',
    cwd: root,
    sessionManager: {
      getSessionId: () => 'managed-rpc',
      getSessionFile: () => null,
      getSessionName: () => null,
    },
    setInterval(callback, delay) {
      const timer = setInterval(callback, delay);
      timers.add(timer);
      return timer;
    },
    clearTimer(timer) {
      clearInterval(timer);
      timers.delete(timer);
    },
  };
  try {
    const {default: register} = await import(`${PLUGIN.href}?rpc=${Date.now()}`);
    register({
      pi: {VERSION: 'test'},
      logger: {warn() {}},
      on(name, handler) { handlers.set(name, handler); },
    });
    for (const event of ['session_start', 'session_switch', 'session_branch']) {
      await handlers.get(event)({}, context);
      assert.deepEqual(await readdir(root), [], `${event} exposed a fallback endpoint for RPC`);
      assert.equal(timers.size, 0, `${event} left a fallback heartbeat running for RPC`);
    }
  } finally {
    await handlers.get('session_shutdown')?.();
    for (const timer of timers) clearInterval(timer);
    if (previousStateHome === undefined) delete process.env[stateVariable];
    else process.env[stateVariable] = previousStateHome;
    await rm(root, {recursive: true, force: true});
  }
});

test('a stale reconnect timer cannot replace a newer session endpoint', {
  skip: process.platform === 'win32',
}, async () => {
  const root = await mkdtemp(path.join(process.platform === 'darwin' ? '/tmp' : tmpdir(), 'od-retry-'));
  const stateVariable = 'XDG_STATE_HOME';
  const previousStateHome = process.env[stateVariable];
  const blockedStateHome = path.join(root, 'state-home');
  await writeFile(blockedStateHome, 'not a directory');
  process.env[stateVariable] = blockedStateHome;
  const handlers = new Map();
  const timeouts = new Map();
  const intervals = new Set();
  let nextTimer = 0;
  const timerMethods = {
    setTimeout(callback) {
      const timer = {kind: 'timeout', id: ++nextTimer};
      timeouts.set(timer, callback);
      return timer;
    },
    setInterval(callback) {
      const timer = {kind: 'interval', id: ++nextTimer, callback};
      intervals.add(timer);
      return timer;
    },
    clearTimer(timer) {
      timeouts.delete(timer);
      intervals.delete(timer);
    },
  };
  const context = sessionId => ({
    ...timerMethods,
    mode: 'tui',
    cwd: `/work/${sessionId}`,
    model: null,
    sessionManager: {
      getSessionId: () => sessionId,
      getSessionFile: () => null,
      getSessionName: () => sessionId,
      getBranch: () => [],
    },
    isIdle: () => true,
    hasPendingMessages: () => false,
    getAsyncJobSnapshot: () => [],
    abort() {},
    shutdown() {},
  });
  try {
    const {default: register} = await import(`${PLUGIN.href}?retry=${Date.now()}-${Math.random()}`);
    register({
      pi: {VERSION: 'test'},
      logger: {warn() {}},
      on(name, handler) { handlers.set(name, handler); },
      getThinkingLevel() { return undefined; },
      setThinkingLevel() {},
      sendUserMessage() {},
    });
    const first = context('first-session');
    const second = context('second-session');
    await handlers.get('session_start')({}, first);
    assert.equal(timeouts.size, 1);
    const staleRetry = [...timeouts.values()][0];

    await rm(blockedStateHome);
    await mkdir(blockedStateHome);
    await handlers.get('session_switch')({}, second);
    const socketDirectory = path.join(blockedStateHome, 'omp-deck', 'sockets');
    const readRegistration = async () => {
      const metadataName = (await readdir(socketDirectory)).find(name => name.endsWith('.sock.json'));
      assert(metadataName);
      return JSON.parse(await readFile(path.join(socketDirectory, metadataName), 'utf8'));
    };
    assert.equal((await readRegistration()).sessionId, 'second-session');

    staleRetry();
    await new Promise(resolve => setTimeout(resolve, 100));

    assert.equal((await readRegistration()).sessionId, 'second-session');
  } finally {
    await handlers.get('session_shutdown')?.();
    if (previousStateHome === undefined) delete process.env[stateVariable];
    else process.env[stateVariable] = previousStateHome;
    await rm(root, {recursive: true, force: true});
  }
});
