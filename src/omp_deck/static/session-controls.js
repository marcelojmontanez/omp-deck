
const sessionActionAttempts = new Map();
function clearSessionActionAttempts() {
  sessionActionAttempts.clear();
}


function closeSessionMenu({restoreFocus = false} = {}) {
  el['session-menu'].hidden = true;
  el['overflow-button'].setAttribute('aria-expanded', 'false');
  if (restoreFocus) el['overflow-button'].focus();
}

el['overflow-button'].addEventListener('click', () => {
  const opening = el['session-menu'].hidden;
  el['session-menu'].hidden = !opening;
  el['overflow-button'].setAttribute('aria-expanded', String(opening));
  if (opening) el['session-menu'].querySelector('button')?.focus();
});

el['session-menu'].addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  event.preventDefault();
  closeSessionMenu({restoreFocus: true});
});

el['session-menu'].addEventListener('click', async event => {
  const button = event.target.closest('[data-session-action]');
  const agent = selectedAgent();
  if (!button || !agent || button.disabled) return;
  const action = button.dataset.sessionAction;
  closeSessionMenu();
  if (action === 'copy') {
    try {
      await navigator.clipboard.writeText(location.href);
      showToast('Session link copied', 'success');
    } catch {
      showToast('Could not copy the session link', 'error');
    }
    return;
  }
  if (action === 'remove') {
    if (!confirm('Remove this terminal session from Deck?')) return;
    try {
      const response = await ompDeckApi.request(`/api/sessions/${encodeURIComponent(agent.id)}/remove`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({}),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || `Removal failed (${response.status})`);
      }
      removeFleetSession(agent.id);
      showToast('Terminal session removed', 'success');
    } catch (error) {
      showToast(error.message, 'error');
    }
    return;
  }
  const route = {clear: 'compact', interrupt: 'interrupt', quit: 'shutdown'}[action];
  if (!route) return;
  if (['clear', 'quit'].includes(action) && !confirm(
    action === 'clear' ? 'Compact this session context?' : 'Shut down this OMP session?'
  )) return;
  const attemptKey = `${agent.id}:${route}`;
  const operationRequestId = sessionActionAttempts.get(attemptKey) || requestId();
  sessionActionAttempts.set(attemptKey, operationRequestId);
  try {
    const response = await ompDeckApi.request(`/api/sessions/${encodeURIComponent(agent.id)}/${route}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({requestId: operationRequestId}),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const error = new Error(payload.error || `Action failed (${response.status})`);
      error.reuseRequestId = (
        payload.status === 'uncertain'
        || payload.errorCode === 'outcome_unknown'
      );
      throw error;
    }
    sessionActionAttempts.delete(attemptKey);
    showToast(`${action[0].toUpperCase()}${action.slice(1)} requested`, 'success');
  } catch (error) {
    if (error.reuseRequestId === false) sessionActionAttempts.delete(attemptKey);
    showToast(error.message, 'error');
  }
});

document.addEventListener('click', event => {
  if (!event.target.closest('.detail-header')) closeSessionMenu();
});
