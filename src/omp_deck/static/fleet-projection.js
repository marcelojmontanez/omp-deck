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

function todosText(todos) {
  if (!Array.isArray(todos) || !todos.length) return '';
  const groups = new Map();
  for (const todo of todos) {
    const phase = String(todo.phase || todo.group || 'Tasks');
    if (!groups.has(phase)) groups.set(phase, []);
    groups.get(phase).push(todo);
  }
  return [...groups].map(([phase, items]) => `  ${phase}:\n${items.map(todo => {
    const done = todo.done === true || ['done', 'completed'].includes(todo.status);
    const suffix = todo.status === 'in_progress' ? ' (in progress)' : todo.status === 'blocked' ? ' (blocked)' : '';
    return `    - [${done ? 'X' : ' '}] ${todo.text || todo.title || todo.content || 'Task'}${suffix}`;
  }).join('\n')}`).join('\n');
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

function projectSession(session, machine = null) {
  const status = normalizedStatus(session.state);
  const updatedMs = Date.parse(session.updatedAt || '') || Date.now();
  const machineId = machine?.id || null;
  const localId = session.id;
  const projectedId = machineId ? ompDeckApi.sessionKey(machineId, localId) : localId;
  const thinkingLevels = Array.isArray(session.thinkingLevels)
    ? session.thinkingLevels.filter(level => typeof level === 'string' && level)
    : [];
  const activity = (session.messages || []).filter(message => (
    ['user', 'assistant'].includes(message?.role) && hasVisibleContent(message)
  )).map(message => {
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
  });
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
    pendingUiRequest: session.pendingUiRequest || null,
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
      todo: todosText(session.todos),
    },
  };
}
