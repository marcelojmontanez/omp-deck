try { localStorage.clear(); } catch {}
const thinkingOverrides = new Map();
const THINKING_LEVELS = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'auto'];
const initialSession = new URL(location.href).searchParams.get('session');
const state = {
  agents: [],
  selectedId: null,
  pendingSelection: initialSession,
  filter: 'all',
  showTodo: false,
  showArchive: false,
  refreshing: false,
  refreshPending: false,
  snapshotGeneration: 0,
  renderRevision: 0,
  renderedRevision: -1,
  renderedSessionId: null,
  renderedUpdatedMs: null,
  eventSource: null,
  recoveryTimer: null,
  eventRefreshTimer: null,
  eventReconnectTimer: null,
};

const el = Object.fromEntries([
  'connection', 'connection-text', 'working-count', 'attention-count', 'unread-count',
  'idle-count', 'agent-list', 'app', 'workspace', 'detail', 'detail-chips',
  'task-control', 'transport-status', 'pending-request', 'activity', 'scroll-newest',
  'composer', 'attachments', 'attach', 'gallery', 'attachment-input', 'gallery-input',
  'message', 'send', 'send-state', 'toast', 'overflow-button', 'session-menu',
  'project-select', 'project-new', 'project-launch', 'launch-state'
].map(id => [id, document.getElementById(id)]));
async function clearLocalData() {
  try { localStorage.clear(); } catch {}
  try { sessionStorage.clear(); } catch {}
  thinkingOverrides.clear();
  composer?.clearLocalData();
  clearPendingRequestAttempts();
  clearSessionActionAttempts();
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

async function refresh() {
  if (state.refreshing) {
    state.refreshPending = true;
    return;
  }
  state.refreshing = true;
  try {
    do {
      state.refreshPending = false;
      const requestedGeneration = state.snapshotGeneration;
      try {
        const response = await ompDeckApi.request('/api/sessions', {cache: 'no-store'});
        if (!response.ok) throw new Error(`OMP Deck returned ${response.status}`);
        applyFleetSnapshot(await response.json(), requestedGeneration);
      } catch {
        el.connection.classList.add('offline');
        el['connection-text'].textContent = 'Offline';
      }
    } while (state.refreshPending);
  } finally {
    state.refreshing = false;
  }
}


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
  if (!button || !agent?.bridgeConnected || !agent.capabilities.setThinking) return;
  const current = thinkingLevel(agent);
  const next = THINKING_LEVELS[(THINKING_LEVELS.indexOf(current) + 1) % THINKING_LEVELS.length];
  thinkingOverrides.set(agent.id, next);
  touchRender();
  renderDetail();
  try {
    const response = await ompDeckApi.request(`/api/sessions/${encodeURIComponent(agent.id)}/thinking`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({requestId: requestId(), level: next}),
    });
    if (!response.ok) throw new Error(`Thinking update failed (${response.status})`);
  } catch (error) {
    thinkingOverrides.set(agent.id, current);
    touchRender();
    renderDetail();
    showToast(error.message, 'error');
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

function showAuthentication() {
  state.eventSource?.close();
  state.eventSource = null;
  if (state.recoveryTimer) clearInterval(state.recoveryTimer);
  state.recoveryTimer = null;
  if (state.eventRefreshTimer) clearTimeout(state.eventRefreshTimer);
  state.eventRefreshTimer = null;
  if (state.eventReconnectTimer) clearTimeout(state.eventReconnectTimer);
  state.eventReconnectTimer = null;
  appRoot.hidden = true;
  authScreen.hidden = false;
  pairToken.value = '';
  pairToken.focus();
}

async function showApplication() {
  authScreen.hidden = true;
  appRoot.hidden = false;
  document.getElementById('dashboard-title').focus();
  if (initialSession) {
    history.replaceState({view: 'chat', session: initialSession}, '', location.href);
  } else {
    history.replaceState({view: 'menu'}, '', location.href);
  }
  await refresh();
  startEventStream();
  if (!state.recoveryTimer) state.recoveryTimer = setInterval(refresh, 5 * 60_000);
}

pairForm.addEventListener('submit', async event => {
  event.preventDefault();
  authError.hidden = true;
  try {
    await ompDeckApi.pair(pairToken.value.trim());
    await showApplication();
    await loadLaunchableProjects();
  } catch (error) {
    authError.textContent = error.message;
    authError.hidden = false;
    pairToken.select();
  }
});

window.addEventListener('omp-deck-auth-required', showAuthentication);

(async () => {
  try {
    const session = await ompDeckApi.session();
    if (session.authenticated) await showApplication();
    else showAuthentication();
  } catch (error) {
    authError.textContent = error.message;
    authError.hidden = false;
    showAuthentication();
  }
})();
