'use strict';

function createDeckRendering({state, el, thinkingOverrides, getComposer}) {
  async function markRead(agent) {
    if (!agent || agent.nodeAvailable === false || agent.inboxState !== 'unread' || document.visibilityState !== 'visible') return;
    agent.inboxState = 'read';
    touchRender();
    renderSummary();
    renderList();
    renderDetail();
    try {
      const response = await ompDeckApi.request(`/api/sessions/${encodeURIComponent(agent.id)}/read`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({requestId: requestId()}),
      });
      if (!response.ok) throw new Error(`Read cursor returned ${response.status}`);
    } catch {
      agent.inboxState = 'unread';
      touchRender();
      renderSummary();
      renderList();
      renderDetail();
    }
  }
  function displayStatus(agent) {
    if (agent?.nodeAvailable === false) return 'disconnected';
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

  function touchRender() {
    state.renderRevision += 1;
  }
  let toastTimer;
  function showToast(message, tone = 'neutral') {
    clearTimeout(toastTimer);
    el.toast.textContent = message;
    el.toast.className = `toast ${['success', 'error'].includes(tone) ? tone : 'neutral'}`;
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
  const statusOrder = new Map([
    ['needs-input', 0],
    ['attention', 1],
    ['failed', 1],
    ['working', 2],
    ['idle', 3],
    ['unknown', 4],
    ['degraded', 5],
    ['disconnected', 6],
  ]);
  const agentLabelOrder = new Intl.Collator(undefined, {sensitivity: 'base'});
  function compareAgents(left, right) {
    const unread = Number(left.inboxState !== 'unread') - Number(right.inboxState !== 'unread');
    if (unread) return unread;
    const status = (statusOrder.get(displayStatus(left)) ?? 9) - (statusOrder.get(displayStatus(right)) ?? 9);
    if (status) return status;
    const label = agentLabelOrder.compare(String(left.label || ''), String(right.label || ''));
    return label || String(left.id || '').localeCompare(String(right.id || ''));
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


  function renderList() {
    const visible = state.agents.filter(matchesFilter).sort(compareAgents);
    el['agent-list'].innerHTML = visible.length ? visible.map(agent => {
      const session = agent.session || {};
      const status = agent.nodeAvailable === false ? 'disconnected' : displayStatus(agent);
      const children = (agent.subagents || [])
        .filter(subagent => subagent.executionState === 'working');
      const subagents = children.map(subagent =>
        `<span class="subagent"><i class="status-dot ${escapeHtml(subagent.executionState)}"></i><span><strong>${escapeHtml(subagent.label)}</strong><small>${escapeHtml(subagent.activitySummary || subagent.executionState)}</small></span></span>`
      ).join('');
      const owner = agent.machineName
        ? `<span class="agent-machine">${escapeHtml(agent.machineName)}</span>`
        : '';
      return `<button class="agent-card" data-id="${escapeHtml(agent.id)}" type="button" aria-label="${escapeHtml(`${agent.project}, ${agent.label}${agent.machineName ? `, on ${agent.machineName}` : ''}`)}">
        <span class="agent-card-top">
          <span class="status-stack"><i class="status-dot ${escapeHtml(status)}"></i>${agent.inboxState === 'unread' ? '<i class="inbox-dot" title="Unread response"></i>' : ''}</span>
          <span>
            <span class="agent-name">${escapeHtml(agent.project)}</span>
            ${owner}
            <span class="agent-project">${escapeHtml(agent.label)}</span>
            <span class="activity-summary">${escapeHtml(agent.nodeAvailable === false ? 'Machine unavailable' : (agent.activitySummary || status.replace('-', ' ')))}</span>
          </span>
          <span class="agent-age">${relativeTime(session.updatedMs)}</span>
        </span>
        ${subagents ? `<span class="subagent-progress">${children.length} working subagent${children.length === 1 ? '' : 's'}</span><span class="subagents" aria-label="Working subagents">${subagents}</span>` : ''}
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
    const previousRenderedId = state.renderedSessionId;
    state.renderedSessionId = agent?.id || null;
    state.renderedUpdatedMs = renderKey;
    state.renderedRevision = state.renderRevision;
    if (previousRenderedId && previousRenderedId !== agent?.id) {
      ompDeckImages.releaseOwner(previousRenderedId);
    }
    const chips = [];
    const capabilities = agent?.capabilities || {};
    if (agent?.machineName) chips.push(`<span class="chip machine-chip">${escapeHtml(agent.machineName)}</span>`);
    if (session.model) chips.push(`<span class="chip">${escapeHtml(session.model)}</span>`);
    if (agent) {
      const level = thinkingLevel(agent);
      chips.push(agent.nodeAvailable !== false && agent.bridgeConnected && capabilities.setThinking
        ? `<button class="chip" type="button" data-thinking-level="${escapeHtml(level)}">thinking: ${escapeHtml(level)}</button>`
        : `<span class="chip">thinking: ${escapeHtml(level)}</span>`);
    }
    const executionState = displayStatus(agent || {});
    const menuCapabilities = {clear: 'compact', interrupt: 'interrupt', quit: 'shutdown'};
    for (const [action, capability] of Object.entries(menuCapabilities)) {
      const button = el['session-menu'].querySelector(`[data-session-action="${action}"]`);
      const available = Boolean(agent?.nodeAvailable !== false && agent?.bridgeConnected && capabilities[capability]);
      button.disabled = !available || (action === 'interrupt' && executionState !== 'working');
      button.title = button.disabled ? `${action} is unavailable for this session` : '';
    }
    const removeButton = el['session-menu'].querySelector('[data-session-action="remove"]');
    const removable = Boolean(agent?.nodeAvailable !== false && capabilities.remove);
    removeButton.hidden = !removable;
    removeButton.disabled = !removable;
    const transportConnected = Boolean(agent?.nodeAvailable !== false && agent?.bridgeConnected);
    const transportLabel = transportConnected
      ? agent.transport.kind === 'rpc'
        ? 'RPC session'
        : agent.transport.kind === 'extension'
          ? 'Terminal session via extension'
          : agent.transport.kind === 'collab'
            ? 'Terminal session via Collab'
            : 'Connected session'
      : 'Disconnected';
    el['transport-status'].className = `transport-status ${transportConnected ? escapeHtml(agent.transport.kind) : 'disconnected'}`;
    el['transport-status'].title = transportLabel;
    el['transport-status'].querySelector('span').textContent = transportLabel;
    if (agent?.nodeAvailable === false) {
      const machine = ompDeckApi.machine(agent.machineId);
      const warning = machine?.status === 'unavailable'
        ? 'Machine access unavailable'
        : machine?.status === 'connecting' ? 'Machine connecting' : 'Machine offline';
      chips.push(`<span class="bridge-warning" role="status">${escapeHtml(warning)}</span>`);
    } else if (agent?.bridgeConnected === false || executionState === 'disconnected') {
      chips.push('<span class="bridge-warning" role="status">Disconnected</span>');
    }
    let taskControl = '';
    if (session.todo) {
      const taskCount = TodoUI.parse(session.todo).reduce((count, phase) => count + phase.tasks.length, 0);
      taskControl = `<button class="task-toggle ${state.showTodo ? 'active' : ''}" type="button" data-toggle-todo aria-label="${taskCount} tasks" aria-expanded="${state.showTodo}" aria-controls="todo-panel"><span class="task-toggle-icon">☑</span><span>${taskCount}</span></button>`;
    }
    el['task-control'].innerHTML = taskControl;
    el['detail-chips'].innerHTML = chips.join('');
    renderPendingRequest(agent);

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
    const pending = agent ? [...getComposer().pendingFor(agent.id)].reverse().map(item => ({
      kind: 'user pending',
      title: item.accepted ? 'You · Delivered live' : 'You · Delivering',
      detail: item.text,
      time: new Date(item.createdAt).toISOString(),
      attachments: item.attachments,
    })) : [];
    events.unshift(...pending);
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
    if (agent) ompDeckImages.hydrate(el.activity, agent.id);
    el['scroll-newest'].hidden = el.activity.scrollTop < 160;

    if (selectionChanged) {
      getComposer().select(agent?.id);
      requestAnimationFrame(() => { el.activity.scrollTop = 0; });
    }
    const canMessage = Boolean(agent?.nodeAvailable !== false && agent?.bridgeConnected && capabilities.sendMessage);
    const canAttach = canMessage && Boolean(capabilities.attachments);
    el.message.setAttribute('aria-label', agent ? `Message ${agent.label}` : 'Message selected OMP');
    el.message.placeholder = agent && !canMessage ? 'Messaging is unavailable for this session' : 'Message this OMP…';
    el.message.disabled = !canMessage;
    el.attach.disabled = !canAttach;
    el.gallery.disabled = !canAttach;
    el.attach.title = canAttach ? '' : 'Attachments are unavailable for this session';
    el.gallery.title = canAttach ? '' : 'Attachments are unavailable for this session';
    el.send.disabled = !canMessage;
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
    if (state.selectedId) ompDeckImages.releaseOwner(state.selectedId);
    state.selectedId = null;
    getComposer().select(null);
    el.app.classList.remove('chat-open');
    document.body.classList.remove('chat-open');
    el.workspace.classList.remove('chat-open');
    renderList();
  }

  function renderFleet() {
    state.agents.forEach(getComposer().reconcile);
    getComposer().prune(state.agents.map(agent => agent.id));
    if (state.pendingSelection) {
      const requested = state.agents.find(agent => agent.id === state.pendingSelection);
      if (requested) {
        selectAgent(requested.id, {pushHistory: false});
      } else if (ompDeckApi.machines().every(machine => ['live', 'offline', 'unavailable'].includes(machine.status))) {
        state.pendingSelection = null;
        const url = new URL(location.href);
        url.searchParams.delete('session');
        history.replaceState({view: 'menu'}, '', url);
        showMenu();
      }
    } else if (state.selectedId && !state.agents.some(agent => agent.id === state.selectedId)) {
      const url = new URL(location.href);
      url.searchParams.delete('session');
      history.replaceState({view: 'menu'}, '', url);
      showMenu();
    }
    renderMachineControls();
    renderSummary();
    renderList();
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) renderDetail();
    if (state.selectedId) markRead(selectedAgent());
  }

  function noteSnapshotGeneration(machineId, generation) {
    if (!Number.isSafeInteger(generation)) return;
    const node = nodeState(machineId);
    node.snapshotGeneration = generation;
  }

  function applyFleetSnapshot(
    payload,
    requestedGeneration = null,
    machineId = 'local',
    requestedRevision = null,
  ) {
    if (!ompDeckApi.authenticated()) return;
    const node = nodeState(machineId);
    if (
      Number.isSafeInteger(requestedGeneration)
      && node.snapshotGeneration !== requestedGeneration
    ) return;
    if (
      Number.isSafeInteger(requestedRevision)
      && node.presentationRevision !== requestedRevision
    ) return;
    const machine = ompDeckApi.machine(machineId);
    if (!machine) return;
    noteSnapshotGeneration(machineId, payload.snapshotGeneration);
    const projected = (payload.sessions || []).map(session => projectSession(session, machine));
    const projectedIds = new Set(projected.map(agent => agent.id));
    for (const agent of state.agents) {
      if (agent.machineId === machineId && !projectedIds.has(agent.id)) {
        ompDeckImages.releaseOwner(agent.id);
      }
    }
    state.agents = [
      ...state.agents.filter(agent => agent.machineId !== machineId),
      ...projected,
    ];
    touchRender();
    renderFleet();
  }

  function applyFleetSession(session, snapshotGeneration, machineId = 'local') {
    const machine = ompDeckApi.machine(machineId);
    if (!machine) return;
    if (!ompDeckApi.authenticated()) return;
    noteSnapshotGeneration(machineId, snapshotGeneration);
    if (!Number.isSafeInteger(snapshotGeneration)) nodeState(machineId).presentationRevision += 1;
    const projected = projectSession(session, machine);
    const index = state.agents.findIndex(agent => agent.id === projected.id);
    if (index < 0) state.agents.push(projected);
    else state.agents[index] = projected;
    touchRender();
    renderFleet();
  }

  function removeFleetSession(sessionId, snapshotGeneration, machineId = 'local') {
    noteSnapshotGeneration(machineId, snapshotGeneration);
    if (!Number.isSafeInteger(snapshotGeneration)) nodeState(machineId).presentationRevision += 1;
    const id = ompDeckApi.sessionKey(machineId, sessionId);
    ompDeckImages.releaseOwner(id);
    state.agents = state.agents.filter(agent => agent.id !== id);
    if (state.selectedId === id) {
      state.selectedId = null;
      const url = new URL(location.href);
      url.searchParams.delete('session');
      history.replaceState({view: 'menu'}, '', url);
      showMenu();
    }
    touchRender();
    renderFleet();
  }

  return Object.freeze({
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
  });
}
