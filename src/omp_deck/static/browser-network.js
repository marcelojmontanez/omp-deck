(() => {
  'use strict';

  const STARTUP_TIMEOUT_MS = 8_000;

  function createObservationAuthority() {
    let next = 0;
    let accepted = 0;
    return Object.freeze({
      begin() {
        next += 1;
        return next;
      },
      accept(observation) {
        if (!Number.isSafeInteger(observation) || observation < accepted) return false;
        accepted = observation;
        return true;
      },
      isLatest: observation => Number.isSafeInteger(observation) && observation === next,
    });
  }

  async function runBounded(operation, externalSignal, timeoutMs) {
    const controller = new AbortController();
    const abortFromCaller = () => controller.abort(externalSignal.reason);
    if (externalSignal?.aborted) abortFromCaller();
    else externalSignal?.addEventListener('abort', abortFromCaller, {once: true});
    const timer = setTimeout(() => {
      controller.abort(new DOMException('Request timed out', 'TimeoutError'));
    }, timeoutMs);
    try {
      return await operation(controller.signal);
    } catch (error) {
      if (controller.signal.reason?.name === 'TimeoutError') {
        throw new Error('Connection timed out');
      }
      throw error;
    } finally {
      clearTimeout(timer);
      externalSignal?.removeEventListener('abort', abortFromCaller);
    }
  }

  function fetchResponseBounded(input, options = {}, timeoutMs = STARTUP_TIMEOUT_MS, request = fetch) {
    return runBounded(signal => request(input, {...options, signal}), options.signal, timeoutMs);
  }

  function fetchJsonBounded(input, options = {}, timeoutMs = STARTUP_TIMEOUT_MS, request = fetch) {
    return runBounded(async signal => {
      const response = await request(input, {...options, signal});
      let payload;
      try {
        payload = await response.json();
      } catch (error) {
        if (response.ok || signal.aborted) throw error;
        payload = null;
      }
      return {response, payload};
    }, options.signal, timeoutMs);
  }

  window.ompDeckNetwork = Object.freeze({
    createObservationAuthority,
    fetchJsonBounded,
    fetchResponseBounded,
    runBounded,
    STARTUP_TIMEOUT_MS,
  });
})();
