
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
  if (!button || !agent) return;
  const action = button.dataset.sessionAction;
  closeSessionMenu();
  if (action === 'copy') {
    try {
      await navigator.clipboard.writeText(location.href);
      showToast('Session link copied');
    } catch {
      showToast('Could not copy the session link');
    }
    return;
  }
  if (['clear', 'quit'].includes(action) && !confirm(
    action === 'clear' ? 'Clear this session context? Earlier history will be archived.' : 'Quit this OMP session?'
  )) return;
  try {
    const response = await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session: agent.id, action}),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `Action failed (${response.status})`);
    }
    showToast(`${action[0].toUpperCase()}${action.slice(1)} requested`);
    await refresh();
  } catch (error) {
    showToast(error.message);
  }
});

document.addEventListener('click', event => {
  if (!event.target.closest('.detail-header')) closeSessionMenu();
});
