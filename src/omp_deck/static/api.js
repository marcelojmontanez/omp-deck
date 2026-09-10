(() => {
  'use strict';

  const LOCAL_MACHINE_ID = 'local';
  let authenticated = false;
  const RENEW_EARLY_MS = 15_000;
  const RETRY_RENEW_MS = 5_000;
  const machines = new Map();
  let csrfToken = null;
  let authGeneration = 0;
  let mode = null;
  let modePromise = null;
  let renewalPromise = null;
  let renewalTimer = null;
  let expiryTimer = null;
  let rosterInitialized = false;
  let coordinatorAvailable = true;

  function emit(name, detail = {}) {
    window.dispatchEvent(new CustomEvent(name, {detail}));
  }

  function publicMachine(machine) {
    return Object.freeze({
      id: machine.id,
      name: machine.name,
      origin: machine.origin,
      status: machine.status,
      error: machine.error || '',
      requiresReload: machine.requiresReload === true,
      local: machine.local === true,
    });
  }

  function machineList() {
    return [...machines.values()].map(publicMachine);
  }

  function notifyMachines(removedIds = [], addedIds = []) {
    emit('omp-deck-machines-changed', {machines: machineList(), removedIds, addedIds});
  }

  function setMachineStatus(machine, status, error = '') {
    if (
      machine
      && machine.local !== true
      && ['live', 'connecting'].includes(status)
      && Date.now() >= machine.expiresAt * 1000
    ) {
      status = 'unavailable';
      error = 'Machine access expired; reconnect to the coordinator';
    }
    if (!machine || (machine.status === status && machine.error === error)) return;
    machine.status = status;
    machine.error = error;
    emit('omp-deck-node-status', {machine: publicMachine(machine)});
  }

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

  async function ensureMode() {
    if (mode) return mode;
    if (!modePromise) {
      modePromise = (async () => {
        const response = await fetch('/api/client-config', {cache: 'no-store', credentials: 'same-origin'});
        if (!response.ok) throw new Error(`Client configuration returned ${response.status}`);
        const payload = await response.json();
        if (!payload || !['local', 'coordinator'].includes(payload.mode)) {
          throw new Error('Invalid client configuration');
        }
        mode = payload.mode;
        if (mode === 'local') {
          machines.set(LOCAL_MACHINE_ID, {
            id: LOCAL_MACHINE_ID,
            name: 'This machine',
            origin: location.origin,
            local: true,
            accessToken: null,
            expiresAt: Number.MAX_SAFE_INTEGER,
            status: 'connecting',
            error: '',
          });
          notifyMachines();
        }
        return mode;
      })().catch(error => {
        modePromise = null;
        throw error;
      });
    }
    return modePromise;
  }

  function authRequired(requestGeneration) {
    if (requestGeneration !== authGeneration) return;
    csrfToken = null;
    authenticated = false;
    authGeneration += 1;
    clearFleet();
    emit('omp-deck-auth-required');
  }

  async function coordinatorRequest(path, options = {}) {
    const requestGeneration = authGeneration;
    const headers = new Headers(options.headers || {});
    const method = (options.method || 'GET').toUpperCase();
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && csrfToken) {
      headers.set('X-OMP-Deck-CSRF', csrfToken);
    }
    let response;
    try {
      response = await fetch(path, {...options, headers, credentials: 'same-origin'});
      coordinatorAvailable = true;
      emit('omp-deck-coordinator-status', {available: true});
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      coordinatorAvailable = false;
      emit('omp-deck-coordinator-status', {available: false, error: 'Coordinator is unreachable'});
      throw new Error(`Coordinator is unreachable: ${error.message}`);
    }
    if (
      response.status === 401
      || (response.status === 403 && path !== '/api/auth/pair')
    ) authRequired(requestGeneration);
    return response;
  }

  async function session() {
    await ensureMode();
    const requestGeneration = authGeneration;
    const response = await fetch('/api/auth/session', {cache: 'no-store', credentials: 'same-origin'});
    if (!response.ok) throw new Error(`Authentication returned ${response.status}`);
    const payload = await response.json();
    if (requestGeneration === authGeneration) {
      authenticated = payload.authenticated === true;
      csrfToken = authenticated ? payload.csrfToken : null;
    }
    return payload;
  }

  async function pair(token) {
    await ensureMode();
    const response = await fetch('/api/auth/pair', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token}),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Pairing returned ${response.status}`);
    authenticated = true;
    authGeneration += 1;
    csrfToken = payload.csrfToken;
    return payload;
  }

  function scheduleAccessTimers() {
    clearTimeout(renewalTimer);
    clearTimeout(expiryTimer);
    renewalTimer = null;
    expiryTimer = null;
    if (mode !== 'coordinator' || !authenticated) return;
    const expiries = [...machines.values()].map(machine => machine.expiresAt * 1000);
    const earliest = expiries.length ? Math.min(...expiries) : Date.now() + 60_000;
    renewalTimer = setTimeout(() => {
      renewalTimer = null;
      void renewAccess().catch(() => {});
    }, Math.max(250, earliest - Date.now() - RENEW_EARLY_MS));
    expiryTimer = setTimeout(() => {
      expiryTimer = null;
      const now = Date.now();
      for (const machine of machines.values()) {
        if (machine.expiresAt * 1000 <= now) {
          setMachineStatus(machine, 'unavailable', coordinatorAvailable
            ? 'Machine access expired'
            : 'Machine access expired while coordinator access is unavailable');
        }
      }
    }, Math.max(250, earliest - Date.now() + 25));
  }

  async function renewAccess() {
    await ensureMode();
    if (mode !== 'coordinator') return machineList();
    if (!authenticated) throw new Error('Authentication required');
    if (renewalPromise) return renewalPromise;
    const requestGeneration = authGeneration;
    renewalPromise = (async () => {
      let response;
      try {
        response = await coordinatorRequest('/api/fleet/access', {
          method: 'POST',
          cache: 'no-store',
          headers: {'Content-Type': 'application/json'},
          body: '{}',
        });
        if (!response.ok) throw new Error(`Fleet access returned ${response.status}`);
        const payload = await response.json();
        if (!authenticated || requestGeneration !== authGeneration) {
          throw new DOMException('Machine access request is stale', 'AbortError');
        }
        if (payload?.version !== 1 || !Array.isArray(payload.machines) || payload.machines.length > 32) {
          throw new Error('Invalid machine access response');
        }
        const incoming = payload.machines.map(value => validateMachine(value, true));
        const ids = new Set(incoming.map(item => item.id));
        if (ids.size !== incoming.length) throw new Error('Invalid duplicate machine roster');
        const previousIds = new Set(machines.keys());
        const removedIds = [...previousIds].filter(id => !ids.has(id));
        const addedIds = rosterInitialized
          ? incoming.filter(item => {
            const previous = machines.get(item.id);
            return !previous || previous.origin !== item.origin;
          }).map(item => item.id)
          : [];
        const next = new Map();
        const addedSet = new Set(addedIds);
        for (const item of incoming) {
          const previous = machines.get(item.id);
          next.set(item.id, {
            ...item,
            local: false,
            requiresReload: addedSet.has(item.id) || previous?.requiresReload === true,
            status: previous?.origin === item.origin ? previous.status : 'connecting',
            error: previous?.origin === item.origin ? previous.error : '',
          });
        }
        machines.clear();
        for (const [id, machine] of next) machines.set(id, machine);
        coordinatorAvailable = true;
        rosterInitialized = true;
        notifyMachines(removedIds, addedIds);
        scheduleAccessTimers();
        return machineList();
      } catch (error) {
        if (requestGeneration !== authGeneration) throw error;
        if (authenticated) {
          coordinatorAvailable = false;
          emit('omp-deck-coordinator-status', {available: false, error: error.message});
        }
        scheduleAccessTimers();
        clearTimeout(renewalTimer);
        if (authenticated) {
          renewalTimer = setTimeout(() => {
            renewalTimer = null;
            void renewAccess().catch(() => {});
          }, RETRY_RENEW_MS);
        }
        throw error;
      }
    })().finally(() => { renewalPromise = null; });
    return renewalPromise;
  }

  async function initializeFleet() {
    await ensureMode();
    if (mode === 'coordinator') await renewAccess();
    return machineList();
  }

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
    if (mode === 'local' || machineId === LOCAL_MACHINE_ID) return localId;
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
    const route = mode === 'local'
      ? {machineId: LOCAL_MACHINE_ID, localId: key}
      : splitSessionKey(key);
    if (requestedMachineId && requestedMachineId !== route.machineId) throw new Error('Conflicting machine route');
    return {
      machineId: route.machineId,
      path: `/api/sessions/${encodeURIComponent(route.localId)}${match[2] || ''}`,
    };
  }

  async function directRequest(path, options, allowRenew = true) {
    if (!authenticated) throw new Error('Authentication required');
    const requestGeneration = authGeneration;
    const {machineId: requestedMachineId, ...fetchOptions} = options || {};
    const route = routeRequest(path, requestedMachineId);
    const machineId = route.machineId || (mode === 'local' ? LOCAL_MACHINE_ID : null);
    let machine = machineId ? machines.get(machineId) : null;
    if (!machine) throw new Error(machineId ? 'Machine is no longer enrolled' : 'Choose a machine');
    if (machine.local) {
      return coordinatorRequest(route.path, fetchOptions);
    }
    if (machine.requiresReload) {
      throw new Error(`Reload the dashboard before connecting to ${machine.name}`);
    }
    if (!machine.accessToken || Date.now() >= machine.expiresAt * 1000) {
      if (allowRenew) {
        try { await renewAccess(); } catch {}
        machine = machines.get(machineId);
      }
      if (!machine?.accessToken || Date.now() >= machine.expiresAt * 1000) {
        if (machine) setMachineStatus(machine, 'unavailable', 'Access expired; reconnect to the coordinator');
        throw new Error(`Access to ${machine?.name || 'machine'} has expired`);
      }
    }
    const headers = new Headers(fetchOptions.headers || {});
    headers.set('Authorization', `Bearer ${machine.accessToken}`);
    const target = new URL(route.path, `${machine.origin}/`);
    if (target.origin !== machine.origin || !target.pathname.startsWith('/api/')) throw new Error('Unsafe machine route');
    let response;
    try {
      response = await fetch(target.href, {...fetchOptions, headers, credentials: 'omit'});
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      setMachineStatus(machines.get(machineId), 'offline', `Direct connection failed: ${error.message}`);
      throw new Error(`Cannot reach ${machine.name} directly. Check network, CORS, HTTPS, and private-network access.`);
    }
    if (requestGeneration !== authGeneration || machines.get(machineId)?.origin !== machine.origin) {
      throw new DOMException('Machine request is stale', 'AbortError');
    }
    if (response.status === 401 && allowRenew) {
      setMachineStatus(machines.get(machineId), 'unavailable', 'Machine access was rejected; renewing');
      try {
        await renewAccess();
        return directRequest(path, options, false);
      } catch {
        setMachineStatus(machines.get(machineId), 'unavailable', 'Machine access was rejected and could not be renewed');
        return response;
      }
    }
    if (response.status === 401 && !allowRenew) {
      setMachineStatus(machines.get(machineId), 'unavailable', 'Machine access was rejected after renewal');
    }
    if (response.status === 403) {
      setMachineStatus(machines.get(machineId), 'unavailable', 'Machine rejected this dashboard origin or access ticket');
    }
    if (response.ok) setMachineStatus(machines.get(machineId), 'live');
    return response;
  }

  async function request(path, options = {}) {
    await ensureMode();
    if (path.startsWith('/api/auth/') || path === '/api/fleet' || path === '/api/fleet/access') {
      return coordinatorRequest(path, options);
    }
    return directRequest(path, options);
  }


  async function logout({all = false} = {}) {
    const response = await coordinatorRequest(all ? '/api/auth/revoke-all' : '/api/auth/logout', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}',
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `Logout returned ${response.status}`);
    }
    authGeneration += 1;
    authenticated = false;
    csrfToken = null;
    clearFleet();
  }

  function clearFleet() {
    clearTimeout(renewalTimer);
    clearTimeout(expiryTimer);
    renewalTimer = null;
    expiryTimer = null;
    rosterInitialized = false;
    const removedIds = [...machines.keys()];
    machines.clear();
    if (mode === 'local') {
      machines.set(LOCAL_MACHINE_ID, {
        id: LOCAL_MACHINE_ID,
        name: 'This machine',
        origin: location.origin,
        local: true,
        accessToken: null,
        expiresAt: Number.MAX_SAFE_INTEGER,
        status: 'connecting',
        error: '',
      });
    }
    notifyMachines(removedIds);
  }

  function shouldReuseRequestId(response, payload = {}) {
    if (payload.status === 'uncertain' || payload.errorCode === 'outcome_unknown') return true;
    if (['failed', 'cancelled', 'stale'].includes(payload.status)) return false;
    return response.ok || response.status >= 500;
  }

  window.ompDeckApi = {
    ensureMode,
    mode: () => mode,
    initializeFleet,
    authenticated: () => authenticated,
    renewAccess,
    machines: machineList,
    machine: id => machines.has(id) ? publicMachine(machines.get(id)) : null,
    setMachineStatus: (id, status, error) => setMachineStatus(machines.get(id), status, error),
    sessionKey,
    splitSessionKey,
    request,
    session,
    pair,
    logout,
    clearFleet,
    shouldReuseRequestId,
  };
})();
