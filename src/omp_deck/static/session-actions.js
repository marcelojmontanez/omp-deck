'use strict';

let sessionActionGeneration = 0;
const sessionActionAttempts = new Map();
const sessionActionOperations = new Map();
const sessionActionStates = new Map();
const sessionActionOwners = new Map();

function clearSessionActionAttempts() {
  for (const operation of sessionActionOperations.values()) operation.controller.abort();
  sessionActionAttempts.clear();
  sessionActionOperations.clear();
  sessionActionStates.clear();
  sessionActionOwners.clear();
  sessionActionGeneration += 1;
  syncSessionActionControls();
}

function sessionActionKey(agent, route) {
  return `${agent.id}:${route}`;
}

function sessionActionOptions(agent) {
  const ongoing = agent?.lifecycle === 'ongoing';
  const nodeAvailable = agent?.nodeAvailable !== false;
  const bridgeConnected = Boolean(agent?.bridgeConnected);
  const capabilities = agent?.capabilities || {};
  const working = (agent?.executionState || agent?.status) === 'working';
  const available = (route, allowed) => Boolean(
    agent && allowed && !sessionActionOperations.has(sessionActionKey(agent, route))
  );
  return [
    {
      action: 'fresh', route: 'fresh', label: 'Start fresh session', idleLabel: 'Start fresh session…',
      enabled: available('fresh', ongoing && nodeAvailable && capabilities.startFresh === true),
    },
    {
      action: 'compact', route: 'compact', command: '/compact', label: 'Compact context',
      enabled: available('compact', ongoing && nodeAvailable && bridgeConnected && capabilities.compact),
    },
    {
      action: 'interrupt', route: 'interrupt', command: '/interrupt', label: 'Interrupt',
      enabled: available(
        'interrupt', ongoing && nodeAvailable && bridgeConnected && capabilities.interrupt && working
      ),
    },
    {
      action: 'quit', route: 'shutdown', command: '/quit', label: 'Quit',
      enabled: available('shutdown', ongoing && nodeAvailable && capabilities.shutdown),
    },
  ];
}

function syncSessionActionControls(agent = selectedAgent()) {
  for (const option of sessionActionOptions(agent)) {
    const button = el['session-menu'].querySelector(`[data-session-action="${option.action}"]`);
    if (!button) continue;
    const status = agent && sessionActionStates.get(sessionActionKey(agent, option.route));
    button.disabled = !option.enabled;
    button.hidden = !option.enabled && status !== 'pending';
    button.textContent = status === 'pending' ? `${option.label}…`
      : status === 'accepted' ? `${option.label} accepted`
        : ['failed', 'uncertain'].includes(status) ? `Retry ${option.label.toLowerCase()}`
          : option.idleLabel || option.label;
    button.title = status === 'uncertain'
      ? 'The outcome is uncertain. Retry safely checks the same request.' : '';
  }
}

function invalidateMachineSessionActions(machineIds) {
  const affected = new Set(machineIds);
  if (!affected.size) return;
  for (const [key, machineId] of sessionActionOwners) {
    if (!affected.has(machineId)) continue;
    sessionActionOperations.get(key)?.controller.abort();
    sessionActionOperations.delete(key);
    sessionActionAttempts.delete(key);
    sessionActionStates.delete(key);
    sessionActionOwners.delete(key);
  }
  syncSessionActionControls();
}
function reconcileMachineSessionActions(detail = {}) {
  invalidateMachineSessionActions([
    ...(detail.removedIds || []),
    ...(detail.addedIds || []),
  ]);
}


