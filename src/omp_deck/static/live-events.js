function scheduleEventRecovery() {
  if (state.eventRefreshTimer) return;
  state.eventRefreshTimer = setTimeout(() => {
    state.eventRefreshTimer = null;
    void refresh();
  }, 100);
}

function scheduleEventReconnect(source) {
  if (state.eventReconnectTimer) return;
  state.eventReconnectTimer = setTimeout(() => {
    state.eventReconnectTimer = null;
    if (state.eventSource === source) startEventStream();
  }, 3000);
}

function startEventStream() {
  state.eventSource?.close();
  const source = new EventSource('/api/events');
  state.eventSource = source;
  source.addEventListener('fleet', event => {
    try {
      const message = JSON.parse(event.data);
      if (message.type === 'snapshot') {
        applyFleetSnapshot(message.payload || {});
        return;
      }
      const generation = message.snapshotGeneration;
      if (
        !Number.isSafeInteger(generation)
        || generation > state.snapshotGeneration + 1
      ) {
        scheduleEventRecovery();
        return;
      }
      if (generation <= state.snapshotGeneration) return;
      if (message.type === 'session_removed') {
        removeFleetSession(message.sessionId, generation);
      } else if (message.payload?.session) {
        applyFleetSession(message.payload.session, generation);
      } else {
        scheduleEventRecovery();
      }
    } catch {
      scheduleEventRecovery();
    }
  });
  source.onopen = () => {
    el.connection.classList.remove('offline');
    el['connection-text'].textContent = 'Live';
  };
  source.onerror = () => {
    source.close();
    el.connection.classList.add('offline');
    el['connection-text'].textContent = 'Reconnecting';
    scheduleEventRecovery();
    scheduleEventReconnect(source);
  };
}
