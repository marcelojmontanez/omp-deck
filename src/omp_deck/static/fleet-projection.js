function requestId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function normalizedStatus(value) {
  return String(value || 'idle').replaceAll('_', '-');
}

const idleSubagentStatuses = new Set(['completed', 'done', 'finished', 'parked', 'yielded']);
const workSummaryStatuses = new Set(['attention', 'failed', 'working']);

function normalizedSubagentStatus(value) {
  const status = normalizedStatus(value);
  if (status === 'running') return 'working';
  if (idleSubagentStatuses.has(status)) return 'idle';
  return status;
}

function contentText(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .filter(item => item && item.type === 'text' && typeof item.text === 'string')
    .map(item => item.text)
    .join('\n');
}

function hasVisibleContent(message) {
  if (!Array.isArray(message?.content)) return false;
  return message.content.some(item => (
    item?.type === 'image'
    || (item?.type === 'text' && typeof item.text === 'string' && item.text.trim())
  ));
}

function projectMessage(message, machineId) {
  if (!['user', 'assistant'].includes(message?.role) || !hasVisibleContent(message)) return null;
  const images = Array.isArray(message.content)
    ? message.content.filter(item => item?.type === 'image').map(item => {
      const url = typeof item.url === 'string' ? item.url : null;
      return {url, machineId, unavailable: !url};
    })
    : [];
  return {
    id: message.id,
    kind: message.role,
    title: message.role === 'user' ? 'You' : 'OMP',
    detail: contentText(message.content),
    time: message.createdAt,
    attachments: images,
  };
}


