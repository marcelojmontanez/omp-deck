function requestId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function normalizedStatus(value) {
  return String(value || 'idle').replaceAll('_', '-');
}

function contentText(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .filter(item => item && item.type === 'text' && typeof item.text === 'string')
    .map(item => item.text)
    .join('\n');
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

function projectSession(session) {
  const status = normalizedStatus(session.state);
  const updatedMs = Date.parse(session.updatedAt || '') || Date.now();
  const activity = (session.messages || []).map(message => {
    const images = Array.isArray(message.content)
      ? message.content.filter(item => item?.type === 'image' && typeof item.url === 'string')
        .map(item => ({url: item.url}))
      : [];
    return {
      id: message.id,
      kind: ['user', 'assistant'].includes(message.role) ? message.role : 'notice',
      title: message.role === 'user' ? 'You' : message.role === 'assistant' ? 'OMP' : String(message.role || 'OMP'),
      detail: contentText(message.content),
      time: message.createdAt,
      attachments: images,
    };
  });
  return {
    id: session.id,
    project: session.project?.name || 'Terminal',
    cwd: session.project?.path || '',
    label: session.title || 'OMP session',
    status,
    executionState: status,
    inboxState: session.unread ? 'unread' : 'read',
    bridgeConnected: session.transport?.status === 'connected',
    activitySummary: session.workingOn || (session.pendingUiRequest ? 'Waiting for input' : status.replace('-', ' ')),
    capabilities: session.capabilities || {},
    transport: session.transport || {kind: 'unknown', status: 'disconnected', label: 'Disconnected'},
    pendingUiRequest: session.pendingUiRequest || null,
    subagents: (session.subagents || []).map(item => ({
      id: item.id,
      label: item.label || item.displayName || item.name || item.id || 'Subagent',
      executionState: normalizedStatus(item.executionState || item.status),
      activitySummary: item.activitySummary || item.status || '',
    })),
    session: {
      model: session.model ? [session.model.provider, session.model.id || session.model.name].filter(Boolean).join('/') : null,
      thinking: session.thinking,
      updatedMs,
      workingOn: session.workingOn,
      activity,
      archiveActivity: [],
      todo: todosText(session.todos),
    },
  };
}
