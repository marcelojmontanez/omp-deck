try { localStorage.clear(); } catch {}
const thinkingOverrides = new Map();
let thinkingUpdateTokens = new Map();
const THINKING_LEVELS = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'auto'];
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
  'idle-count', 'agent-list', 'app', 'workspace', 'detail', 'detail-chips',
  'task-control', 'transport-status', 'pending-request', 'activity', 'scroll-newest',
  'composer', 'attachments', 'attach', 'gallery', 'attachment-input', 'gallery-input',
  'message', 'send', 'send-state', 'toast', 'overflow-button', 'session-menu',
  'machine-panel', 'machine-select', 'machine-status-list', 'fleet-reload', 'fleet-reload-button',
  'project-root-label', 'project-select', 'project-new', 'project-launch', 'launch-state'
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
  usageLoadedAt = 0;
  usageMachineId = null;
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
  removeFleetSession,
} = createDeckRendering({
  state,
  el,
  thinkingOverrides,
  getComposer: () => composer,
});

async function refreshNode(machineId) {
  const node = nodeState(machineId);
  if (node.refreshPromise) {
    node.refreshPending = true;
    return node.refreshPromise;
  }
  node.refreshPromise = (async () => {
    do {
      const requestedRevision = node.presentationRevision;
      node.refreshPending = false;
      const requestedGeneration = node.snapshotGeneration;
      try {
        const response = await ompDeckApi.request('/api/sessions', {
          machineId,
          cache: 'no-store',
        });
        if (!response.ok) throw new Error(`Sessions returned ${response.status}`);
        applyFleetSnapshot(await response.json(), requestedGeneration, machineId, requestedRevision);
      } catch (error) {
        const machine = ompDeckApi.machine(machineId);
        if (machine?.status !== 'unavailable') {
          ompDeckApi.setMachineStatus(machineId, 'offline', error.message);
        }
      }
    } while (node.refreshPending);
  })().finally(() => { node.refreshPromise = null; });
  return node.refreshPromise;
}

async function refresh(machineId = null) {
  const targets = (machineId
    ? [machineId]
    : ompDeckApi.machines().map(machine => machine.id))
    .filter(id => !state.reloadRequiredMachines.has(id));
  await Promise.allSettled(targets.map(refreshNode));
}
function machineStatusLabel(status) {
  return {
    connecting: 'Connecting',
    live: 'Live',
    offline: 'Offline',
    unavailable: 'Access unavailable',
  }[status] || 'Unavailable';
}

function renderMachineControls() {
  const machines = ompDeckApi.machines();
  const coordinatorMode = ompDeckApi.mode() === 'coordinator';
  el['machine-panel'].hidden = !coordinatorMode;
  const previous = state.selectedMachineId;
  el['machine-select'].replaceChildren(...machines.map(machine =>
    new Option(`${machine.name} — ${machineStatusLabel(machine.status)}`, machine.id)
  ));
  state.selectedMachineId = machines.some(machine => machine.id === previous)
    ? previous
    : machines[0]?.id || null;
  el['machine-select'].value = state.selectedMachineId || '';
  el['machine-select'].disabled = machines.length === 0;
  el['machine-status-list'].innerHTML = machines.length
    ? machines.map(machine => `<span class="machine-status ${escapeHtml(machine.status)}" title="${escapeHtml(machine.error || `${machine.name} is ${machineStatusLabel(machine.status).toLowerCase()}`)}"><i aria-hidden="true"></i><strong>${escapeHtml(machine.name)}</strong> ${escapeHtml(machineStatusLabel(machine.status))}</span>`).join('')
    : '<span class="machine-empty">No machines are enrolled.</span>';
  const live = machines.filter(machine => machine.status === 'live').length;
  if (coordinatorMode && state.coordinatorAvailable === false) {
    el.connection.classList.add('offline');
    el['connection-text'].textContent = 'Coordinator access unavailable; current machine access expires shortly';
  } else {
    el.connection.classList.toggle('offline', machines.length > 0 && live === 0);
    el['connection-text'].textContent = coordinatorMode
      ? `${live}/${machines.length} machines live`
      : (machines[0]?.status === 'live' ? 'Live' : machineStatusLabel(machines[0]?.status));
  }
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
  if (!appRoot.hidden && ompDeckApi.authenticated()) {
    if (
      state.selectedMachineId !== previousSelection
      || addedIds.includes(state.selectedMachineId)
    ) {
      usageLoadedAt = 0;
      void loadLaunchableProjects();
      void refreshUsage();
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
  usageLoadedAt = 0;
  void loadLaunchableProjects();
  void refreshUsage();
});
el['fleet-reload-button'].addEventListener('click', () => location.reload());


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
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  refresh();
  if (!state.selectedId) return;
  renderDetail();
  markRead(selectedAgent());
  renderSummary();
  renderList();
});
window.addEventListener('popstate', event => {
  const session = event.state?.view === 'chat'
    ? event.state.session
    : new URL(location.href).searchParams.get('session');
  if (session && state.agents.some(agent => agent.id === session)) {
    selectAgent(session, {pushHistory: false});
  } else {
    showMenu();
  }
});

