const DASHBOARD_CACHE_KEY = 'omp:dashboard:v1';
const USAGE_CACHE_KEY = 'omp:usage:v1';
try { localStorage.removeItem('omp:read-state:v1'); } catch {}
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
  renderRevision: 0,
  renderedRevision: -1,
  renderedSessionId: null,
  renderedUpdatedMs: null,
  usageAt: 0,
};
const el = Object.fromEntries([
  'connection', 'connection-text', 'working-count', 'attention-count', 'unread-count',
  'idle-count', 'usage-list', 'agent-list', 'app', 'workspace', 'detail',
  'detail-chips', 'activity', 'scroll-newest', 'composer', 'attachments', 'attach', 'gallery',
  'attachment-input', 'gallery-input', 'message', 'send', 'send-state', 'toast',
  'overflow-button', 'session-menu', 'command-menu', 'command-mode',
  'project-select', 'project-new', 'project-launch', 'launch-state'
].map(id => [id, document.getElementById(id)]));
function readCache(key) {
  try {
    return JSON.parse(localStorage.getItem(key));
  } catch {
    return null;
  }
}
function writeCache(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // The live request still works when storage is unavailable.
  }
}
function cacheAgents(agents) {
  const summaries = agents.map(agent => ({
    id: agent.id,
    label: agent.label,
    project: agent.project,
    cwd: agent.cwd,
    bridgeConnected: agent.bridgeConnected,
    status: agent.status,
    executionState: agent.executionState,
    inboxState: agent.inboxState,
    activitySummary: agent.activitySummary,
    subagents: agent.subagents || [],
    session: {
      updatedMs: agent.session?.updatedMs,
      responseAt: agent.session?.responseAt,
      responseId: agent.session?.responseId,
      model: agent.session?.model,
      thinking: agent.session?.thinking,
      source: agent.session?.source,
      activity: [],
      archiveActivity: [],
    },
  }));
  writeCache(DASHBOARD_CACHE_KEY, summaries);
}
function hydrateDashboard() {
  const agents = readCache(DASHBOARD_CACHE_KEY);
  if (Array.isArray(agents) && agents.length) {
    state.agents = agents;
    renderSummary();
    renderList();
  }
  const usage = readCache(USAGE_CACHE_KEY);
  if (usage) renderUsage(usage);
}
async function markRead(agent) {
  const eventId = agent?.session?.responseId;
  if (!eventId || agent.inboxState !== 'unread' || document.visibilityState !== 'visible') return;
  agent.inboxState = 'read';
  touchRender();
  renderSummary();
  renderList();
  renderDetail();
  try {
    await fetch('/api/read', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session: agent.id, eventId}),
    });
  } catch {
    agent.inboxState = 'unread';
    touchRender();
    renderSummary();
    renderList();
    renderDetail();
  }
}
function displayStatus(agent) {
  return agent.executionState || agent.status || 'idle';
}
function relativeTime(value) {
  if (!value) return 'now';
  const seconds = Math.max(0, Math.round((Date.now() - value) / 1000));
  if (seconds < 8) return 'now';
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h`;
}
function clockTime(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}
function selectedAgent() {
  return state.agents.find(agent => agent.id === state.selectedId) || null;
}
let composer;
function touchRender() {
  state.renderRevision += 1;
}
let toastTimer;
function showToast(message) {
  clearTimeout(toastTimer);
  el.toast.textContent = message;
  el.toast.hidden = false;
  toastTimer = setTimeout(() => { el.toast.hidden = true; }, 5000);
}
function matchesFilter(agent) {
  if (state.filter === 'all') return true;
  const status = displayStatus(agent);
  if (state.filter === 'attention') return ['needs-input', 'failed'].includes(status);
  if (state.filter === 'unread') return agent.inboxState === 'unread';
  return status === state.filter;
}
function renderSummary() {
  const statuses = state.agents.map(displayStatus);
  const working = statuses.filter(status => status === 'working').length;
  const attention = statuses.filter(status => ['needs-input', 'failed'].includes(status)).length;
  const unread = state.agents.filter(agent => agent.inboxState === 'unread').length;
  const idle = statuses.filter(status => status === 'idle').length;
  el['working-count'].textContent = working;
  el['attention-count'].textContent = attention;
  el['unread-count'].textContent = unread;
  el['idle-count'].textContent = idle;
}

function resetTime(value) {
  const seconds = Math.max(0, Math.round((value - Date.now()) / 1000));
  if (seconds < 60) return '<1m';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

function renderUsage(payload) {
  if (payload.error) {
    el['usage-list'].innerHTML = `<span class="usage-error">${escapeHtml(payload.error)}</span>`;
    return;
  }
  const limits = (payload.reports || []).flatMap(report =>
    (report.limits || []).map(limit => ({...limit, provider: report.provider}))
  );
  if (!limits.length) {
    el['usage-list'].replaceChildren();
    return;
  }
  el['usage-list'].innerHTML = limits.map(limit => {
    const used = Math.round((limit.amount?.usedFraction || 0) * 100);
    const reset = limit.window?.resetsAt ? ` · ${resetTime(limit.window.resetsAt)}` : '';
    return `<div class="usage-item">
      <div class="usage-label"><strong>${escapeHtml(limit.label)}</strong><span>${used}%${reset}</span></div>
      <div class="usage-bar"><div class="usage-fill" style="width:${Math.min(100, Math.max(0, used))}%"></div></div>
    </div>`;
  }).join('');
}

async function refreshUsage() {
  if (Date.now() - state.usageAt < 60000) return;
  state.usageAt = Date.now();
  try {
    const response = await fetch('/api/usage', {cache: 'no-store'});
    if (!response.ok) throw new Error(`Usage returned ${response.status}`);
    const payload = await response.json();
    renderUsage(payload);
    writeCache(USAGE_CACHE_KEY, payload);
  } catch (error) {
    if (!el['usage-list'].children.length) console.warn('Usage unavailable', error);
  }
}

function renderList() {
  const visible = state.agents.filter(matchesFilter);
  el['agent-list'].innerHTML = visible.length ? visible.map(agent => {
    const session = agent.session || {};
    const status = displayStatus(agent);
    const children = agent.subagents || [];
    const activeChildren = children.filter(subagent => subagent.executionState === 'working').length;
    const subagents = children.map(subagent =>
      `<span class="subagent"><i class="status-dot ${escapeHtml(subagent.executionState)}"></i><span><strong>${escapeHtml(subagent.label)}</strong><small>${escapeHtml(subagent.activitySummary || subagent.executionState)}</small></span></span>`
    ).join('');
    return `<button class="agent-card" data-id="${escapeHtml(agent.id)}" type="button">
      <span class="agent-card-top">
        <span class="status-stack"><i class="status-dot ${escapeHtml(status)}"></i>${agent.inboxState === 'unread' ? '<i class="inbox-dot" title="Unread response"></i>' : ''}</span>
        <span>
          <span class="agent-name">${escapeHtml(agent.project)}</span>
          <span class="agent-project">${escapeHtml(agent.label)}</span>
          <span class="activity-summary">${escapeHtml(agent.activitySummary || status.replace('-', ' '))}</span>
        </span>
        <span class="agent-age">${relativeTime(session.updatedMs)}</span>
      </span>
      ${subagents ? `<span class="subagent-progress">${children.length} subagent${children.length === 1 ? '' : 's'} · ${activeChildren} working · ${children.length - activeChildren} waiting or done</span><span class="subagents" aria-label="Subagents">${subagents}</span>` : ''}
    </button>`;
  }).join('') : '<div class="empty-card">No OMPs match this filter.</div>';
}

function renderDetail({selectionChanged = false} = {}) {
  const agent = selectedAgent();
  const session = agent?.session || {};
  const renderKey = `${session.updatedMs || ''}:${agent?.inboxState || ''}:${state.showArchive}`;
  if (!selectionChanged
      && state.renderedSessionId === agent?.id
      && state.renderedUpdatedMs === renderKey
      && state.renderedRevision === state.renderRevision) return;
  state.renderedSessionId = agent?.id || null;
  state.renderedUpdatedMs = renderKey;
  state.renderedRevision = state.renderRevision;
  const chips = [];
  if (session.todo) {
    const taskCount = TodoUI.parse(session.todo).reduce((count, phase) => count + phase.tasks.length, 0);
    chips.push(`<button class="task-toggle ${state.showTodo ? 'active' : ''}" type="button" data-toggle-todo aria-label="${taskCount} tasks" aria-expanded="${state.showTodo}" aria-controls="todo-panel"><span class="task-toggle-icon">☑</span><span>${taskCount}</span></button>`);
  }
  if (session.model) chips.push(`<span class="chip">${escapeHtml(session.model)}</span>`);
  if (agent) {
    const level = thinkingLevel(agent);
    chips.push(`<button class="chip" type="button" data-thinking-level="${escapeHtml(level)}">thinking: ${escapeHtml(level)}</button>`);
  }
  const executionState = displayStatus(agent || {});
  if (agent) {
    const interrupt = el['session-menu'].querySelector('[data-session-action="interrupt"]');
    interrupt.disabled = executionState !== 'working';
    interrupt.title = interrupt.disabled ? 'Available only while OMP is working' : '';
  }
  if (agent?.bridgeConnected === false || executionState === 'disconnected') {
    chips.push('<span class="bridge-warning" role="status">Disconnected · reconnect with /reload-plugins then /omp-deck</span>');
  }
  el['detail-chips'].innerHTML = chips.join('');

  const currentEvents = (session.activity || [])
    .filter(event => ['user', 'assistant', 'error'].includes(event.kind));
  const archivedEvents = state.showArchive
    ? (session.archiveActivity || []).filter(event => ['user', 'assistant', 'error'].includes(event.kind))
    : [];
  const events = [...currentEvents].reverse();
  const todoPanel = state.showTodo && session.todo
    ? TodoUI.render(session.todo, escapeHtml)
    : '';
  const archive = (session.archiveActivity || []).length
    ? `<div class="archive-separator"><button type="button" data-toggle-archive aria-expanded="${state.showArchive}">${state.showArchive ? 'Hide earlier history' : 'Show earlier history'}</button></div>${state.showArchive ? archivedEvents.reverse().map(event => `<article class="event archived ${escapeHtml(event.kind)}"><div class="event-head"><span class="event-title">${escapeHtml(event.title)}</span><time class="event-time">${escapeHtml(clockTime(event.time))}</time></div>${eventAttachments(event)}${renderEventDetail(event)}</article>`).join('') : ''}`
    : '';
  const pending = agent ? [...composer.pendingFor(agent.id)].reverse().map(item => ({
    kind: 'user pending',
    title: item.accepted ? 'You · Delivered live' : 'You · Delivering',
    detail: item.text,
    time: new Date(item.createdAt).toISOString(),
    attachments: item.attachments,
  })) : [];
  if (executionState === 'working' || pending.length) {
    const workingOn = pending.length ? 'Preparing a response' : (agent?.activitySummary || session.workingOn || 'Preparing a response');
    events.unshift({kind: 'thinking', title: `OMP · ${workingOn}`, time: null});
  } else if (executionState === 'needs-input') {
    events.unshift({kind: 'notice', title: 'OMP needs your input', detail: agent.activitySummary || 'Reply to continue.', time: null});
  } else if (executionState === 'failed') {
    events.unshift({kind: 'error', title: 'OMP failed', detail: agent.activitySummary || 'The last operation failed.', time: null});
  }
  const conversation = events.length ? events.map(event => `<article class="event ${escapeHtml(event.kind)}" ${event.id ? `data-event-id="${escapeHtml(event.id)}"` : ''}>
    <div class="event-head">
      <span class="event-title">${escapeHtml(event.title)}</span>
      <time class="event-time">${escapeHtml(clockTime(event.time))}</time>
    </div>
    ${event.kind === 'thinking'
      ? '<div class="typing-dots" aria-label="OMP is working"><i></i><i></i><i></i></div>'
      : `${eventAttachments(event)}${renderEventDetail(event)}`}
  </article>`).join('') : '<p class="activity-empty">No conversation available yet.</p>';
  el.activity.innerHTML = todoPanel + conversation + archive;
  el['scroll-newest'].hidden = el.activity.scrollTop < 160;

  if (selectionChanged) {
    composer.select(agent?.id);
    requestAnimationFrame(() => { el.activity.scrollTop = 0; });
  }
  const canMessage = Boolean(agent?.bridgeConnected);
  const composerEnabled = canMessage && !composer.isSending(agent?.id);
  el.message.setAttribute('aria-label', agent ? `Message ${agent.label}` : 'Message selected OMP');
  el.message.placeholder = agent && !canMessage ? 'Run /reload-plugins, then /omp-deck…' : 'Message this OMP…';
  el.message.disabled = !canMessage;
  el.attach.disabled = !canMessage;
  el.gallery.disabled = !canMessage;
  el.send.disabled = !composerEnabled;
}

function selectAgent(id, {pushHistory = true} = {}) {
  state.showArchive = false;
  state.selectedId = id;
  state.pendingSelection = null;
  el.app.classList.add('chat-open');
  document.body.classList.add('chat-open');
  el.workspace.classList.add('chat-open');
  if (pushHistory) {
    const url = new URL(location.href);
    url.searchParams.set('session', id);
    history.pushState({view: 'chat', session: id}, '', url);
  }
  renderDetail({selectionChanged: true});
  markRead(selectedAgent());
}

function showMenu() {
  state.selectedId = null;
  composer.select(null);
  el.app.classList.remove('chat-open');
  document.body.classList.remove('chat-open');
  el.workspace.classList.remove('chat-open');
  renderList();
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const response = await fetch('/api/agents', {cache: 'no-store'});
    if (!response.ok) throw new Error(`OMP Deck returned ${response.status}`);
    const payload = await response.json();
    state.agents = payload.agents || [];
    cacheAgents(state.agents);
    state.agents.forEach(composer.reconcile);
    if (state.pendingSelection) {
      const requested = state.agents.find(agent => agent.id === state.pendingSelection);
      if (requested) {
        selectAgent(requested.id, {pushHistory: false});
      } else {
        state.pendingSelection = null;
        const url = new URL(location.href);
        url.searchParams.delete('session');
        history.replaceState({view: 'menu'}, '', url);
        showMenu();
      }
    } else if (state.selectedId && !state.agents.some(agent => agent.id === state.selectedId)) {
      showMenu();
    }
    el.connection.classList.remove('offline');
    el['connection-text'].textContent = 'Live';
    renderSummary();
    renderList();
    refreshUsage();
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) renderDetail();
    if (state.selectedId) markRead(selectedAgent());
  } catch (error) {
    el.connection.classList.add('offline');
    el['connection-text'].textContent = 'Offline';
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
el['detail-chips'].addEventListener('click', event => {
  const todoButton = event.target.closest('[data-toggle-todo]');
  if (todoButton) {
    state.showTodo = !state.showTodo;
    touchRender();
    renderDetail();
    return;
  }
  const button = event.target.closest('[data-thinking-level]');
  const agent = selectedAgent();
  if (!button || !agent) return;
  const current = thinkingLevel(agent);
  const next = THINKING_LEVELS[(THINKING_LEVELS.indexOf(current) + 1) % THINKING_LEVELS.length];
  localStorage.setItem(thinkingKey(agent.id), next);
  touchRender();
  renderDetail();
});
el.activity.addEventListener('click', event => {
  const button = event.target.closest('[data-toggle-archive]');
  if (!button) return;
  state.showArchive = !state.showArchive;
  touchRender();
  renderDetail();
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


if (initialSession) {
  history.replaceState({view: 'chat', session: initialSession}, '', location.href);
} else {
  history.replaceState({view: 'menu'}, '', location.href);
}
hydrateDashboard();
refresh();
setInterval(refresh, 1000);
