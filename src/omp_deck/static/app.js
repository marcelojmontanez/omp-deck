try { localStorage.clear(); } catch {}
const thinkingOverrides = new Map();
let thinkingUpdateTokens = new Map();
const initialSession = new URL(location.href).searchParams.get('session');
const state = {
  agents: [],
  nodes: new Map(),
  reloadRequiredMachines: new Set(),
  selectedId: null,
  selectedMachineId: null,
  coordinatorAvailable: true,
  pendingSelection: initialSession,
  filter: 'all',
  sessionScope: 'ongoing',
  showHistory: false,
  showTodo: false,
  showArchive: false,
  renderRevision: 0,
  renderedRevision: -1,
  renderedSessionId: null,
  renderedUpdatedMs: null,
  recoveryTimer: null,
};

const el = Object.fromEntries([
  'connection', 'connection-text', 'working-count', 'attention-count', 'unread-count',
  'idle-count', 'agent-list', 'history-toggle', 'history-count', 'session-scope-title',
  'app', 'workspace', 'detail', 'detail-chips', 'task-control', 'transport-status',
  'pending-request', 'activity', 'scroll-newest', 'composer', 'attachments', 'attach',
  'attachment-status', 'command-menu', 'command-mode', 'delivery-mode', 'message', 'send',
  'send-state', 'toast', 'overflow-button', 'session-menu', 'machine-panel', 'machine-select',
  'session-info', 'session-metadata', 'session-name-dialog', 'session-name-form',
  'session-name-input', 'session-name-status', 'deck-settings', 'settings-status',
  'machine-status-list', 'fleet-reload', 'fleet-reload-button', 'project-root-label',
  'project-select', 'project-new', 'project-launch', 'launch-state'
].map(id => [id, document.getElementById(id)]));
async function clearLocalData() {
  try { localStorage.clear(); } catch {}
  try { sessionStorage.clear(); } catch {}
  thinkingOverrides.clear();
  thinkingUpdateTokens = new Map();
  composer?.clearLocalData();
  clearPendingRequestAttempts();
  clearSessionActionAttempts();
  if (typeof window.clearLauncherAttempts === 'function') window.clearLauncherAttempts();
  ompDeckUsage.invalidate();
  ompDeckImages.clear();
  if ('caches' in window) {
    try {
      await Promise.all((await caches.keys()).map(name => caches.delete(name)));
    } catch {
      // Browser storage may be unavailable without affecting the live dashboard.
    }
  }
}

window.ompDeckLocalData = Object.freeze({clear: clearLocalData});
let composer;
let authRecovery;
const {
  markRead,
  selectedAgent,
  touchRender,
  showToast,
  renderSummary,
  renderList,
  renderDetail,
  selectAgent,
  showMenu,
  renderFleet,
  applyFleetSnapshot,
  applyFleetSession,
  applyFleetPatch,
  removeFleetSession,
} = createDeckRendering({
  state,
  el,
  thinkingOverrides,
  getComposer: () => composer,
});

async function refreshNode(machineId, {replace = false} = {}) {
  const node = nodeState(machineId);
  if (node.refreshRequest && !replace) {
    node.refreshRequest.pending = true;
    return node.refreshRequest.promise;
  }
  if (node.refreshRequest) node.refreshRequest.controller.abort();
  const request = {
    controller: new AbortController(),
    pending: false,
    promise: null,
  };
  node.refreshRequest = request;
  request.promise = (async () => {
    const requestedRevision = node.presentationRevision;
    const requestedGeneration = node.snapshotGeneration;
    try {
      const {response, payload: snapshot} = await ompDeckNetwork.fetchJsonBounded('/api/sessions', {
        machineId,
        cache: 'no-store',
        signal: request.controller.signal,
      }, ompDeckNetwork.STARTUP_TIMEOUT_MS, ompDeckApi.request);
      if (!response.ok) throw new Error(`Sessions returned ${response.status}`);
      if (request.controller.signal.aborted || node.refreshRequest !== request) return;
      applyFleetSnapshot(snapshot, requestedGeneration, machineId, requestedRevision);
    } catch (error) {
      if (request.controller.signal.aborted || node.refreshRequest !== request) return;
      showToast('Session refresh failed; keeping the last received state.', 'error');
    } finally {
      if (node.refreshRequest === request) {
        node.refreshRequest = null;
        if (request.pending && ompDeckApi.authenticated()) void refreshNode(machineId);
      }
    }
  })();
  return request.promise;
}

