'use strict';

// Python retains 2 MiB of normalized messages per session. A 32 MiB data cap
// admits a multi-session replacement snapshot while still bounding one frame.
const MAX_SSE_DATA = 32 * 1024 * 1024;
const MAX_SSE_BUFFER = MAX_SSE_DATA + (4 * 1024);

function nodeState(machineId) {
  if (!state.nodes.has(machineId)) {
    state.nodes.set(machineId, {
      presentationRevision: 0,
      snapshotGeneration: 0,
      refreshPromise: null,
      refreshPending: false,
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

function parseSseFrame(frame) {
  let event = 'message';
  let id = null;
  const data = [];
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? '' : line.slice(separator + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') event = value;
    else if (field === 'id' && !value.includes('\0') && value.length <= 256) id = value;
    else if (field === 'data') data.push(value);
  }
  const joined = data.join('\n');
  if (joined.length > MAX_SSE_DATA) throw new Error('Event data is too large');
  return {event, id, data: joined};
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
  let buffer = '';
  try {
    while (!stream.controller.signal.aborted) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      while (true) {
        const match = /\r?\n\r?\n/.exec(buffer);
        if (!match) break;
        if (match.index > MAX_SSE_BUFFER) throw new Error('Event frame is too large');
        const frame = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        const parsed = parseSseFrame(frame);
        if (parsed.event !== 'fleet' || !parsed.data) continue;
        applyNodeEvent(machineId, parsed.data);
        if (parsed.id !== null) {
          stream.lastEventId = parsed.id;
          nodeState(machineId).lastEventId = parsed.id;
        }
        stream.failures = 0;
        ompDeckApi.setMachineStatus(machineId, 'live');
      }
      if (buffer.length > MAX_SSE_BUFFER) throw new Error('Event frame is too large');
    }
    buffer += decoder.decode();
  } finally {
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
  if (stream.controller.signal.aborted || nodeState(machineId).stream !== stream) return;
  const machine = ompDeckApi.machine(machineId);
  if (!machine) return;
  if (machine.status !== 'live') ompDeckApi.setMachineStatus(machineId, 'connecting');
  try {
    const headers = {Accept: 'text/event-stream'};
    if (stream.lastEventId) headers['Last-Event-ID'] = stream.lastEventId;
    const response = await ompDeckApi.request('/api/events', {
      machineId,
      cache: 'no-store',
      headers,
      signal: stream.controller.signal,
    });
    if (!response.ok) throw new Error(`Event stream returned ${response.status}`);
    await consumeSse(response, machineId, stream);
    if (!stream.controller.signal.aborted) throw new Error('Event stream ended');
  } catch (error) {
    if (stream.controller.signal.aborted) return;
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
