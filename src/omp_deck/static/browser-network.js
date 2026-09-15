(() => {
  'use strict';

  const STARTUP_TIMEOUT_MS = 8_000;

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

  function fetchJsonBounded(input, options = {}, timeoutMs = STARTUP_TIMEOUT_MS) {
    return runBounded(async signal => {
      const response = await fetch(input, {...options, signal});
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
    fetchJsonBounded,
    STARTUP_TIMEOUT_MS,
  });
})();
