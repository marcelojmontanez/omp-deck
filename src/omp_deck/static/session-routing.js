'use strict';

function createSessionRouting(isLocalMode) {
  const LOCAL_MACHINE_ID = 'local';
  function encodeBase64Url(value) {
    const bytes = new TextEncoder().encode(value);
    let binary = '';
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
  }

  function decodeBase64Url(value) {
    if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error('Invalid session route');
    const padded = value.replaceAll('-', '+').replaceAll('_', '/') + '='.repeat((4 - value.length % 4) % 4);
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
    return new TextDecoder('utf-8', {fatal: true}).decode(bytes);
  }

  function sessionKey(machineId, localId) {
    if (isLocalMode() || machineId === LOCAL_MACHINE_ID) return localId;
    return `fleet-${encodeBase64Url(JSON.stringify([machineId, localId]))}`;
  }

  function splitSessionKey(key) {
    if (typeof key !== 'string') throw new Error('Invalid session route');
    if (!key.startsWith('fleet-')) return {machineId: LOCAL_MACHINE_ID, localId: key};
    let decoded;
    try {
      decoded = JSON.parse(decodeBase64Url(key.slice(6)));
    } catch {
      throw new Error('Invalid session route');
    }
    if (
      !Array.isArray(decoded) || decoded.length !== 2
      || typeof decoded[0] !== 'string' || typeof decoded[1] !== 'string' || !decoded[1]
    ) throw new Error('Invalid session route');
    return {machineId: decoded[0], localId: decoded[1]};
  }

  function routeRequest(path, requestedMachineId) {
    if (typeof path !== 'string' || !path.startsWith('/api/')) throw new Error('Invalid API route');
    const match = /^\/api\/sessions\/([^/]+)(\/.*)?$/.exec(path);
    if (!match) return {machineId: requestedMachineId, path};
    let key;
    try {
      key = decodeURIComponent(match[1]);
    } catch {
      throw new Error('Invalid session route');
    }
    const route = isLocalMode()
      ? {machineId: LOCAL_MACHINE_ID, localId: key}
      : splitSessionKey(key);
    if (requestedMachineId && requestedMachineId !== route.machineId) throw new Error('Conflicting machine route');
    return {
      machineId: route.machineId,
      path: `/api/sessions/${encodeURIComponent(route.localId)}${match[2] || ''}`,
    };
  }

  return Object.freeze({sessionKey, splitSessionKey, routeRequest});
}