async function refresh(machineId = null, options = {}) {
  const targets = (machineId
    ? [machineId]
    : ompDeckApi.machines().map(machine => machine.id))
    .filter(id => !state.reloadRequiredMachines.has(id));
  await Promise.allSettled(targets.map(id => refreshNode(id, options)));
}

function removeMachinePresentation(machineId) {
  stopNodeEventStream(machineId);
  state.nodes.delete(machineId);
  state.reloadRequiredMachines.delete(machineId);
  ompDeckImages.releaseMachine(machineId);
  const removed = state.agents.filter(agent => agent.machineId === machineId).map(agent => agent.id);
  state.agents = state.agents.filter(agent => agent.machineId !== machineId);
  if (removed.includes(state.selectedId)) {
    const preserveNavigation = !ompDeckApi.authenticated();
    if (preserveNavigation) state.pendingSelection = state.selectedId;
    showMenu();
    if (!preserveNavigation) {
      const url = new URL(location.href);
      url.searchParams.delete('session');
      history.replaceState({view: 'menu'}, '', url);
    }
  }
  touchRender();
}
window.addEventListener('omp-deck-machines-changed', event => {
  const previousSelection = state.selectedMachineId;
  for (const machineId of event.detail?.removedIds || []) removeMachinePresentation(machineId);
  // An open SSE connection still carries its original, short-lived ticket.
  for (const machineId of event.detail?.renewedIds || []) stopNodeEventStream(machineId);
  const addedIds = event.detail?.addedIds || [];
  for (const machineId of addedIds) {
    state.reloadRequiredMachines.add(machineId);
    ompDeckImages.releaseMachine(machineId);
    for (const agent of state.agents) {
      if (agent.machineId === machineId) agent.nodeAvailable = false;
    }
  }
  if (addedIds.length && ompDeckApi.mode() === 'coordinator') {
    el['fleet-reload'].hidden = false;
  }
  renderMachineControls();
  if (!el.app.hidden && ompDeckApi.authenticated()) {
    if (
      state.selectedMachineId !== previousSelection
      || addedIds.includes(state.selectedMachineId)
      || (event.detail?.renewedIds || []).includes(state.selectedMachineId)
    ) {
      ompDeckUsage.machineChanged();
      void loadLaunchableProjects();
    }
    startEventStreams();
    renderFleet();
  }
});
window.addEventListener('omp-deck-node-status', event => {
  const machine = event.detail?.machine;
  if (!machine) return;
  const available = machine.status === 'live';
  for (const agent of state.agents) {
    if (agent.machineId === machine.id) agent.nodeAvailable = available;
  }
  if (available) ompDeckImages.recover(machine.id, {replacePending: false});
  touchRender();
  renderMachineControls();
  renderFleet();
});

window.addEventListener('omp-deck-coordinator-status', event => {
  if (ompDeckApi.mode() !== 'coordinator') return;
  state.coordinatorAvailable = event.detail?.available !== false;
  renderMachineControls();
});

el['machine-select'].addEventListener('change', () => {
  state.selectedMachineId = el['machine-select'].value || null;
  ompDeckUsage.machineChanged();
  void loadLaunchableProjects();
});
el['fleet-reload-button'].addEventListener('click', () => location.reload());


el['history-toggle'].addEventListener('click', () => {
  if (!state.showHistory) return;
  state.sessionScope = state.sessionScope === 'history' ? 'ongoing' : 'history';
  state.filter = 'all';
  document.querySelectorAll('.filter').forEach(item =>
    item.classList.toggle('active', item.dataset.filter === 'all')
  );
  renderSummary();
  renderList();
});

document.querySelector('.filters').addEventListener('click', event => {
  const button = event.target.closest('[data-filter]');
  if (!button) return;
  state.filter = button.dataset.filter;
  document.querySelectorAll('.filter').forEach(item => item.classList.toggle('active', item === button));
  renderList();
});

el['agent-list'].addEventListener('click', event => {
  const card = event.target.closest('[data-id]');
  if (card) selectAgent(card.dataset.id);
});
function stopLiveConnections() {
  stopEventStreams();
  for (const node of state.nodes.values()) {
    node.refreshRequest?.controller.abort();
    node.refreshRequest = null;
  }
}

