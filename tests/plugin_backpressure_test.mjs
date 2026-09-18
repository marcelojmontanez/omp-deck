import assert from 'node:assert/strict';
import {mkdtemp, readFile, readdir, rm} from 'node:fs/promises';
import net from 'node:net';
import {tmpdir} from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import test from 'node:test';
import {pathToFileURL} from 'node:url';

const PLUGIN = pathToFileURL(path.join(import.meta.dirname, '../src/omp_deck/plugin/omp-deck.ts'));

async function fixture(messages, options = {}) {
  const root = await mkdtemp(path.join(process.platform === 'darwin' ? '/tmp' : tmpdir(), 'od-backpressure-'));
  const stateVariable = process.platform === 'win32' ? 'LOCALAPPDATA' : 'XDG_STATE_HOME';
  const previousStateHome = process.env[stateVariable];
  process.env[stateVariable] = root;
  const handlers = new Map();
  const timers = new Set();
  const intervalCallbacks = [];
  let abortCalls = 0;
  const context = {
    mode: 'tui', cwd: '/work/backpressure', model: options.model ?? null,
    sessionManager: {
      getSessionId: () => 'backpressure-test', getSessionFile: () => null,
      getSessionName: () => 'Backpressure test',
      getBranch: () => messages.map((message, index) => ({type: 'message', id: String(index), message})),
    },
    isIdle: () => true, hasPendingMessages: () => false,
    getAsyncJobSnapshot: () => options.asyncJobs ?? [],
    setTimeout(callback, delay) { const timer = setTimeout(callback, delay); timers.add(timer); return timer; },
    setInterval(callback, delay) { intervalCallbacks.push(callback); const timer = setInterval(callback, delay); timers.add(timer); return timer; },
    clearTimer(timer) { clearTimeout(timer); clearInterval(timer); timers.delete(timer); },
    abort() { abortCalls++; }, shutdown() {},
  };
  const pi = {
    pi: {VERSION: 'test'}, logger: {warn() {}}, on(name, handler) { handlers.set(name, handler); },
    async sendUserMessage() {},
    getThinkingLevel() { return options.thinking; },
    setThinkingLevel() {},
  };
  const {default: register} = await import(`${PLUGIN.href}?backpressure=${Date.now()}-${Math.random()}`);
  register(pi);
  await handlers.get('session_start')({}, context);
  const directory = process.platform === 'win32'
    ? path.join(root, 'OMP Deck', 'state', 'sockets')
    : path.join(root, 'omp-deck', 'sockets');
  const suffix = process.platform === 'win32' ? '.pipe.json' : '.sock';
  const name = (await readdir(directory)).find(candidate => candidate.endsWith(suffix));
  assert(name);
  const metadata = JSON.parse(await readFile(process.platform === 'win32' ? path.join(directory, name) : path.join(directory, `${name}.json`), 'utf8'));
  const endpoint = process.platform === 'win32' ? metadata.endpoint : path.join(directory, name);
  async function connect(authenticate = true) {
    const socket = net.createConnection(endpoint);
    await new Promise((resolve, reject) => { socket.once('connect', resolve); socket.once('error', reject); });
    if (process.platform === 'win32' && authenticate) socket.write(`${JSON.stringify({protocol: 'omp-deck-fallback-v1', version: 1, type: 'connect', token: metadata.authToken})}\n`);
    return socket;
  }
  async function cleanup() {
    await handlers.get('session_shutdown')();
    for (const timer of timers) { clearTimeout(timer); clearInterval(timer); }
    if (previousStateHome === undefined) delete process.env[stateVariable]; else process.env[stateVariable] = previousStateHome;
    await rm(root, {recursive: true, force: true});
  }
  return {connect, cleanup, context, handlers, timers, intervalCallbacks, get abortCalls() { return abortCalls; }};
}

test('fragmented request input preserves extension framing', async () => {
  const app = await fixture([]);
  let socket;
  try {
    socket = await app.connect();
    const lines = readline.createInterface({input: socket, crlfDelay: Infinity});
    const frames = lines[Symbol.asyncIterator]();
    await frames.next();
    await frames.next();
    const request = `${JSON.stringify({
      protocol: 'omp-deck-fallback-v1', type: 'request', id: 'fragmented', action: 'interrupt',
    })}\n`;
    for (let offset = 0; offset < request.length; offset += 3) socket.write(request.slice(offset, offset + 3));
    const response = JSON.parse((await frames.next()).value);
    assert.equal(response.id, 'fragmented');
    assert.equal(response.ok, true);
    assert.equal(app.abortCalls, 1);
    lines.close();
  } finally {
    socket?.destroy();
    await app.cleanup();
  }
});

