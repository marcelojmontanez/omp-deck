import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('./src/omp_deck/static/fleet-projection.js', import.meta.url), 'utf8');

function loadProjection(overrides = {}) {
  const context = vm.createContext({
    Date,
    Math,
    ...overrides,
  });
  vm.runInContext(source, context, { filename: 'fleet-projection.js' });
  return {
    projectSession: vm.runInContext('projectSession', context),
    requestId: vm.runInContext('requestId', context),
  };
}

function session(overrides = {}) {
  return {
    id: 'session-1',
    title: 'Build release',
    project: { name: 'deck', path: '/work/deck' },
    state: 'needs_input',
    transport: { kind: 'rpc', status: 'connected', label: 'RPC' },
    capabilities: {
      sendMessage: true,
      followUp: true,
      interrupt: true,
      shutdown: false,
      attachments: true,
      answerUiRequest: true,
    },
    messages: [],
    todos: [],
    subagents: [],
    pendingUiRequest: null,
    unread: false,
    updatedAt: '2026-08-20T12:34:56.789Z',
    workingOn: null,
    model: { provider: 'openai', id: 'gpt-test' },
    thinking: 'high',
    ...overrides,
  };
}

test('projects normalized RPC, collaboration, extension, and disconnected sessions', () => {
  const { projectSession } = loadProjection();
  const cases = [
    {
      name: 'rpc',
      input: session(),
      expected: { status: 'needs-input', connected: true, kind: 'rpc', project: 'deck', cwd: '/work/deck' },
    },
    {
      name: 'collaboration',
      input: session({
        id: 'collab',
        state: 'working',
        project: { name: 'shared', path: '/shared' },
        transport: { kind: 'collab', status: 'connected', label: 'Collaboration' },
      }),
      expected: { status: 'working', connected: true, kind: 'collab', project: 'shared', cwd: '/shared' },
    },
    {
      name: 'extension',
      input: session({
        id: 'extension',
        state: 'waiting_for_input',
        transport: { kind: 'extension', status: 'connected', label: 'Extension' },
      }),
      expected: { status: 'waiting-for-input', connected: true, kind: 'extension', project: 'deck', cwd: '/work/deck' },
    },
    {
      name: 'disconnected',
      input: session({
        id: 'gone',
        state: 'disconnected',
        project: null,
        transport: undefined,
      }),
      expected: { status: 'disconnected', connected: false, kind: 'unknown', project: 'Terminal', cwd: '' },
    },
  ];

  for (const { name, input, expected } of cases) {
    const projected = projectSession(input);
    assert.equal(projected.status, expected.status, name);
    assert.equal(projected.executionState, expected.status, name);
    assert.equal(projected.bridgeConnected, expected.connected, name);
    assert.equal(projected.transport.kind, expected.kind, name);
    assert.equal(projected.project, expected.project, name);
    assert.equal(projected.cwd, expected.cwd, name);
  }
});

test('preserves the capability contract and pending UI request shape', () => {
  const { projectSession } = loadProjection();
  const capabilities = {
    sendMessage: true,
    followUp: false,
    interrupt: true,
    shutdown: true,
    todos: true,
    subagents: true,
    attachments: false,
    answerUiRequest: true,
  };
  const pendingUiRequest = {
    id: 'request-7',
    type: 'select',
    status: 'pending',
    title: 'Choose target',
    prompt: 'Where?',
    options: [{ id: 'prod', label: 'Production', description: 'Live environment' }],
    allowMultiple: false,
    allowCustom: true,
  };
  const projected = projectSession(session({ capabilities, pendingUiRequest }));

  assert.deepEqual({ ...projected.capabilities }, capabilities);
  assert.deepEqual(JSON.parse(JSON.stringify(projected.pendingUiRequest)), pendingUiRequest);
  assert.equal(projected.activitySummary, 'Waiting for input');
});

test('keeps message content as plain text data without generating HTML', () => {
  const { projectSession } = loadProjection();
  const hostile = '<img src=x onerror="alert(1)"> & <script>bad()</script>';
  const projected = projectSession(session({
    messages: [
      { id: 'm1', role: 'user', content: hostile, createdAt: '2026-08-20T12:00:00Z' },
      { id: 'm2', role: 'assistant', content: [
        { type: 'text', text: '<b>literal</b>' },
        { type: 'tool', text: '<i>ignored</i>' },
        { type: 'text', text: 'second & line' },
      ], createdAt: '2026-08-20T12:01:00Z' },
    ],
  }));

  assert.equal(projected.session.activity[0].detail, hostile);
  assert.equal(projected.session.activity[1].detail, '<b>literal</b>\nsecond & line');
  assert.equal(projected.session.activity[0].title, 'You');
  assert.equal(projected.session.activity[1].title, 'OMP');
  assert.equal(projected.session.activity.some(item => /&lt;|&gt;|<span|<div/.test(item.detail)), false);
});

test('formats todo phases and statuses as deterministic plain text', () => {
  const { projectSession } = loadProjection();
  const projected = projectSession(session({ todos: [
    { phase: 'Plan', text: 'Read API', status: 'completed' },
    { phase: 'Plan', title: 'Confirm scope', status: 'in_progress' },
    { group: 'Ship', content: 'Deploy safely', status: 'blocked' },
    { group: 'Ship', text: 'Announce', done: true },
  ] }));

  assert.equal(projected.session.todo,
    '  Plan:\n' +
    '    - [X] Read API\n' +
    '    - [ ] Confirm scope (in progress)\n' +
    '  Ship:\n' +
    '    - [ ] Deploy safely (blocked)\n' +
    '    - [X] Announce');
  assert.equal(projected.session.todo.includes('<'), false);
});

test('uses parsed update timestamps and a deterministic clock fallback', () => {
  class FixedDate extends Date {
    static now() { return 1_700_000_000_123; }
  }
  const { projectSession } = loadProjection({ Date: FixedDate });

  assert.equal(projectSession(session()).session.updatedMs, 1_787_229_296_789);
  assert.equal(projectSession(session({ updatedAt: 'not-a-date' })).session.updatedMs, 1_700_000_000_123);
  assert.equal(projectSession(session({ updatedAt: null })).session.updatedMs, 1_700_000_000_123);
});
test('request IDs are nonempty and unique with a deterministic crypto stub', () => {
  let sequence = 0;
  const crypto = { randomUUID: () => `uuid-${++sequence}` };
  const { requestId } = loadProjection({ crypto });
  const ids = [requestId(), requestId(), requestId()];

  assert.deepEqual(ids, ['uuid-1', 'uuid-2', 'uuid-3']);
  assert(ids.every(id => typeof id === 'string' && id.length > 0));
  assert.equal(new Set(ids).size, ids.length);
});
