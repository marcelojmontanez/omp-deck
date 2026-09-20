// Visual deduplication only: native acceptance comes from the correlated response,
// never from matching conversation text or from an agent execution transition.
function createComposerHandoff({pendingFor, releasePending}) {
  const MAX_TRACKED_PER_SESSION = 1_000;
  const MAX_NATIVE_EVENTS_PER_SNAPSHOT = 512;
  const MAX_CONSUMED_PER_SESSION = 4_096;
  const sessions = new Map();

  function nativeUserEvents(agent) {
    const events = [];
    for (const event of agent?.session?.activity || []) {
      if (event?.kind !== 'user' || typeof event.id !== 'string' || !event.id) continue;
      if (events.length >= MAX_NATIVE_EVENTS_PER_SNAPSHOT) return null;
      events.push(event);
    }
    return events;
  }

  function sessionFor(id) {
    if (!sessions.has(id)) {
      sessions.set(id, {
        tracked: new Map(), untrackable: new WeakSet(), consumed: new Set(), saturated: false,
      });
    }
    return sessions.get(id);
  }

  function forgetStale(id, session) {
    const pending = new Set(pendingFor(id));
    for (const [requestId, tracker] of session.tracked) {
      if (pending.has(tracker.item)) continue;
      if (tracker.counterpartId) rememberConsumed(session, tracker.counterpartId);
      session.tracked.delete(requestId);
    }
  }

  function sameContent(item, event) {
    return item.text === event.detail
      && item.attachments.length === 0
      && Array.isArray(event.attachments)
      && event.attachments.length === 0;
  }

  function uniqueEvents(agent) {
    const events = nativeUserEvents(agent);
    if (!events) return null;
    const unique = new Map();
    for (const event of events) {
      if (unique.has(event.id)) unique.set(event.id, null);
      else unique.set(event.id, event);
    }
    return unique;
  }

  function rememberConsumed(session, eventId) {
    if (!session.consumed.has(eventId) && session.consumed.size >= MAX_CONSUMED_PER_SESSION) {
      session.saturated = true;
      for (const tracker of session.tracked.values()) tracker.baseline = null;
      return false;
    }
    session.consumed.add(eventId);
    for (const tracker of session.tracked.values()) {
      if (!tracker.baseline) continue;
      if (tracker.baseline.size >= MAX_NATIVE_EVENTS_PER_SNAPSHOT) tracker.baseline = null;
      else tracker.baseline.add(eventId);
    }
    return true;
  }

  function reconcile(agent) {
    const id = agent?.id;
    const session = sessions.get(id);
    if (!session?.tracked.size) return false;
    forgetStale(id, session);
    if (session.saturated) return false;
    if (!session.tracked.size) return false;

    const events = uniqueEvents(agent);
    if (!events) return false;
    const claimed = new Set();
    for (const tracker of session.tracked.values()) {
      const counterpart = tracker.counterpartId && events.get(tracker.counterpartId);
      if (counterpart && sameContent(tracker.item, counterpart)) {
        claimed.add(tracker.counterpartId);
      } else {
        tracker.counterpartId = null;
      }
    }

    for (const tracker of session.tracked.values()) {
      if (tracker.counterpartId) continue;
      for (const [eventId, event] of events) {
        if (!event || !tracker.baseline || session.consumed.has(eventId)
          || claimed.has(eventId) || tracker.baseline.has(eventId)) continue;
        if (!sameContent(tracker.item, event)) continue;
        tracker.counterpartId = eventId;
        claimed.add(eventId);
        break;
      }
    }

    let changed = false;
    for (const [requestId, tracker] of [...session.tracked]) {
      if (tracker.item.state !== 'accepted' || !tracker.counterpartId) continue;
      if (!rememberConsumed(session, tracker.counterpartId)) continue;
      session.tracked.delete(requestId);
      releasePending(id, tracker.item);
      changed = true;
    }
    return changed;
  }

  function capture(agent, item) {
    if (!agent?.id || !item?.id || item.action === 'followUp') return false;
    const session = sessionFor(agent.id);
    forgetStale(agent.id, session);
    if (session.tracked.has(item.id) || session.untrackable.has(item)) return true;
    if (session.saturated || item.attachments.length || session.tracked.size >= MAX_TRACKED_PER_SESSION) {
      session.untrackable.add(item);
      return true;
    }
    const baseline = nativeUserEvents(agent);
    session.tracked.set(item.id, {
      item,
      baseline: baseline ? new Set(baseline.map(event => event.id)) : null,
      counterpartId: null,
    });
    return true;
  }

  function accepted(agent, item) {
    const session = sessions.get(agent?.id);
    const tracked = session?.tracked.has(item?.id) === true;
    const retained = tracked || session?.untrackable.has(item) === true;
    if (tracked) reconcile(agent);
    return retained;
  }

  function forget(id, item) {
    const session = sessions.get(id);
    if (!session) return;
    const tracker = session.tracked.get(item.id);
    if (tracker?.counterpartId) rememberConsumed(session, tracker.counterpartId);
    session.tracked.delete(item.id);
    session.untrackable.delete(item);
  }

  return {
    capture,
    accepted,
    reconcile,
    forget,
    has(id, item) {
      const session = sessions.get(id);
      return session?.tracked.get(item?.id)?.item === item || session?.untrackable.has(item) === true;
    },
    clear() {
      sessions.clear();
    },
    remove(id) {
      sessions.delete(id);
    },
    prune(validIds) {
      const retained = new Set(validIds);
      for (const id of sessions.keys()) {
        if (!retained.has(id)) sessions.delete(id);
      }
    },
  };
}
