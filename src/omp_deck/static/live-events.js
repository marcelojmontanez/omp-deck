'use strict';

const {createParser: createSseParser} = window.ompDeckSse;

function nodeState(machineId) {
  if (!state.nodes.has(machineId)) {
    state.nodes.set(machineId, {
      presentationRevision: 0,
      snapshotGeneration: 0,
      hasSnapshot: false,
      refreshRequest: null,
      stream: null,
      lastEventId: '',
    });
  }
  return state.nodes.get(machineId);
}

function scheduleEventRecovery(machineId) {
  const node = nodeState(machineId);
  if (node.recoveryTimer) return;
  node.recoveryTimer = setTimeout(() => {
    node.recoveryTimer = null;
    void refresh(machineId);
  }, 100);
}


function applyNodeEvent(machineId, raw) {
  const node = nodeState(machineId);
  const message = JSON.parse(raw);
  if (!message || typeof message !== 'object' || Array.isArray(message)) throw new Error('Invalid fleet event');
  if (message.type === 'snapshot') {
    applyFleetSnapshot(message.payload || {}, null, machineId);
    return;
  }
  const generation = message.snapshotGeneration;
  if (!Number.isSafeInteger(generation)) throw new Error('Invalid fleet event generation');
  if (generation > node.snapshotGeneration + 1) {
    scheduleEventRecovery(machineId);
    return;
  }
  if (generation <= node.snapshotGeneration) return;
  if (message.type === 'session_removed' && typeof message.sessionId === 'string') {
    removeFleetSession(message.sessionId, generation, machineId);
  } else if (message.payload?.session) {
    applyFleetSession(message.payload.session, generation, machineId);
  } else {
    scheduleEventRecovery(machineId);
  }
}

async function consumeSse(response, machineId, stream) {
  if (!response.body) throw new Error('Event stream has no body');
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', {fatal: true});
  const isCurrent = () => (
    !stream.controller.signal.aborted
    && nodeState(machineId).stream === stream
  );
  const parser = createSseParser(parsed => {
    if (!isCurrent()) throw new DOMException('Event stream is stale', 'AbortError');
    if (parsed.event !== 'fleet' || !parsed.data) return;
    applyNodeEvent(machineId, parsed.data);
    if (parsed.id !== null) {
      stream.lastEventId = parsed.id;
      nodeState(machineId).lastEventId = parsed.id;
    }
    stream.failures = 0;
    ompDeckApi.setMachineStatus(machineId, 'live');
  });
  const abortRead = () => { void reader.cancel().catch(() => {}); };
  stream.controller.signal.addEventListener('abort', abortRead, {once: true});
  try {
    while (isCurrent()) {
      const {value, done} = await reader.read();
      if (done) break;
      parser.push(decoder.decode(value, {stream: true}));
    }
    if (isCurrent()) {
      parser.push(decoder.decode());
      parser.finish();
    }
  } finally {
    stream.controller.signal.removeEventListener('abort', abortRead);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function scheduleStreamReconnect(machineId, stream) {
  if (stream.controller.signal.aborted || nodeState(machineId).stream !== stream) return;
  stream.failures += 1;
  const delay = Math.min(30_000, 1000 * (2 ** Math.min(5, stream.failures - 1)));
  stream.timer = setTimeout(() => {
    stream.timer = null;
    if (nodeState(machineId).stream === stream) void connectEventStream(machineId, stream);
  }, delay);
}

async function connectEventStream(machineId, stream) {
  const isCurrent = () => (
    !stream.controller.signal.aborted
    && nodeState(machineId).stream === stream
  );
  if (!isCurrent()) return;
  const machine = ompDeckApi.machine(machineId);
  if (!machine) return;
  if (machine.status === 'offline') ompDeckApi.setMachineStatus(machineId, 'connecting');
  try {
    const headers = {Accept: 'text/event-stream'};
    if (stream.lastEventId) headers['Last-Event-ID'] = stream.lastEventId;
    const response = await ompDeckNetwork.fetchResponseBounded('/api/events', {
      machineId,
      cache: 'no-store',
      headers,
      signal: stream.controller.signal,
    }, ompDeckNetwork.STARTUP_TIMEOUT_MS, ompDeckApi.request);
    if (!isCurrent()) {
      await response.body?.cancel().catch(() => {});
      return;
    }
    if (!response.ok) throw new Error(`Event stream returned ${response.status}`);
    ompDeckApi.setMachineStatus(machineId, 'live');
    await consumeSse(response, machineId, stream);
    if (isCurrent()) throw new Error('Event stream ended');
  } catch (error) {
    if (!isCurrent()) return;
    const current = ompDeckApi.machine(machineId);
    if (current?.status !== 'unavailable') {
      ompDeckApi.setMachineStatus(machineId, 'offline', error.message || 'Direct event stream failed');
    }
    scheduleStreamReconnect(machineId, stream);
  }
}

function startNodeEventStream(machineId) {
  const node = nodeState(machineId);
  if (node.stream && !node.stream.controller.signal.aborted) return;
  const stream = {
    controller: new AbortController(),
    timer: null,
    failures: 0,
    lastEventId: node.lastEventId,
  };
  node.stream = stream;
  void connectEventStream(machineId, stream);
}

function stopNodeEventStream(machineId) {
  const node = state.nodes.get(machineId);
  if (!node) return;
  clearTimeout(node.recoveryTimer);
  node.recoveryTimer = null;
  if (node.stream) {
    clearTimeout(node.stream.timer);
    node.stream.controller.abort();
    node.stream = null;
  }
}

window.addEventListener('omp-deck-node-status', event => {
  if (event.detail?.machine?.status === 'unavailable') {
    stopNodeEventStream(event.detail.machine.id);
  }
});

function startEventStreams() {
  const ids = new Set(ompDeckApi.machines()
    .map(machine => machine.id)
    .filter(machineId => !state.reloadRequiredMachines.has(machineId)));
  for (const machineId of ids) startNodeEventStream(machineId);
  for (const machineId of state.nodes.keys()) {
    if (!ids.has(machineId)) stopNodeEventStream(machineId);
  }
}

function stopEventStreams() {
  for (const machineId of state.nodes.keys()) stopNodeEventStream(machineId);
}

function restartEventStreams() {
  const machineIds = ompDeckApi.machines().map(machine => machine.id);
  for (const machineId of machineIds) stopNodeEventStream(machineId);
  startEventStreams();
}
