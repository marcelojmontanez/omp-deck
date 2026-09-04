function scheduleEventRecovery() {
  if (state.eventRefreshTimer) return;
  state.eventRefreshTimer = setTimeout(() => {
    state.eventRefreshTimer = null;
    void refresh();
  }, 100);
}

function startEventStream() {
  state.eventSource?.close();
  const source = new EventSource('/api/events');
  state.eventSource = source;
  source.addEventListener('fleet', event => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      scheduleEventRecovery();
      return;
    }
    if (message.type === 'snapshot') {
      applyFleetSnapshot(message.payload || {});
    } else if (message.type === 'session_removed') {
      removeFleetSession(message.sessionId);
    } else if (message.payload?.session) {
      applyFleetSession(message.payload.session);
    } else {
      scheduleEventRecovery();
    }
  });
  source.onopen = () => {
    el.connection.classList.remove('offline');
    el['connection-text'].textContent = 'Live';
  };
  source.onerror = () => {
    el.connection.classList.add('offline');
    el['connection-text'].textContent = 'Reconnecting';
  };
}