async function runSessionAction(action, agent = selectedAgent()) {
  const option = sessionActionOptions(agent).find(item => item.action === action);
  if (!agent || !option?.enabled) return false;
  if (
    (action === 'fresh' && !confirm('Start a fresh session with these settings? The current session will be kept.'))
    || (action === 'compact' && !confirm('Compact this session context?'))
    || (action === 'quit' && !confirm('Shut down this OMP session?'))
  ) return false;

  const machine = ompDeckApi.machine(agent.machineId);
  if (!machine) {
    showToast(`${agent.label || 'Session'}: Machine is no longer enrolled`, 'error');
    return false;
  }
  const generation = sessionActionGeneration;
  const navigationRevision = state.navigationRevision;
  const attemptKey = sessionActionKey(agent, option.route);
  const operationRequestId = sessionActionAttempts.get(attemptKey) || requestId();
  const operation = {controller: new AbortController()};
  sessionActionAttempts.set(attemptKey, operationRequestId);
  sessionActionOperations.set(attemptKey, operation);
  sessionActionOwners.set(attemptKey, agent.machineId);
  sessionActionStates.set(attemptKey, 'pending');
  if (selectedAgent()?.id === agent.id) syncSessionActionControls(agent);
  const activity = action === 'fresh' ? 'Starting a fresh session' : `Requesting ${option.label.toLowerCase()}`;
  showToast(`${agent.label || 'Session'}: ${activity}…`);
  try {
    const {response, payload} = await ompDeckNetwork.fetchJsonBounded(
      `/api/sessions/${encodeURIComponent(agent.id)}/${option.route}`,
      {
        method: 'POST',
        machineId: agent.machineId,
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({requestId: operationRequestId}),
        signal: operation.controller.signal,
      },
      ompDeckNetwork.STARTUP_TIMEOUT_MS,
      ompDeckApi.request,
    );
    if (
      generation !== sessionActionGeneration
      || sessionActionOperations.get(attemptKey) !== operation
      || !ompDeckApi.authenticated()
      || ompDeckApi.machine(agent.machineId)?.origin !== machine.origin
    ) return false;
    if (!response.ok) {
      const error = new Error(payload?.error || `Action failed (${response.status})`);
      error.reuseRequestId = ompDeckApi.shouldReuseRequestId(response, payload || {});
      throw error;
    }
    if (payload?.requestId !== operationRequestId || payload.status !== 'accepted') {
      const error = new Error('Server did not confirm action acceptance');
      error.reuseRequestId = true;
      throw error;
    }
    if (action === 'fresh') {
      if (response.status !== 201 || !payload.session || typeof payload.session.id !== 'string' || !payload.session.id) {
        const error = new Error('Server did not return the fresh session');
        error.reuseRequestId = true;
        throw error;
      }
      const keepFocus = state.navigationRevision === navigationRevision && state.selectedId === agent.id;
      if (!applyFleetSession(payload.session, undefined, agent.machineId)) return false;
      if (keepFocus) selectAgent(ompDeckApi.sessionKey(agent.machineId, payload.session.id));
    }
    sessionActionAttempts.delete(attemptKey);
    sessionActionStates.set(attemptKey, 'accepted');
    showToast(
      action === 'fresh'
        ? `${agent.label || 'Session'}: Fresh session started`
        : `${agent.label || 'Session'}: ${option.label} accepted`,
      'success',
    );
    return true;
  } catch (error) {
    if (
      generation !== sessionActionGeneration
      || sessionActionOperations.get(attemptKey) !== operation
    ) return false;
    const uncertain = error?.reuseRequestId !== false;
    if (!uncertain) sessionActionAttempts.delete(attemptKey);
    sessionActionStates.set(attemptKey, uncertain ? 'uncertain' : 'failed');
    const message = error?.message || 'Action failed';
    if (ompDeckApi.authenticated()) showToast(
      `${agent.label || 'Session'}: ${message}${uncertain ? '. Retry will use the same request.' : ''}`,
      'error',
    );
    return false;
  } finally {
    if (sessionActionOperations.get(attemptKey) === operation) {
      sessionActionOperations.delete(attemptKey);
      if (selectedAgent()?.id === agent.id) syncSessionActionControls(agent);
    }
  }
}

window.addEventListener('omp-deck-machines-changed', event => {
  reconcileMachineSessionActions(event.detail);
});