let liveRecoveryGeneration = 0;
async function recoverLiveConnections() {
  if (!ompDeckApi.authenticated()) return;
  const generation = ++liveRecoveryGeneration;
  stopLiveConnections();
  try {
    await ompDeckApi.recoverAccess();
  } catch {
    // Existing valid machine access can outlive a coordinator interruption.
  }
  if (generation !== liveRecoveryGeneration || !ompDeckApi.authenticated()) return;
  restartEventStreams();
  void refresh(null, {replace: true});
  ompDeckUsage.foreground();
  if (state.selectedMachineId) recoverLaunchableProjects();
  ompDeckImages.recover();
  void composer.recover(selectedAgent());
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  recoverLiveConnections();
  if (!state.selectedId) return;
  renderDetail();
  markRead(selectedAgent());
  renderSummary();
  renderList();
});
window.addEventListener('offline', renderFleet);
window.addEventListener('online', recoverLiveConnections);
window.addEventListener('pageshow', event => {
  if (event.persisted) void recoverLiveConnections();
});
function recoverVisibleConnections() {
  if (document.visibilityState === 'visible') void recoverLiveConnections();
}
window.addEventListener('focus', recoverVisibleConnections);
document.addEventListener('resume', recoverVisibleConnections);
window.addEventListener('popstate', event => {
  const session = event.state?.view === 'chat'
    ? event.state.session
    : new URL(location.href).searchParams.get('session');
  if (session && selectAgent(session, {pushHistory: false})) return;
  const url = new URL(location.href);
  url.searchParams.delete('session');
  history.replaceState({view: 'menu'}, '', url);
  showMenu();
});

composer = createComposer({
  elements: el,
  selectedAgent,
  thinkingLevel,
  thinkingChoices,
  commandActions: agent => sessionActionOptions(agent),
  executeAction: (action, agent) => runSessionAction(action, agent),
  setThinkingLevel: (level, agent) => setThinkingLevel(level, agent),
  showToast,
  touchRender,
  renderDetail,
  refresh,
  escapeHtml,
});
el.composer.addEventListener('pointerdown', event => {
  if (event.target === el.send && document.activeElement === el.message) event.preventDefault();
});
document.querySelector('.detail-header').addEventListener('click', event => {
  const todoButton = event.target.closest('[data-toggle-todo]');
  if (todoButton) {
    state.showTodo = !state.showTodo;
    touchRender();
    renderDetail();
    return;
  }
  const button = event.target.closest('[data-thinking-level]');
  const agent = selectedAgent();
  const choices = thinkingChoices(agent);
  if (!button || !choices.length) return;
  const current = thinkingLevel(agent);
  void setThinkingLevel(choices[(choices.indexOf(current) + 1) % choices.length], agent);
});

async function setThinkingLevel(next, agent) {
  if (!agent || agent.lifecycle !== 'ongoing' || agent.nodeAvailable === false || !agent.bridgeConnected
      || !agent.capabilities?.setThinking || !thinkingChoices(agent).includes(next)) return false;
  const updates = thinkingUpdateTokens;
  const previous = updates.get(agent.id);
  let settle;
  const updateToken = {done: new Promise(resolve => { settle = resolve; })};
  updates.set(agent.id, updateToken);
  thinkingOverrides.set(agent.id, next);
  touchRender();
  renderDetail();
  try {
    await previous?.done;
    if (updates !== thinkingUpdateTokens) return false;
    const response = await ompDeckApi.request(`/api/sessions/${encodeURIComponent(agent.id)}/thinking`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({requestId: requestId(), level: next}),
    });
    if (!response.ok) throw new Error(`Thinking update failed (${response.status})`);
    if (updates.get(agent.id) !== updateToken) return false;
    await refresh(agent.machineId);
    if (updates !== thinkingUpdateTokens || updates.get(agent.id) !== updateToken) return false;
    updates.delete(agent.id);
    thinkingOverrides.delete(agent.id);
    touchRender();
    renderDetail();
    return true;
  } catch (error) {
    if (!ompDeckApi.authenticated() || updates !== thinkingUpdateTokens || updates.get(agent.id) !== updateToken) return false;
    updates.delete(agent.id);
    thinkingOverrides.delete(agent.id);
    touchRender();
    renderDetail();
    showToast(error.message, 'error');
    void refresh(agent.machineId);
    return false;
  } finally {
    settle();
  }
}
el.activity.addEventListener('click', event => {
  const receipt = event.target.closest('[data-delivery-action]');
  if (receipt) {
    const id = receipt.dataset.deliveryId;
    if (receipt.dataset.deliveryAction === 'restore') composer.restoreDelivery(id);
    else void composer.dismissDelivery(id).catch(() => showToast('Could not dismiss receipt. Try again.', 'error'));
    return;
  }
  const button = event.target.closest('[data-toggle-archive]');
  if (!button) return;
  state.showArchive = !state.showArchive;
  touchRender();
  renderDetail();
});
el['pending-request'].addEventListener('submit', event => {
  event.preventDefault();
  void settlePendingRequest();
});
el['pending-request'].addEventListener('click', event => {
  if (event.target.closest('[data-cancel-request]')) void settlePendingRequest({cancel: true});
});
el.activity.addEventListener('scroll', () => {
  el['scroll-newest'].hidden = el.activity.scrollTop < 160;
}, {passive: true});
const syncComposerHeight = () => {
  el.detail.style.setProperty('--composer-height', `${el.composer.getBoundingClientRect().height}px`);
};
new ResizeObserver(syncComposerHeight).observe(el.composer);
syncComposerHeight();
el['scroll-newest'].addEventListener('click', () => {
  el.activity.scrollTo({top: 0, behavior: 'smooth'});
});


