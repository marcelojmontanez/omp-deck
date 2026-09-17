(() => {
  'use strict';

  function parseBuild(value) {
    if (value === undefined || value === null) return null;
    if (
      typeof value !== 'object' || Array.isArray(value)
      || typeof value.version !== 'string' || !/^[0-9A-Za-z][0-9A-Za-z.!+_-]{0,159}$/.test(value.version)
      || typeof value.revision !== 'string' || !/^(?:[0-9a-f]{40}|unavailable)$/.test(value.revision)
    ) throw new Error('Invalid build identity');
    return Object.freeze({version: value.version, revision: value.revision});
  }

  function parse(payload) {
    const authMode = payload?.authMode === undefined ? 'pairing' : payload.authMode;
    if (
      !payload || !['local', 'coordinator'].includes(payload.mode)
      || !['pairing', 'tailscale'].includes(authMode)
    ) throw new Error('Invalid client configuration');
    return {mode: payload.mode, authMode, build: parseBuild(payload.build)};
  }

  window.ompDeckClientConfig = Object.freeze({parse, parseBuild});
})();