test('oversized snapshots retain their small thinking state when collections degrade', async () => {
  const app = await fixture([], {
    thinking: 'high',
    model: {
      provider: 'openai',
      id: 'gpt',
      reasoning: true,
      thinking: {mode: 'effort', efforts: ['low', 'medium', 'high', 'xhigh']},
    },
    asyncJobs: [{output: 'x'.repeat(2 * 1024 * 1024)}],
  });
  let socket;
  try {
    socket = await app.connect();
    const lines = readline.createInterface({input: socket, crlfDelay: Infinity});
    const frames = lines[Symbol.asyncIterator]();
    await frames.next();
    const snapshot = JSON.parse((await frames.next()).value);
    const degraded = JSON.parse((await frames.next()).value);

    assert.equal(snapshot.kind, 'snapshot');
    assert.deepEqual(snapshot.data.async, []);
    assert.equal(snapshot.data.thinking, 'high');
    assert.deepEqual(snapshot.data.thinkingLevels, ['off', 'low', 'medium', 'high', 'xhigh']);
    assert.equal(degraded.kind, 'degraded');
    assert.equal(degraded.data.droppedKind, 'snapshot');
  } finally {
    socket?.destroy();
    await app.cleanup();
  }
});

test('history larger than the outbound cap completes before its response', async () => {
  const messages = Array.from({length: 500}, (_, index) => ({role: 'assistant', content: `${index}:`.padEnd(20 * 1024, 'x'), timestamp: index}));
  const app = await fixture(messages);
  let socket;
  try {
    socket = await app.connect();
    const lines = readline.createInterface({input: socket, crlfDelay: Infinity});
    const frames = lines[Symbol.asyncIterator]();
    await frames.next();
    await frames.next();
    socket.write(`${JSON.stringify({protocol: 'omp-deck-fallback-v1', type: 'request', id: 'large-history', action: 'syncHistory'})}\n`);
    const received = [];
    let response;
    while (!response) {
      const next = await frames.next();
      assert.equal(next.done, false, 'socket closed before history completed');
      const frame = JSON.parse(next.value);
      if (frame.type === 'event' && frame.kind === 'history') received.push(...frame.data.messages);
      if (frame.type === 'response' && frame.id === 'large-history') response = frame;
    }
    assert.equal(response.ok, true);
    assert.equal(received.length, 500);
    assert.equal(received[499].content.slice(0, 4), '499:');
    lines.close();
  } finally {
    socket?.destroy();
    await app.cleanup();
  }
});

test('disconnecting a history consumer does not stall plugin shutdown', async () => {
  const messages = Array.from({length: 500}, (_, index) => ({role: 'assistant', content: `${index}:`.padEnd(20 * 1024, 'x'), timestamp: index}));
  const app = await fixture(messages);
  let socket;
  try {
    socket = await app.connect();
    const lines = readline.createInterface({input: socket, crlfDelay: Infinity});
    const frames = lines[Symbol.asyncIterator]();
    await frames.next();
    await frames.next();
    socket.write(`${JSON.stringify({protocol: 'omp-deck-fallback-v1', type: 'request', id: 'abandoned', action: 'syncHistory'})}\n`);
    const firstHistory = JSON.parse((await frames.next()).value);
    assert.equal(firstHistory.kind, 'history');
    socket.destroy();
    let timeout;
    try {
      await Promise.race([
        app.cleanup(),
        new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('shutdown stalled behind disconnected history')), 1000); }),
      ]);
    } finally {
      clearTimeout(timeout);
      lines.close();
    }
    return;
  } finally {
    socket?.destroy();
  }
});

test('a synchronous event flood cannot create an unbounded outbound frame queue', async () => {
  const app = await fixture([]);
  let socket;
  try {
    socket = await app.connect();
    const lines = readline.createInterface({input: socket, crlfDelay: Infinity});
    const frames = lines[Symbol.asyncIterator]();
    await frames.next();
    await frames.next();
    const closed = new Promise(resolve => socket.once('close', resolve));
    socket.pause();
    for (let index = 0; index < 300; index++) {
      app.handlers.get('message_end')({message: {role: 'assistant', content: String(index)}});
    }
    // Drain already-written bytes so the peer's close is observable locally.
    socket.resume();
    await Promise.race([
      closed,
      new Promise((_, reject) => setTimeout(() => reject(new Error('flooded peer was not disconnected')), 1000)),
    ]);
    assert.equal(socket.destroyed, true);
    lines.close();
  } finally {
    socket?.destroy();
    await app.cleanup();
  }
});

