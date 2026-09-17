(() => {
  'use strict';

  function validOrigin(value) {
    if (typeof value !== 'string' || value.length > 2048) return null;
    try {
      const parsed = new URL(value);
      if (
        !['http:', 'https:'].includes(parsed.protocol)
        || parsed.origin !== value
        || parsed.username
        || parsed.password
        || parsed.pathname !== '/'
        || parsed.search
        || parsed.hash
      ) return null;
      return parsed.origin;
    } catch {
      return null;
    }
  }

  function validateMachine(value, requireAccess) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid machine roster');
    const id = value.id;
    const name = value.name;
    const origin = validOrigin(value.origin);
    if (
      typeof id !== 'string' || !/^[a-f0-9]{32}$/.test(id)
      || typeof name !== 'string' || !name.trim() || name.length > 80
      || !origin
    ) throw new Error('Invalid machine roster');
    if (requireAccess && (
      typeof value.accessToken !== 'string'
      || !value.accessToken || value.accessToken.length > 4096
      || !Number.isSafeInteger(value.expiresAt)
    )) throw new Error('Invalid machine access response');
    return {id, name, origin, accessToken: value.accessToken || null, expiresAt: value.expiresAt || 0};
  }

  window.ompDeckFleetAccess = Object.freeze({validateMachine});
})();