composer = createComposer({
  elements: el,
  selectedAgent,
  thinkingLevel,
  showToast,
  touchRender,
  renderDetail,
  refresh,
  escapeHtml,
});
el['session-menu'].addEventListener('click', async event => {
  const clearButton = event.target.closest('[data-clear-local-data]');
  const authButton = event.target.closest(
    '[data-session-action="logout"], [data-session-action="revoke-all"]'
  );
  if (!clearButton && !authButton) return;
  event.stopImmediatePropagation();
  closeSessionMenu();
  if (clearButton) {
    if (!confirm('Clear drafts and other local browser data for OMP Deck?')) return;
    await clearLocalData();
    showToast('Local data cleared', 'success');
    return;
  }
  const all = authButton.dataset.sessionAction === 'revoke-all';
  if (all && !confirm('Revoke access for every paired device?')) return;
  try {
    await ompDeckApi.logout({all});
    await clearLocalData();
    showAuthentication();
  } catch (error) {
    showToast(error.message, 'error');
  }
});
document.querySelector('.detail-header').addEventListener('click', async event => {
  const todoButton = event.target.closest('[data-toggle-todo]');
  if (todoButton) {
    state.showTodo = !state.showTodo;
    touchRender();
    renderDetail();
    return;
  }
  const button = event.target.closest('[data-thinking-level]');
  const agent = selectedAgent();
  if (!button || agent?.nodeAvailable === false || !agent?.bridgeConnected || !agent.capabilities.setThinking) return;
  const current = thinkingLevel(agent);
  const next = THINKING_LEVELS[(THINKING_LEVELS.indexOf(current) + 1) % THINKING_LEVELS.length];
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
    if (updates !== thinkingUpdateTokens) return;
    const response = await ompDeckApi.request(`/api/sessions/${encodeURIComponent(agent.id)}/thinking`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({requestId: requestId(), level: next}),
    });
    if (!response.ok) throw new Error(`Thinking update failed (${response.status})`);
    if (updates.get(agent.id) !== updateToken) return;
    await refresh(agent.machineId);
    if (updates !== thinkingUpdateTokens || updates.get(agent.id) !== updateToken) return;
    updates.delete(agent.id);
    thinkingOverrides.delete(agent.id);
    touchRender();
    renderDetail();
  } catch (error) {
    if (!ompDeckApi.authenticated() || updates !== thinkingUpdateTokens || updates.get(agent.id) !== updateToken) return;
    updates.delete(agent.id);
    thinkingOverrides.delete(agent.id);
    touchRender();
    renderDetail();
    showToast(error.message, 'error');
    void refresh(agent.machineId);
  } finally {
    settle();
  }
});
el.activity.addEventListener('click', event => {
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


const appRoot = document.getElementById('app');
const authScreen = document.getElementById('auth-screen');
const pairForm = document.getElementById('pair-form');
const pairToken = document.getElementById('pair-token');
const authError = document.getElementById('auth-error');
const authStatus = document.getElementById('auth-status');
const pairSubmit = document.getElementById('pair-submit');

function showAuthentication({clearToken = false} = {}) {
  stopEventStreams();
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
  el['detail-chips'].replaceChildren();
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
  appRoot.hidden = true;
  document.getElementById('pair-command').textContent = ompDeckApi.mode() === 'coordinator'
    ? 'omp-deck coordinator pair --config FILE'
    : 'omp-deck pair';
  authScreen.hidden = false;
  if (clearToken) pairToken.value = '';
  pairToken.focus();
}

async function showApplication() {
  authScreen.hidden = true;
  appRoot.hidden = false;
  document.getElementById('dashboard-title').focus();
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
  void refresh();
  void loadLaunchableProjects();
  void refreshUsage();
  if (!state.recoveryTimer) state.recoveryTimer = setInterval(refresh, 5 * 60_000);
}

pairForm.addEventListener('submit', async event => {
  event.preventDefault();
  authError.hidden = true;
  authStatus.textContent = 'Pairing…';
  pairSubmit.disabled = true;
  try {
    await ompDeckApi.pair(pairToken.value.trim());
    authStatus.textContent = 'Paired. Verifying secure session…';
    const session = await ompDeckApi.session();
    if (!session.authenticated) {
      throw new Error('Pairing succeeded, but the secure session was not retained. Open OMP Deck over HTTPS.');
    }
    if (ompDeckApi.mode() === 'coordinator') {
      authStatus.textContent = 'Paired. Reloading the authenticated dashboard…';
      location.reload();
      return;
    }
    await showApplication();
  } catch (error) {
    authStatus.textContent = '';
    authError.textContent = error.message;
    authError.hidden = false;
    pairToken.select();
  } finally {
    pairSubmit.disabled = false;
  }
});

window.addEventListener('omp-deck-auth-required', () => {
  void clearLocalData();
  showAuthentication();
});

document.addEventListener('DOMContentLoaded', async () => {
  try {
    const session = await ompDeckApi.session();
    if (session.authenticated) await showApplication();
    else showAuthentication({clearToken: true});
  } catch (error) {
    authError.textContent = error.message;
    authError.hidden = false;
    showAuthentication();
  }
}, {once: true});
