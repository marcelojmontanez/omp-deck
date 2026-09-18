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
    if (externalSignal?.aborted) throw externalSignal.reason;

    const controller = new AbortController();
    return await new Promise((resolve, reject) => {
      let pending = true;
      let timer;

      const cleanup = () => {
        clearTimeout(timer);
        externalSignal?.removeEventListener('abort', abortFromCaller);
      };
      const settle = (callback, value) => {
        if (!pending) return;
        pending = false;
        cleanup();
        callback(value);
      };
      const cancel = (reason, outwardError = reason) => {
        if (!pending) return;
        pending = false;
        cleanup();
        reject(outwardError);
        controller.abort(reason);
      };
      const abortFromCaller = () => cancel(externalSignal.reason);

      externalSignal?.addEventListener('abort', abortFromCaller, {once: true});
      timer = setTimeout(() => {
        cancel(
          new DOMException('Request timed out', 'TimeoutError'),
          new Error('Connection timed out'),
        );
      }, timeoutMs);

      let result;
      try {
        result = operation(controller.signal);
      } catch (error) {
        settle(reject, error);
        return;
      }
      Promise.resolve(result).then(
        value => settle(resolve, value),
        error => settle(reject, error),
      );
    });
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