test('Windows shutdown closes unauthenticated named-pipe sockets before clearing timers', {
  skip: process.platform !== 'win32',
}, async () => {
  const app = await fixture([]);
  const socket = await app.connect(false);
  const closed = new Promise(resolve => socket.once('close', resolve));
  let timeout;
  let probe;
  let lines;
  let shutdown;
  try {
    socket.write('{');
    // A completed second handshake proves the first connection was accepted,
    // rather than letting shutdown race ahead of the server's accept callback.
    probe = await app.connect();
    lines = readline.createInterface({input: probe, crlfDelay: Infinity});
    const frames = lines[Symbol.asyncIterator]();
    assert.equal(JSON.parse((await frames.next()).value).type, 'registration');
    assert.equal(JSON.parse((await frames.next()).value).kind, 'snapshot');
    assert.equal(socket.destroyed, false);
    shutdown = app.cleanup();
    await Promise.race([
      Promise.all([shutdown, closed]),
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error('shutdown waited for the authentication timeout')), 1000);
      }),
    ]);
    assert.equal(socket.destroyed, true);
    assert.equal(app.timers.size, 0);
  } finally {
    clearTimeout(timeout);
    socket.destroy();
    probe?.destroy();
    lines?.close();
    await (shutdown ?? app.cleanup());
  }
});

test('overlapping histories stay contiguous and live messages follow the replacement', async () => {
  const messages = Array.from({length: 500}, (_, index) => ({
    role: 'assistant', content: `old-${index}:`.padEnd(20 * 1024, 'x'),
  }));
  const app = await fixture(messages);
  let socket;
  try {
    socket = await app.connect();
    const lines = readline.createInterface({input: socket, crlfDelay: Infinity});
    const frames = lines[Symbol.asyncIterator]();
    await frames.next();
    await frames.next();
    socket.write(`${JSON.stringify({protocol: 'omp-deck-fallback-v1', type: 'request', id: 'overlap', action: 'syncHistory'})}\n`);
    const first = JSON.parse((await frames.next()).value);
    assert.equal(first.kind, 'history');
    messages.splice(0, messages.length,
      {role: 'user', content: 'replacement'},
      {role: 'assistant', content: 'replacement answer'});
    void app.handlers.get('session_tree')({}, app.context);
    const live = {role: 'assistant', content: 'live after replacement'};
    messages.push(live);
    app.handlers.get('message_end')({message: live});

    const syncBuffers = new Map([[first.data.syncId, [...first.data.messages]]]);
    const completedSyncs = [];
    let activeSync = first.data.syncId;
    let transcript = [];
    if (first.data.complete) {
      transcript = [...syncBuffers.get(activeSync)];
      completedSyncs.push(activeSync);
    }
    let sawLive = false;
    while (!sawLive) {
      const frame = JSON.parse((await frames.next()).value);
      if (frame.type !== 'event') continue;
      if (frame.kind === 'history') {
        if (frame.data.syncId !== activeSync) {
          assert.equal(completedSyncs.includes(activeSync), true, 'history syncs interleaved');
          activeSync = frame.data.syncId;
          syncBuffers.set(activeSync, []);
        }
        const buffer = syncBuffers.get(activeSync);
        assert.equal(frame.data.offset, buffer.length);
        buffer.push(...frame.data.messages);
        if (frame.data.complete) {
          transcript = [...buffer];
          completedSyncs.push(activeSync);
        }
      }
      if (frame.kind === 'message') {
        transcript.push(frame.data.message);
        if (frame.data.message.content === live.content) sawLive = true;
      }
    }
    assert.equal(completedSyncs.length, 2);
    const contents = transcript.map(message => message.content);
    assert.deepEqual(contents, [
      'replacement',
      'replacement answer',
      'live after replacement',
    ]);
    assert.equal(contents.filter(content => content === live.content).length, 1);
    lines.close();
  } finally {
    socket?.destroy();
    await app.cleanup();
  }
});