function resetApplication() {
  liveRecoveryGeneration += 1;
  stopLiveConnections();
  ompDeckUsage.stop();
  state.nodes.clear();
  state.reloadRequiredMachines.clear();
  el['fleet-reload'].hidden = true;
  clearInterval(state.recoveryTimer);
  state.recoveryTimer = null;
  const viewer = document.getElementById('image-viewer');
  if (viewer.open) {
    if (typeof window.closeImageViewer === 'function') window.closeImageViewer();
    else viewer.close();
  }
  el['agent-list'].replaceChildren();
  document.querySelectorAll('[data-session-identity], #detail-chips').forEach(node => node.replaceChildren());
  el.detail.setAttribute('aria-label', 'Conversation');
  for (const dialog of document.querySelectorAll('.deck-dialog[open]')) dialog.close();
  document.getElementById('settings-status').textContent = '';
  el['pending-request'].replaceChildren();
  el['pending-request'].hidden = true;
  el.activity.innerHTML = '<p class="activity-empty">Waiting for structured OMP events.</p>';
  document.getElementById('usage-list').replaceChildren();
  el['project-select'].replaceChildren(new Option('Authentication required', ''));
  el.toast.hidden = true;
  el.toast.textContent = '';
  el['launch-state'].textContent = '';
  state.agents = [];
  state.selectedId = null;
  state.pendingSelection = new URL(location.href).searchParams.get('session');
  state.showArchive = false;
  state.showTodo = false;
  closeSessionMenu();
  composer?.select(null);
  el.app.classList.remove('chat-open');
  document.body.classList.remove('chat-open');
  el.workspace.classList.remove('chat-open');
  ompDeckImages.clear();
}

async function showApplication() {
  const opening = el.app.hidden;
  document.getElementById('auth-screen').hidden = true;
  el.app.hidden = false;
  state.showHistory = ompDeckApi.showHistory();
  if (!state.showHistory) state.sessionScope = 'ongoing';
  renderSummary();
  if (opening) document.getElementById('dashboard-title').focus();
  const requestedSession = new URL(location.href).searchParams.get('session');
  state.pendingSelection = requestedSession;
  if (requestedSession) {
    history.replaceState({view: 'chat', session: requestedSession}, '', location.href);
  } else {
    const url = new URL(location.href);
    url.searchParams.delete('session');
    history.replaceState({view: 'menu'}, '', url);
  }
  try {
    await ompDeckApi.initializeFleet();
  } catch (error) {
    el.connection.classList.add('offline');
    el['connection-text'].textContent = error.message;
  }
  if (!ompDeckApi.authenticated()) return;
  renderMachineControls();
  startEventStreams();
  void loadLaunchableProjects();
  ompDeckUsage.start();
  if (!state.recoveryTimer) state.recoveryTimer = setInterval(refresh, 5 * 60_000);
}

authRecovery = createAuthRecovery({
  api: ompDeckApi,
  showApplication,
  resetApplication,
});
