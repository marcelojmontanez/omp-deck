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
    return agent?.executionState || agent?.status || 'unknown';
  }
  function lifecycle(agent) {
    return ['ongoing', 'history'].includes(agent?.lifecycle) ? agent.lifecycle : 'unknown';
  }
  function canOpen(agent) {
    return Boolean(agent && (state.showHistory || lifecycle(agent) !== 'history'));
  }
  function needsAttention(agent) {
    if (lifecycle(agent) === 'history') return false;
    return lifecycle(agent) === 'unknown'
      || Boolean(agent?.historyError)
      || agent?.nodeAvailable === false
      || !agent?.bridgeConnected
      || ['needs-input', 'failed', 'degraded', 'recovering', 'disconnected', 'unknown']
        .includes(displayStatus(agent));
  }
  function selectedAgent() {
    const agent = state.agents.find(candidate => candidate.id === state.selectedId) || null;
    return canOpen(agent) ? agent : null;
  }

  let detailRenderDeferred = false;
  function hasConversationSelection() {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) return false;
    return el.activity.contains(selection.anchorNode) || el.activity.contains(selection.focusNode);
  }
  document.addEventListener('selectionchange', () => {
    if (!detailRenderDeferred || hasConversationSelection()) return;
    renderDetail();
  });

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
    const agentLifecycle = lifecycle(agent);
    if (agentLifecycle === 'history') {
      return state.showHistory && state.sessionScope === 'history';
    }
    if (state.sessionScope === 'history') return false;
    if (state.filter === 'all') return true;
    const status = displayStatus(agent);
    if (state.filter === 'attention') return needsAttention(agent);
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
    const pinned = Number(right.pinned === true) - Number(left.pinned === true);
    if (pinned) return pinned;
    const unread = Number(left.inboxState !== 'unread') - Number(right.inboxState !== 'unread');
    if (unread) return unread;
    const status = (statusOrder.get(displayStatus(left)) ?? 9) - (statusOrder.get(displayStatus(right)) ?? 9);
    if (status) return status;
    const label = agentLabelOrder.compare(String(left.label || ''), String(right.label || ''));
    return label || String(left.id || '').localeCompare(String(right.id || ''));
  }
  function renderSummary() {
    if (!state.showHistory && state.sessionScope === 'history') state.sessionScope = 'ongoing';
    const ongoing = state.agents.filter(agent => lifecycle(agent) !== 'history');
    const statuses = ongoing.map(displayStatus);
    el['working-count'].textContent = statuses.filter(status => status === 'working').length;
    el['attention-count'].textContent = ongoing.filter(needsAttention).length;
    el['unread-count'].textContent = ongoing.filter(agent => agent.inboxState === 'unread').length;
    el['idle-count'].textContent = statuses.filter(status => status === 'idle').length;
    el['history-count'].textContent = state.sessionScope === 'history'
      ? ongoing.length : state.agents.length - ongoing.length;
    el['history-toggle'].hidden = !state.showHistory;
    el['history-toggle'].setAttribute('aria-pressed', String(state.sessionScope === 'history'));
    el['history-toggle'].lastChild.textContent = state.sessionScope === 'history' ? ' Ongoing' : ' History';
    el['session-scope-title'].textContent = state.sessionScope === 'history' ? 'History' : 'Ongoing';
    document.querySelector('.filters').hidden = state.sessionScope === 'history';
  }


  function renderList() {
    const focusedId = el['agent-list'].contains(document.activeElement)
      ? document.activeElement.closest('[data-id]')?.dataset.id : null;
    const visible = state.agents.filter(matchesFilter).sort(compareAgents);
    const duplicateLabels = state.showHistory ? sessionDuplicateLabels(state.agents) : null;
    el['agent-list'].innerHTML = visible.length ? visible.map(agent => {
      const session = agent.session || {};
      const agentLifecycle = lifecycle(agent);
      const status = agentLifecycle === 'history' ? 'history' : displayStatus(agent);
      const children = (agent.subagents || [])
        .filter(subagent => subagent.executionState === 'working');
      const subagents = children.map(subagent =>
        `<span class="subagent"><i class="status-dot ${escapeHtml(subagent.executionState)}"></i><span><strong>${escapeHtml(subagent.label)}</strong><small>${escapeHtml(subagent.activitySummary || subagent.executionState)}</small></span></span>`
      ).join('');
      const {title, project, machine, metadata} = sessionIdentity(agent);
      const duplicate = duplicateLabels?.get(agent.id) || '';
      const lifecycleNote = agentLifecycle === 'history'
        ? 'Saved conversation'
        : agentLifecycle === 'unknown'
          ? 'Session lifecycle unavailable'
          : agent.nodeAvailable === false
            ? 'Machine unavailable'
            : `${agent.activitySummary || status.replace('-', ' ')}${agent.historyError ? ' · History unavailable' : ''}`;
      const accessible = [
        title,
        ...metadata,
        duplicate,
        agent.pinned ? 'Pinned' : '',
        agent.inboxState === 'unread' ? 'Unread response' : '',
        lifecycleNote,
      ].filter(Boolean).join(', ');
      return `<button class="agent-card ${escapeHtml(agentLifecycle)}${agent.pinned ? ' pinned' : ''}" data-id="${escapeHtml(agent.id)}" type="button" aria-label="${escapeHtml(accessible)}">
        <span class="agent-card-top">
          <span class="status-stack"><i class="status-dot ${escapeHtml(status)}"></i>${agent.inboxState === 'unread' ? '<i class="inbox-dot" title="Unread response"></i>' : ''}</span>
          <span>
            <span class="agent-name">${escapeHtml(title)}</span>
            <span class="agent-project">${escapeHtml(`Project: ${project}`)}</span>
            <span class="agent-machine-line">${escapeHtml(`Machine: ${machine}`)}</span>
            ${duplicate ? `<span class="agent-disambiguator">${escapeHtml(duplicate)}</span>` : ''}
            ${agentLifecycle !== 'history' ? `<span class="activity-summary">${escapeHtml(lifecycleNote)}</span>` : ''}
          </span>
          <span class="agent-card-side">${agent.pinned ? '<span class="pin-badge" title="Pinned" aria-hidden="true">Pinned</span>' : ''}<span class="agent-age">${relativeTime(session.updatedMs)}</span></span>
        </span>
        ${subagents ? `<span class="subagent-progress">${children.length} working subagent${children.length === 1 ? '' : 's'}</span><span class="subagents" aria-label="Working subagents">${subagents}</span>` : ''}
      </button>`;
    }).join('') : `<div class="empty-card">${state.sessionScope === 'history' ? 'No saved conversation history.' : 'No ongoing OMPs match this filter.'}</div>`;
    if (focusedId) {
      el['agent-list'].querySelector(`[data-id="${CSS.escape(focusedId)}"]`)?.focus({preventScroll: true});
    }
  }

  function renderDetail({selectionChanged = false} = {}) {
    const agent = selectedAgent();
    if (!selectionChanged
        && state.renderedSessionId === agent?.id
        && hasConversationSelection()) {
      detailRenderDeferred = true;
      return;
    }
    detailRenderDeferred = false;
    const session = agent?.session || {};
    const renderKey = `${session.updatedMs || ''}:${agent?.inboxState || ''}:${agent?.historyError || ''}:${state.showArchive}:${navigator.onLine}`;
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
    const focusedControl = document.activeElement?.matches('[data-thinking-level]')
      ? '[data-thinking-level]' : document.activeElement?.matches('[data-toggle-todo]') ? '[data-toggle-todo]' : null;
    const chips = [];
    const notices = [];
    const capabilities = agent?.capabilities || {};
    const agentLifecycle = lifecycle(agent);
    const isLiveSession = agentLifecycle === 'ongoing';
    let thinkingDescription = 'Unavailable';
    if (session.model) chips.push(`<span class="chip model-chip" title="${escapeHtml(session.model)}">${escapeHtml(session.model)}</span>`);
    if (agent) {
      const choices = thinkingChoices(agent);
      const level = thinkingLevel(agent);
      const label = level || 'unavailable';
      const editable = isLiveSession && agent.nodeAvailable !== false && agent.bridgeConnected
        && capabilities.setThinking && choices.length > 0;
      const thinkingReason = typeof agent.transportReason === 'string'
        ? agent.transportReason.trim().slice(0, 160) : '';
      const unavailable = thinkingReason
        ? `Thinking controls unavailable: ${thinkingReason}`
        : 'Thinking controls are unavailable for this session';
      thinkingDescription = editable ? label : `${label} · ${thinkingReason || 'control unavailable'}`;
      if (level || isLiveSession) chips.push(editable
        ? `<button class="chip" type="button" data-thinking-level="${escapeHtml(label)}" aria-label="${escapeHtml(`Change thinking level; current ${label}`)}">thinking: ${escapeHtml(label)}</button>`
        : `<span class="chip read-only" title="${escapeHtml(unavailable)}" aria-label="${escapeHtml(`Thinking ${label}; read-only`)}">thinking: ${escapeHtml(label)}</span>`);
    }
    const executionState = displayStatus(agent || {});
    el.detail.setAttribute('aria-label', agent ? `${agent.label} conversation` : 'Conversation');
    for (const {action, enabled} of sessionActionOptions(agent)) {
      const button = el['session-menu'].querySelector(`[data-session-action="${action}"]`);
      button.disabled = !enabled;
      button.hidden = !enabled;
    }
    const presentationAvailable = Boolean(agent && agent.nodeAvailable !== false);
    const renameButton = el['session-menu'].querySelector('[data-session-action="rename"]');
    renameButton.hidden = !agent;
    renameButton.disabled = !presentationAvailable;
    const pinButton = el['session-menu'].querySelector('[data-session-action="pin"]');
    pinButton.hidden = !agent;
    pinButton.disabled = !presentationAvailable;
    pinButton.textContent = agent?.pinned ? 'Unpin session' : 'Pin session';
    const removeButton = el['session-menu'].querySelector('[data-session-action="remove"]');
    const removable = Boolean(agent?.nodeAvailable !== false && capabilities.remove);
    removeButton.hidden = !removable;
    removeButton.disabled = !removable;
    const transportConnected = Boolean(
      isLiveSession && agent?.nodeAvailable !== false && agent?.bridgeConnected
    );
    const transportLabel = agentLifecycle === 'history'
      ? 'History · no live session connection'
      : agentLifecycle === 'unknown'
        ? 'Session lifecycle unavailable'
        : agent?.nodeAvailable === false
          ? 'Machine unavailable'
          : transportConnected
            ? (agent.transport?.label || 'Session connected')
            : ['recovering', 'reconnecting', 'connecting'].includes(agent.transport?.status)
              ? 'Session connection recovering'
              : 'Session connection degraded';
    el['transport-status'].className = `transport-status ${transportConnected ? 'connected' : 'disconnected'}`;
    el['transport-status'].title = transportLabel;
    el['transport-status'].dataset.transport = agent?.transport?.kind || '';
    el['transport-status'].dataset.lifecycle = agentLifecycle;
    el['transport-status'].querySelector('span').textContent = transportLabel;
    if (agentLifecycle === 'history') {
      notices.push('<span class="history-notice">Saved conversation · read-only</span>');
    } else if (!transportConnected) {
      const safeReason = typeof agent?.transportReason === 'string'
        ? agent.transportReason.slice(0, 160)
        : '';
      notices.push(`<span class="bridge-warning" role="status">${escapeHtml(safeReason ? `${transportLabel}: ${safeReason}` : transportLabel)}</span>`);
    }
    if (isLiveSession && agent?.historyError) {
      const safeReason = String(agent.historyError).slice(0, 240);
      notices.push(`<span class="bridge-warning history-warning" role="status">${escapeHtml(`Conversation history could not synchronize. ${safeReason}. Restart this OMP when idle and resume the session to load an updated extension.`)}</span>`);
    }
    if (agent?.deliveryError) {
      const errorCode = typeof agent.deliveryError.errorCode === 'string'
        ? agent.deliveryError.errorCode.trim().slice(0, 80)
        : '';
      const message = 'Native prompt failed after acceptance. '
        + 'Exact delivery cannot be identified by the native API.'
        + (errorCode ? ` Error: ${errorCode}.` : '');
      notices.push(`<span class="bridge-warning delivery-warning" role="status">${escapeHtml(message)}</span>`);
    }
    if (agent?.subagentError) {
      notices.push(`<span class="bridge-warning" role="status">${escapeHtml(agent.subagentError)}</span>`);
    }
    el.detail.querySelector('.detail-notices').innerHTML = notices.join('');
    const metadata = [
      ['Name', agent?.label],
      ['Default name', agent?.displayName ? agent.nativeLabel : null],
      ['Project', agent?.project],
      ['Machine', agent?.machineName],
      ['Directory', agent?.cwd],
      ['Model', session.model],
      ['Thinking', thinkingDescription],
      ['Connection', transportLabel],
      ['Status', agentLifecycle === 'history' ? 'Saved conversation' : executionState.replaceAll('-', ' ')],
    ];
    document.getElementById('session-metadata').innerHTML = agent
      ? metadata.filter(([, value]) => value).map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join('')
      : '';
    const taskCount = TodoUI.count(session.todos);
    const taskControl = taskCount
      ? `<button class="task-toggle ${state.showTodo ? 'active' : ''}" type="button" data-toggle-todo aria-label="${taskCount} tasks" aria-expanded="${state.showTodo}" aria-controls="todo-panel"><span class="task-toggle-icon">☑</span><span>${taskCount}</span></button>`
      : '';
    el['task-control'].innerHTML = taskControl;
    el['detail-chips'].innerHTML = chips.join('');
    if (focusedControl) el.detail.querySelector(focusedControl)?.focus({preventScroll: true});
    if (isLiveSession) renderPendingRequest(agent);
    else renderPendingRequest(null);

    const currentEvents = (session.activity || [])
      .filter(event => ['user', 'assistant', 'error'].includes(event.kind));
    const archivedEvents = state.showArchive
      ? (session.archiveActivity || []).filter(event => ['user', 'assistant', 'error'].includes(event.kind))
      : [];
    const events = [...currentEvents].reverse();
    const todoPanel = state.showTodo && session.todos?.length
      ? TodoUI.render(session.todos, escapeHtml)
      : '';
    const archive = (session.archiveActivity || []).length
      ? `<div class="archive-separator"><button type="button" data-toggle-archive aria-expanded="${state.showArchive}">${state.showArchive ? 'Hide earlier history' : 'Show earlier history'}</button></div>${state.showArchive ? archivedEvents.reverse().map(event => `<article class="event archived ${escapeHtml(event.kind)}"><div class="event-head"><span class="event-title">${escapeHtml(event.title)}</span><time class="event-time">${escapeHtml(clockTime(event.time))}</time></div>${eventAttachments(event)}${renderEventDetail(event)}</article>`).join('') : ''}`
      : '';
    const pending = isLiveSession && agent
      ? [...getComposer().pendingFor(agent.id)].reverse().map(deliveryEvent).filter(Boolean)
      : [];
    events.unshift(...pending);
    if (isLiveSession && executionState === 'working') {
      const workingOn = agent?.activitySummary || session.workingOn || 'Preparing a response';
      events.unshift({kind: 'thinking', title: `OMP · ${workingOn}`, time: null});
    } else if (isLiveSession && executionState === 'needs-input') {
      events.unshift({kind: 'notice', title: 'OMP needs your input', detail: agent.activitySummary || 'Reply to continue.', time: null});
    } else if (isLiveSession && executionState === 'failed') {
      events.unshift({kind: 'error', title: 'OMP failed', detail: agent.activitySummary || 'The last operation failed.', time: null});
    }
    const conversation = events.length ? events.map(event => `<article class="event ${escapeHtml(event.kind)}" ${event.id ? `data-event-id="${escapeHtml(event.id)}"` : ''}>
      <div class="event-head">
        <span class="event-title">${escapeHtml(event.title)}</span>
        <time class="event-time">${escapeHtml(clockTime(event.time))}</time>
      </div>
      ${event.kind === 'thinking'
        ? '<div class="typing-dots" aria-label="OMP is working"><i></i><i></i><i></i></div>'
        : `${eventAttachments(event)}${renderEventDetail(event)}${deliveryControls(event)}`}
    </article>`).join('') : `<p class="activity-empty">${agentLifecycle === 'history' ? 'No conversation content retained for this session.' : 'No conversation available yet.'}</p>`;
    el.activity.innerHTML = todoPanel + conversation + archive;
    if (agent) ompDeckImages.hydrate(el.activity, agent.id);
    el['scroll-newest'].hidden = el.activity.scrollTop < 160;

    if (selectionChanged) {
      getComposer().select(agent?.id);
      if (agent) void getComposer().recover(agent);
      requestAnimationFrame(() => { el.activity.scrollTop = 0; });
    }
    const canCompose = Boolean(isLiveSession && capabilities.sendMessage);
    const canMessage = Boolean(
      canCompose
      && navigator.onLine !== false
      && agent?.nodeAvailable !== false
      && agent?.bridgeConnected
    );
    const canAttach = canCompose && Boolean(capabilities.attachments && agent?.attachmentLimits);
    el.composer.hidden = !isLiveSession;
    el.message.setAttribute('aria-label', agent ? `Message ${agent.label}` : 'Message selected OMP');
    el.message.placeholder = agentLifecycle === 'history'
      ? 'History is read-only'
      : canCompose && !canMessage ? 'Draft now; send when reconnected…'
        : agent && !canCompose ? 'Messaging is unavailable for this session' : 'Message this OMP…';
    el.message.disabled = !isLiveSession;
    el.attach.disabled = !canAttach;
    el.attach.title = canAttach ? `Up to ${agent.attachmentLimits.maxImages} images, ${formatImageLimit(agent.attachmentLimits.maxTotalBytes)} total` : 'Image uploads are unavailable in this session.';
    const attachmentStatus = getComposer().attachmentStatus(agent?.id);
    el['attachment-status'].textContent = attachmentStatus
      || (canAttach ? agent.attachmentLimits.reason || el.attach.title : el.attach.title);
    el['attachment-status'].hidden = !canCompose
      || (!attachmentStatus && canAttach && !agent.attachmentLimits.reason);
    el.send.disabled = !canMessage;
    getComposer().syncDeliveryMode(agent);
    getComposer().syncDraftSaving(agent);
  }

  function selectAgent(id, {pushHistory = true} = {}) {
    const agent = state.agents.find(candidate => candidate.id === id);
    if (!canOpen(agent)) return false;
    if (state.selectedId !== id) state.navigationRevision += 1;
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
    markRead(agent);
    return true;
  }

  function showMenu() {
    if (state.selectedId) state.navigationRevision += 1;
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
    renderPendingRequest(selectedAgent());
    if (state.pendingSelection) {
      const requested = state.agents.find(agent => agent.id === state.pendingSelection);
      if (canOpen(requested)) {
        selectAgent(requested.id, {pushHistory: false});
      } else if (
        requested
        || ompDeckApi.machines().every(machine =>
          ['offline', 'unavailable'].includes(machine.status)
          || nodeState(machine.id).hasSnapshot
        )
      ) {
        state.pendingSelection = null;
        const url = new URL(location.href);
        url.searchParams.delete('session');
        history.replaceState({view: 'menu'}, '', url);
        showMenu();
      }
    } else {
      const selected = state.agents.find(agent => agent.id === state.selectedId);
      if (state.selectedId && !canOpen(selected)) {
        const url = new URL(location.href);
        url.searchParams.delete('session');
        history.replaceState({view: 'menu'}, '', url);
        showMenu();
      }
    }
    renderMachineControls();
    renderSummary();
    renderList();
    renderDetail();
    if (state.selectedId) markRead(selectedAgent());
  }

  const {
    applyFleetSnapshot,
    applyFleetSession,
    applyFleetPatch,
    removeFleetSession,
  } = createFleetMutations({state, touchRender, renderFleet, showMenu, getComposer});


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
    applyFleetPatch,
    removeFleetSession,
  });
}
