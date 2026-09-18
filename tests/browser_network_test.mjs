import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(
  new URL('../src/omp_deck/static/browser-network.js', import.meta.url),
  'utf8',
);

function loadNetwork() {
  const window = {};
  const context = vm.createContext({
    AbortController,
    DOMException,
    Error,
    Promise,
    clearTimeout,
    fetch,
    setTimeout,
    window,
  });
  vm.runInContext(source, context, {filename: 'browser-network.js'});
  return window.ompDeckNetwork;
}

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function outcomeWithin(promise, ms = 250) {
  return await Promise.race([
    promise.then(
      value => ({status: 'fulfilled', value}),
      reason => ({status: 'rejected', reason}),
    ),
    delay(ms).then(() => ({status: 'pending'})),
  ]);
}

test('a pre-aborted caller is rejected with its reason without starting work', async () => {
  const network = loadNetwork();
  const caller = new AbortController();
  const reason = new DOMException('Caller stopped', 'TimeoutError');
  caller.abort(reason);
  let calls = 0;

  const outcome = await outcomeWithin(network.runBounded(() => {
    calls += 1;
    return new Promise(() => {});
  }, caller.signal, 20));

  assert.equal(calls, 0);
  assert.equal(outcome.status, 'rejected');
  assert.equal(outcome.reason, reason);
});

test('caller cancellation promptly settles work that ignores AbortSignal', async () => {
  const network = loadNetwork();
  const caller = new AbortController();
  const reason = new Error('View was replaced');
  let operationSignal;
  let resolveOperation;
  const operation = new Promise(resolve => { resolveOperation = resolve; });

  const bounded = network.runBounded(signal => {
    operationSignal = signal;
    return operation;
  }, caller.signal, 1_000);
  caller.abort(reason);

  const outcome = await outcomeWithin(bounded);
  assert.equal(outcome.status, 'rejected');
  assert.equal(outcome.reason, reason);
  assert.equal(operationSignal.aborted, true);
  assert.equal(operationSignal.reason, reason);

  resolveOperation('too late');
  assert.equal((await outcomeWithin(bounded)).reason, reason);
});

test('deadline promptly settles and consumes a later native rejection', async () => {
  const network = loadNetwork();
  const lateError = new Error('Native request rejected after abort');
  const unhandled = [];
  const onUnhandled = reason => unhandled.push(reason);
  let operationSignal;
  let rejectOperation;
  const operation = new Promise((_, reject) => { rejectOperation = reject; });
  process.on('unhandledRejection', onUnhandled);

  try {
    const bounded = network.runBounded(signal => {
      operationSignal = signal;
      return operation;
    }, undefined, 20);
    const outcome = await outcomeWithin(bounded);

    assert.equal(outcome.status, 'rejected');
    assert(outcome.reason instanceof Error);
    assert.equal(operationSignal.aborted, true);
    assert.equal(operationSignal.reason?.name, 'TimeoutError');

    rejectOperation(lateError);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(unhandled, []);
  } finally {
    process.removeListener('unhandledRejection', onUnhandled);
  }
});

test('response startup deadline ends at headers and leaves a healthy body open', async () => {
  const network = loadNetwork();
  const caller = new AbortController();
  let operationSignal;
  let bodyController;

  const response = await network.fetchResponseBounded('/api/events', {
    signal: caller.signal,
  }, 20, (_input, options) => {
    operationSignal = options.signal;
    return Promise.resolve(new Response(new ReadableStream({
      start(controller) {
        bodyController = controller;
      },
    })));
  });

  await delay(60);
  caller.abort(new Error('Caller moved on after headers'));
  assert.equal(operationSignal.aborted, false);

  bodyController.enqueue(new TextEncoder().encode('event: heartbeat\n\n'));
  const reader = response.body.getReader();
  const {value, done} = await reader.read();
  assert.equal(done, false);
  assert.equal(new TextDecoder().decode(value), 'event: heartbeat\n\n');
  await reader.cancel();
});