test('large tool payloads cannot replace conversation history or lifecycle with degradation', async () => {
  const large = 'tool output '.repeat(200_000);
  const answer = {role: 'assistant', timestamp: 2, content: [
    {type: 'toolCall', id: 'tool-1', name: 'read', arguments: {output: large}},
    {type: 'text', text: 'The useful answer.'},
    {type: 'image', data: `blob:sha256:${'a'.repeat(64)}`},
    {type: 'image', data: large},
  ]};
  const messages = [{role: 'toolResult', toolCallId: 'tool-1', content: large}, answer];
  const app = await fixture(messages);
  let socket;
  try {
    socket = await app.connect();
    const lines = readline.createInterface({input: socket, crlfDelay: Infinity});
    const frames = lines[Symbol.asyncIterator]();
    await frames.next();
    await frames.next();
    socket.write(`${JSON.stringify({protocol: 'omp-deck-fallback-v1', type: 'request', id: 'large', action: 'syncHistory'})}\n`);
    const history = JSON.parse((await frames.next()).value);
    assert.equal(history.kind, 'history');
    assert.equal(history.data.complete, true);
    assert.deepEqual(history.data.messages.map(message => message.content), [[], [
      {type: 'text', text: 'The useful answer.'},
      {type: 'image', data: `blob:sha256:${'a'.repeat(64)}`},
      {type: 'image'},
    ]]);
    assert.equal(JSON.parse((await frames.next()).value).ok, true);
    const tool = {toolCallId: 'tool-1', toolName: 'read', args: {output: large}, partialResult: large, result: large};
    app.handlers.get('tool_execution_start')(tool);
    app.handlers.get('tool_execution_update')(tool);
    app.handlers.get('tool_execution_end')(tool);
    app.handlers.get('message_update')({message: answer, assistantMessageEvent: {partial: large}});
    app.handlers.get('message_end')({message: answer});
    app.handlers.get('turn_end')({messages, toolResults: large}, app.context);
    app.handlers.get('agent_end')({messages}, app.context);
    const events = [];
    for (let index = 0; index < 7; index++) events.push(JSON.parse((await frames.next()).value));
    assert.deepEqual(events.map(event => event.kind), ['tool', 'tool', 'tool', 'message', 'message', 'async', 'lifecycle']);
    assert.deepEqual(events.slice(0, 3).map(event => [event.data.phase, event.data.toolCallId]), [
      ['start', 'tool-1'], ['update', 'tool-1'], ['end', 'tool-1'],
    ]);
    assert.deepEqual(events[4].data.message, history.data.messages[1]);
    assert.equal(events[6].data.state, 'agent_end');
    assert.equal(events[6].data.snapshot.idle, true);
    lines.close();
  } finally {
    socket?.destroy();
    await app.cleanup();
  }
});

test('interrupt and pong bypass a history write blocked by its reader', async () => {
  const messages = Array.from({length: 500}, (_, index) => ({
    role: 'assistant', content: `${index}:`.padEnd(20 * 1024, 'x'),
  }));
  const app = await fixture(messages);
  let socket;
  const realNow = Date.now;
  try {
    socket = await app.connect();
    const lines = readline.createInterface({input: socket, crlfDelay: Infinity});
    const frames = lines[Symbol.asyncIterator]();
    await frames.next();
    await frames.next();
    socket.write(`${JSON.stringify({protocol: 'omp-deck-fallback-v1', type: 'request', id: 'blocked', action: 'syncHistory'})}\n`);
    assert.equal(JSON.parse((await frames.next()).value).kind, 'history');
    socket.pause();
    Date.now = () => realNow() + 60_000;
    socket.write(
      `${JSON.stringify({protocol: 'omp-deck-fallback-v1', type: 'request', id: 'pong', action: 'pong'})}\n`
      + `${JSON.stringify({protocol: 'omp-deck-fallback-v1', type: 'request', id: 'interrupt', action: 'interrupt'})}\n`,
    );
    for (let attempt = 0; attempt < 20 && app.abortCalls === 0; attempt++) {
      await new Promise(resolve => setImmediate(resolve));
    }
    assert.equal(app.abortCalls, 1);
    app.intervalCallbacks[0]();
    assert.equal(socket.destroyed, false, 'fresh priority pong was queued behind history');
    lines.close();
  } finally {
    Date.now = realNow;
    socket?.destroy();
    await app.cleanup();
  }
});

test('live events queued behind history have an aggregate byte bound', async () => {
  const messages = Array.from({length: 500}, (_, index) => ({
    role: 'assistant', content: `${index}:`.padEnd(20 * 1024, 'x'),
  }));
  const app = await fixture(messages);
  let socket;
  try {
    socket = await app.connect();
    const lines = readline.createInterface({input: socket, crlfDelay: Infinity});
    const frames = lines[Symbol.asyncIterator]();
    await frames.next();
    await frames.next();
    socket.write(`${JSON.stringify({protocol: 'omp-deck-fallback-v1', type: 'request', id: 'blocked-bytes', action: 'syncHistory'})}\n`);
    assert.equal(JSON.parse((await frames.next()).value).kind, 'history');
    const closed = new Promise(resolve => socket.once('close', resolve));
    socket.pause();
    const content = 'x'.repeat(900 * 1024);
    for (let index = 0; index < 5; index++) {
      app.handlers.get('message_end')({message: {role: 'assistant', content}});
    }
    socket.resume();
    await Promise.race([
      closed,
      new Promise((_, reject) => setTimeout(() => reject(new Error('aggregate live-event queue was not bounded')), 1000)),
    ]);
    assert.equal(socket.destroyed, true);
    lines.close();
  } finally {
    socket?.destroy();
    await app.cleanup();
  }
});