function sessionDuplicateLabels(agents) {
  const groups = new Map();
  for (const agent of agents) {
    const key = JSON.stringify([agent.machineId, agent.project, agent.label]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(agent);
  }
  const labels = new Map();
  for (const group of groups.values()) {
    if (group.length < 2) continue;
    const kinds = new Map();
    for (const agent of group) {
      const label = agent.lifecycle === 'ongoing' ? 'Live session'
        : agent.lifecycle === 'history' ? 'Saved conversation' : 'Session status unavailable';
      if (!kinds.has(label)) kinds.set(label, []);
      kinds.get(label).push(agent);
    }
    for (const [label, peers] of kinds) {
      peers.sort((left, right) => {
        const leftId = String(left.localId || left.id);
        const rightId = String(right.localId || right.id);
        return leftId < rightId ? -1 : leftId > rightId ? 1 : 0;
      });
      peers.forEach((agent, index) => {
        labels.set(agent.id, peers.length === 1 ? label : `${label} ${index + 1} of ${peers.length}`);
      });
    }
  }
  return labels;
}

function sessionIdentity(agent) {
  const title = agent.label || 'Untitled conversation';
  const project = agent.project || 'Unknown project';
  const machine = agent.machineName || 'Unknown machine';
  return {
    title,
    project,
    machine,
    metadata: [`Project: ${project}`, `Machine: ${machine}`],
  };
}

function projectSession(session, machine = null, previous = null) {
  const status = normalizedStatus(session.state);
  const updatedMs = Date.parse(session.updatedAt || '') || Date.now();
  const machineId = machine?.id || null;
  const localId = session.id;
  const projectedId = machineId ? ompDeckApi.sessionKey(machineId, localId) : localId;
  const thinkingLevels = Array.isArray(session.thinkingLevels)
    ? session.thinkingLevels.filter(level => typeof level === 'string' && level)
    : [];
  const messageSource = Array.isArray(session.messages) ? session.messages : [];
  const completedActivity = previous?._messageSource === messageSource
    && Array.isArray(previous._completedActivity)
    ? previous._completedActivity
    : messageSource.map(message => projectMessage(message, machineId)).filter(Boolean);
  const streamingActivity = projectMessage(session.streamingMessage, machineId);
  const activity = streamingActivity
    && !completedActivity.some(item => item.id === streamingActivity.id)
    ? [...completedActivity, streamingActivity]
    : completedActivity;
  const activitySummary = session.pendingUiRequest
    ? 'Waiting for input'
    : workSummaryStatuses.has(status) && session.workingOn
      ? session.workingOn
      : status.replaceAll('-', ' ');
  const nativeTitle = typeof session.title === 'string' && session.title
    ? session.title
    : 'OMP session';
  const displayName = typeof session.displayName === 'string' && session.displayName
    ? session.displayName
    : null;
  return {
    id: projectedId,
    localId,
    machineId,
    machineName: machine?.name || '',
    machineOrigin: machine?.origin || '',
    lifecycle: ['ongoing', 'history'].includes(session.lifecycle) ? session.lifecycle : 'unknown',
    nodeAvailable: machine ? machine.status === 'live' : true,
    project: session.project?.name || 'Terminal',
    cwd: session.project?.path || '',
    label: displayName || nativeTitle,
    nativeLabel: nativeTitle,
    displayName,
    pinned: session.pinned === true,
    status,
    executionState: status,
    inboxState: session.unread ? 'unread' : 'read',
    bridgeConnected: session.transport?.status === 'connected',
    activitySummary,
    capabilities: session.capabilities || {},
    attachmentLimits: session.attachmentLimits || null,
    transport: session.transport || {kind: 'unknown', status: 'unknown', label: 'Transport unavailable', reason: null},
    transportReason: typeof session.transport?.reason === 'string' && session.transport.reason.trim()
      ? session.transport.reason
      : null,
    transportStatus: session.transport?.status || 'unknown',
    historyError: typeof session.historyError === 'string' && session.historyError.trim()
      ? session.historyError
      : null,
    transportLabel: session.transport?.label || 'Transport unavailable',
    deliveryError: session.deliveryError && typeof session.deliveryError === 'object'
      ? {
        nativeRequestId: typeof session.deliveryError.nativeRequestId === 'string'
          ? session.deliveryError.nativeRequestId : null,
        errorCode: typeof session.deliveryError.errorCode === 'string'
          ? session.deliveryError.errorCode : null,
        correlationAvailable: session.deliveryError.correlationAvailable === true,
        observedAt: typeof session.deliveryError.observedAt === 'string'
          ? session.deliveryError.observedAt : null,
      }
      : null,
    subagentError: typeof session.subagentError === 'string' ? session.subagentError : null,
    pendingUiRequest: session.pendingUiRequest || null,
    _messageSource: messageSource,
    _completedActivity: completedActivity,
    subagents: (session.subagents || []).map(item => {
      const executionState = normalizedSubagentStatus(item.executionState || item.status);
      return {
        id: item.id,
        label: item.label || item.displayName || item.name || item.id || 'Subagent',
        executionState,
        activitySummary: item.activitySummary || executionState,
      };
    }),
    session: {
      model: session.model ? [session.model.provider, session.model.id || session.model.name].filter(Boolean).join('/') : null,
      thinking: typeof session.thinking === 'string' && session.thinking ? session.thinking : null,
      thinkingLevels,
      updatedMs,
      workingOn: session.workingOn,
      activity,
      archiveActivity: [],
      todos: Array.isArray(session.todos) ? session.todos : [],
    },
  };
}

function createFleetMutations({state, touchRender, renderFleet, showMenu, getComposer}) {
  const MAX_CACHED_SESSIONS_PER_NODE = 500;

  function canonicalSessions(machineId) {
    const node = nodeState(machineId);
    if (!(node.sessions instanceof Map)) node.sessions = new Map();
    return node.sessions;
  }

  function rememberCanonicalSession(machineId, session) {
    if (!session || typeof session.id !== 'string' || !session.id) return false;
    const sessions = canonicalSessions(machineId);
    if (!sessions.has(session.id) && sessions.size >= MAX_CACHED_SESSIONS_PER_NODE) {
      sessions.delete(sessions.keys().next().value);
    }
    sessions.delete(session.id);
    sessions.set(session.id, session);
    return true;
  }

  function noteSnapshotGeneration(machineId, generation) {
    if (Number.isSafeInteger(generation)) nodeState(machineId).snapshotGeneration = generation;
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
      (Number.isSafeInteger(requestedGeneration)
        && node.snapshotGeneration !== requestedGeneration)
      || (Number.isSafeInteger(requestedRevision)
        && node.presentationRevision !== requestedRevision)
    ) return;
    const machine = ompDeckApi.machine(machineId);
    if (!machine) return;
    noteSnapshotGeneration(machineId, payload.snapshotGeneration);
    node.hasSnapshot = true;
    const sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
    node.sessions = new Map(sessions
      .filter(session => session && typeof session.id === 'string' && session.id)
      .slice(0, MAX_CACHED_SESSIONS_PER_NODE)
      .map(session => [session.id, session]));
    const projected = sessions.map(session => projectSession(session, machine));
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
    if (!machine || !ompDeckApi.authenticated()) return false;
    if (!rememberCanonicalSession(machineId, session)) return false;
    noteSnapshotGeneration(machineId, snapshotGeneration);
    if (!Number.isSafeInteger(snapshotGeneration)) nodeState(machineId).presentationRevision += 1;
    const projected = projectSession(session, machine);
    const index = state.agents.findIndex(agent => agent.id === projected.id);
    if (index < 0) state.agents.push(projected);
    else state.agents[index] = projected;
    touchRender();
    renderFleet();
    return true;
  }

  function applyFleetPatch(sessionId, patch, snapshotGeneration, machineId = 'local') {
    const machine = ompDeckApi.machine(machineId);
    if (
      !machine
      || !ompDeckApi.authenticated()
      || typeof sessionId !== 'string'
      || !sessionId
      || !patch
      || typeof patch !== 'object'
      || Array.isArray(patch)
    ) return false;
    const sessions = canonicalSessions(machineId);
    const current = sessions.get(sessionId);
    if (!current) return false;
    const merged = {...current, ...patch, id: sessionId};
    sessions.delete(sessionId);
    sessions.set(sessionId, merged);
    noteSnapshotGeneration(machineId, snapshotGeneration);
    if (!Number.isSafeInteger(snapshotGeneration)) nodeState(machineId).presentationRevision += 1;
    const id = ompDeckApi.sessionKey(machineId, sessionId);
    const index = state.agents.findIndex(agent => agent.id === id);
    const projected = projectSession(merged, machine, index < 0 ? null : state.agents[index]);
    if (index < 0) state.agents.push(projected);
    else state.agents[index] = projected;
    touchRender();
    renderFleet();
    return true;
  }

  function removeFleetSession(sessionId, snapshotGeneration, machineId = 'local') {
    canonicalSessions(machineId).delete(sessionId);
    noteSnapshotGeneration(machineId, snapshotGeneration);
    if (!Number.isSafeInteger(snapshotGeneration)) nodeState(machineId).presentationRevision += 1;
    const id = ompDeckApi.sessionKey(machineId, sessionId);
    ompDeckImages.releaseOwner(id);
    getComposer().remove(id);
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
    applyFleetSnapshot,
    applyFleetSession,
    applyFleetPatch,
    removeFleetSession,
  });
}
