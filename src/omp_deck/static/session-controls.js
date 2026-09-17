let sessionActionGeneration = 0;
const sessionActionAttempts = new Map();
const sessionActionOperations = new Map();
const sessionActionStates = new Map();
const dialogReturnFocus = new WeakMap();
let settingsBusy = false;
let sessionNameTarget = null;
let presentationOperation = null;

function clearSessionActionAttempts() {
  sessionActionAttempts.clear();
  sessionActionOperations.clear();
  sessionActionStates.clear();
  sessionActionGeneration += 1;
  syncSessionActionControls();
}

function sessionActionKey(agent, route) {
  return `${agent.id}:${route}`;
}

function sessionActionOptions(agent) {
  const ongoing = agent?.lifecycle === 'ongoing';
  const nodeAvailable = agent?.nodeAvailable !== false;
  const bridgeConnected = Boolean(agent?.bridgeConnected);
  const capabilities = agent?.capabilities || {};
  const working = (agent?.executionState || agent?.status) === 'working';
  const available = (route, allowed) => Boolean(
    allowed && !sessionActionOperations.has(sessionActionKey(agent, route))
  );
  return [
    {
      action: 'compact', route: 'compact', command: '/compact', label: 'Compact context',
      enabled: available('compact', ongoing && nodeAvailable && bridgeConnected && capabilities.compact),
    },
    {
      action: 'interrupt', route: 'interrupt', command: '/interrupt', label: 'Interrupt',
      enabled: available(
        'interrupt', ongoing && nodeAvailable && bridgeConnected && capabilities.interrupt && working
      ),
    },
    {
      action: 'quit', route: 'shutdown', command: '/quit', label: 'Quit',
      enabled: available('shutdown', ongoing && nodeAvailable && capabilities.shutdown),
    },
  ];
}
function syncSessionActionControls(agent = selectedAgent()) {
  for (const option of sessionActionOptions(agent)) {
    const button = el['session-menu'].querySelector(`[data-session-action="${option.action}"]`);
    const status = agent && sessionActionStates.get(sessionActionKey(agent, option.route));
    button.disabled = !option.enabled;
    button.hidden = !option.enabled && status !== 'pending';
    button.textContent = status === 'pending' ? `${option.label}…`
      : status === 'accepted' ? `${option.label} accepted`
        : ['failed', 'uncertain'].includes(status) ? `Retry ${option.label.toLowerCase()}`
          : option.label;
    button.title = status === 'uncertain'
      ? 'The outcome is uncertain. Retry safely checks the same request.' : '';
  }
}
async function runSessionAction(action, agent = selectedAgent()) {
  const option = sessionActionOptions(agent).find(item => item.action === action);
  if (!agent || !option?.enabled) return false;
  if (
    (action === 'compact' && !confirm('Compact this session context?'))
    || (action === 'quit' && !confirm('Shut down this OMP session?'))
  ) return false;

  const generation = sessionActionGeneration;
  const attemptKey = sessionActionKey(agent, option.route);
  const operationRequestId = sessionActionAttempts.get(attemptKey) || requestId();
  const operation = {};
  sessionActionAttempts.set(attemptKey, operationRequestId);
  sessionActionOperations.set(attemptKey, operation);
  sessionActionStates.set(attemptKey, 'pending');
  if (selectedAgent()?.id === agent.id) syncSessionActionControls(agent);
  showToast(`${agent.label || 'Session'}: Requesting ${option.label.toLowerCase()}…`);
  try {
    const {response, payload} = await ompDeckNetwork.fetchJsonBounded(
      `/api/sessions/${encodeURIComponent(agent.id)}/${option.route}`,
      {
        method: 'POST',
        machineId: agent.machineId,
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({requestId: operationRequestId}),
      },
      ompDeckNetwork.STARTUP_TIMEOUT_MS,
      ompDeckApi.request,
    );
    if (
      generation !== sessionActionGeneration
      || sessionActionOperations.get(attemptKey) !== operation
      || !ompDeckApi.authenticated()
    ) return false;
    if (!response.ok) {
      const error = new Error(payload?.error || `Action failed (${response.status})`);
      error.reuseRequestId = ompDeckApi.shouldReuseRequestId(response, payload || {});
      throw error;
    }
    if (payload?.requestId !== operationRequestId || payload.status !== 'accepted') {
      const error = new Error('Server did not confirm action acceptance');
      error.reuseRequestId = true;
      throw error;
    }
    sessionActionAttempts.delete(attemptKey);
    sessionActionStates.set(attemptKey, 'accepted');
    showToast(`${agent.label || 'Session'}: ${option.label} accepted`, 'success');
    return true;
  } catch (error) {
    if (
      generation !== sessionActionGeneration
      || sessionActionOperations.get(attemptKey) !== operation
    ) return false;
    const uncertain = error?.reuseRequestId !== false;
    if (!uncertain) sessionActionAttempts.delete(attemptKey);
    sessionActionStates.set(attemptKey, uncertain ? 'uncertain' : 'failed');
    const message = error?.message || 'Action failed';
    if (ompDeckApi.authenticated()) showToast(
      `${agent.label || 'Session'}: ${message}${uncertain ? '. Retry will use the same request.' : ''}`,
      'error',
    );
    return false;
  } finally {
    if (sessionActionOperations.get(attemptKey) === operation) {
      sessionActionOperations.delete(attemptKey);
      if (selectedAgent()?.id === agent.id) syncSessionActionControls(agent);
    }
  }
}

function visibleMenuItems() {
  return [...el['session-menu'].querySelectorAll('button')]
    .filter(button => !button.disabled && !button.hidden);
}

function closeSessionMenu({restoreFocus = false} = {}) {
  el['session-menu'].hidden = true;
  el['overflow-button'].setAttribute('aria-expanded', 'false');
  if (restoreFocus && !el['overflow-button'].disabled) el['overflow-button'].focus();
}

function openSessionMenu() {
  syncSessionActionControls();
  el['session-menu'].hidden = false;
  el['overflow-button'].setAttribute('aria-expanded', 'true');
  visibleMenuItems()[0]?.focus();
}

function closeDialog(dialog, {restoreFocus = true} = {}) {
  if (!dialog) return;
  if (!restoreFocus) dialogReturnFocus.delete(dialog);
  if (dialog.open) dialog.close();
}

function openDialog(dialog, trigger) {
  if (!dialog || dialog.open) return;
  for (const open of document.querySelectorAll('.deck-dialog[open]')) {
    closeDialog(open, {restoreFocus: false});
  }
  dialogReturnFocus.set(dialog, trigger);
  dialog.showModal();
  queueMicrotask(() => {
    dialog.querySelector(
      '[autofocus], button:not([disabled]):not([hidden]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled])'
    )?.focus();
  });
}

function clearPrivateDialogState() {
  closeSessionMenu();
  const info = el['session-info'];
  const rename = el['session-name-dialog'];
  const settings = el['deck-settings'];
  closeDialog(info, {restoreFocus: false});
  closeDialog(rename, {restoreFocus: false});
  closeDialog(settings, {restoreFocus: false});
  sessionNameTarget = null;
  presentationOperation = null;
  setPresentationBusy(false);
  el['session-metadata']?.replaceChildren();
  if (el['session-name-input']) el['session-name-input'].value = '';
  if (el['session-name-status']) el['session-name-status'].textContent = '';
  if (el['settings-status']) el['settings-status'].textContent = '';
}


function dialogReturnTarget(trigger) {
  return trigger.closest('#session-menu') ? el['overflow-button'] : trigger;
}

function showSessionInfo(trigger) {
  if (!selectedAgent()) return;
  openDialog(el['session-info'], dialogReturnTarget(trigger));
}

function showSessionNameEditor(trigger) {
  const agent = selectedAgent();
  if (!agent || agent.nodeAvailable === false || presentationOperation) return;
  sessionNameTarget = {id: agent.id, machineId: agent.machineId};
  el['session-name-input'].value = agent.displayName || '';
  el['session-name-status'].textContent = '';
  openDialog(el['session-name-dialog'], dialogReturnTarget(trigger));
}

function setPresentationBusy(busy) {
  el['session-name-dialog']?.querySelectorAll('[data-save-session-name], [data-reset-session-name]')
    .forEach(button => { button.disabled = busy; });
}

async function updateSessionPresentation(agent, changes) {
  if (!agent || agent.nodeAvailable === false || presentationOperation) return false;
  const generation = sessionActionGeneration;
  const operation = {};
  presentationOperation = operation;
  setPresentationBusy(true);
  try {
    const response = await ompDeckApi.request(
      `/api/sessions/${encodeURIComponent(agent.id)}/presentation`,
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(changes),
      }
    );
    const payload = await response.json().catch(() => ({}));
    if (generation !== sessionActionGeneration || !ompDeckApi.authenticated()) return false;
    if (!response.ok) throw new Error(payload.error || `Session update failed (${response.status})`);
    if (
      !payload.session || typeof payload.session !== 'object'
      || !Number.isSafeInteger(payload.snapshotGeneration)
    ) {
      throw new Error('Session update returned invalid state');
    }
    const current = state.agents.find(item => item.id === agent.id);
    if (!current || current.machineId !== agent.machineId) {
      throw new Error('Session is no longer available');
    }
    applyFleetSession(payload.session, payload.snapshotGeneration, agent.machineId);
    return true;
  } catch (error) {
    if (generation === sessionActionGeneration && ompDeckApi.authenticated()) {
      showToast(error.message, 'error');
      if (el['session-name-dialog']?.open) el['session-name-status'].textContent = error.message;
    }
    return false;
  } finally {
    if (presentationOperation === operation) {
      presentationOperation = null;
      setPresentationBusy(false);
    }
  }
}

async function saveSessionName({reset = false} = {}) {
  const target = sessionNameTarget;
  const agent = target ? state.agents.find(item => item.id === target.id) : null;
  if (!agent || agent.machineId !== target.machineId) {
    el['session-name-status'].textContent = 'Session is no longer available.';
    return false;
  }
  const displayName = reset ? '' : el['session-name-input'].value.trim();
  const updated = await updateSessionPresentation(agent, {displayName});
  if (!updated) return false;
  sessionNameTarget = null;
  closeDialog(el['session-name-dialog']);
  showToast(displayName ? 'Session renamed' : 'Session name reset', 'success');
  return true;
}

function showDeckSettings(trigger) {
  const revoke = el['deck-settings']?.querySelector('[data-session-action="revoke-all"]');
  if (revoke) {
    revoke.textContent = ompDeckApi.authMode() === 'tailscale'
      ? 'Sign out all browser sessions'
      : 'Revoke all devices';
  }
  openDialog(el['deck-settings'], dialogReturnTarget(trigger));
}

function returnToSessions() {
  const previousId = state.selectedId;
  closeSessionMenu();
  const url = new URL(location.href);
  url.searchParams.delete('session');
  history.replaceState({view: 'menu'}, '', url);
  showMenu();
  const card = el['agent-list'].querySelector(`[data-id="${CSS.escape(previousId || '')}"]`);
  (card || el['history-toggle']).focus();
}

function setSettingsBusy(busy) {
  settingsBusy = busy;
  el['deck-settings']?.querySelectorAll(
    '[data-clear-local-data], [data-session-action="logout"], [data-session-action="revoke-all"]'
  ).forEach(button => { button.disabled = busy; });
}

async function runSettingsAction(button) {
  if (settingsBusy || button.disabled) return;
  const status = el['settings-status'];
  const action = button.dataset.sessionAction;
  if (button.matches('[data-clear-local-data]')) {
    if (!confirm('Clear drafts and other local browser data for OMP Deck?')) return;
    setSettingsBusy(true);
    if (status) status.textContent = 'Clearing local data…';
    try {
      await clearLocalData();
      if (status) status.textContent = 'Local data cleared.';
      showToast('Local data cleared', 'success');
    } catch (error) {
      if (status) status.textContent = error.message;
      showToast(error.message, 'error');
    } finally {
      setSettingsBusy(false);
    }
    return;
  }

  if (!['logout', 'revoke-all'].includes(action)) return;
  const all = action === 'revoke-all';
  const tailscale = ompDeckApi.authMode() === 'tailscale';
  if (all && !confirm(tailscale
    ? 'Sign out every OMP Deck browser session? Approved Tailscale users can sign in again.'
    : 'Revoke access for every paired device?')) return;
  setSettingsBusy(true);
  if (status) {
    status.textContent = all
      ? (tailscale ? 'Signing out browser sessions…' : 'Revoking paired devices…')
      : 'Logging out…';
  }
  let loggedOut = false;
  try {
    await ompDeckApi.logout({all});
    loggedOut = true;
    await clearLocalData();
  } catch (error) {
    if (!loggedOut) {
      if (status) status.textContent = error.message;
      showToast(error.message, 'error');
    }
  } finally {
    if (loggedOut) {
      clearPrivateDialogState();
      authRecovery.requireAuthentication({clearToken: true});
    }
    setSettingsBusy(false);
  }
}

async function removeSession(agent) {
  const removable = Boolean(agent?.nodeAvailable !== false && agent?.capabilities?.remove);
  if (!removable || !confirm('Remove this terminal session from Deck?')) return;
  const generation = sessionActionGeneration;
  try {
    const response = await ompDeckApi.request(`/api/sessions/${encodeURIComponent(agent.id)}/remove`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
    if (generation !== sessionActionGeneration || !ompDeckApi.authenticated()) return;
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `Removal failed (${response.status})`);
    }
    removeFleetSession(agent.localId, undefined, agent.machineId);
    showToast('Terminal session removed', 'success');
  } catch (error) {
    if (generation === sessionActionGeneration && ompDeckApi.authenticated()) {
      showToast(error.message, 'error');
    }
  }
}

el['overflow-button'].addEventListener('click', () => {
  if (el['session-menu'].hidden) openSessionMenu();
  else closeSessionMenu({restoreFocus: true});
});

el['session-menu'].addEventListener('keydown', event => {
  const items = visibleMenuItems();
  if (event.key === 'Escape') {
    event.preventDefault();
    closeSessionMenu({restoreFocus: true});
    return;
  }
  if (event.key === 'Tab') {
    closeSessionMenu({restoreFocus: true});
    return;
  }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) || !items.length) return;
  event.preventDefault();
  const current = items.indexOf(document.activeElement);
  const index = event.key === 'Home'
    ? 0
    : event.key === 'End'
      ? items.length - 1
      : event.key === 'ArrowDown'
        ? (current + 1 + items.length) % items.length
        : current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length;
  items[index].focus();
});

el['session-menu'].addEventListener('click', async event => {
  const button = event.target.closest('[data-session-action]');
  const agent = selectedAgent();
  if (!button || !agent || button.disabled) return;
  const action = button.dataset.sessionAction;
  closeSessionMenu({restoreFocus: action !== 'rename'});
  if (action === 'copy') {
    const url = new URL(location.href);
    url.searchParams.set('session', agent.id);
    try {
      await navigator.clipboard.writeText(url.href);
      showToast('Session link copied', 'success');
    } catch {
      showToast('Could not copy the session link', 'error');
    }
    return;
  }
  if (action === 'rename') {
    showSessionNameEditor(button);
    return;
  }
  if (action === 'pin') {
    const updated = await updateSessionPresentation(agent, {pinned: !agent.pinned});
    if (updated) showToast(agent.pinned ? 'Session unpinned' : 'Session pinned', 'success');
    return;
  }
  if (action === 'remove') {
    await removeSession(agent);
    return;
  }
  await runSessionAction(action, agent);
});

document.addEventListener('click', event => {
  if (!(event.target instanceof Element)) return;
  const target = event.target;
  const closeButton = target.closest('[data-close-dialog]');
  if (closeButton) {
    closeDialog(closeButton.closest('dialog'));
    return;
  }
  const backButton = target.closest('[data-back-to-sessions]');
  if (backButton) {
    returnToSessions();
    return;
  }
  const infoButton = target.closest('[data-session-info]');
  if (infoButton) {
    closeSessionMenu();
    showSessionInfo(infoButton);
    return;
  }
  const settingsButton = target.closest('[data-deck-settings]');
  if (settingsButton) {
    closeSessionMenu();
    showDeckSettings(settingsButton);
    return;
  }
  const settingsAction = target.closest(
    '#deck-settings [data-clear-local-data], #deck-settings [data-session-action="logout"], #deck-settings [data-session-action="revoke-all"]'
  );
  if (settingsAction) {
    void runSettingsAction(settingsAction);
    return;
  }
  if (!target.closest('#session-menu, #overflow-button')) closeSessionMenu();
});

el['session-name-form'].addEventListener('submit', event => {
  event.preventDefault();
  void saveSessionName();
});
el['session-name-dialog'].querySelector('[data-reset-session-name]').addEventListener('click', () => {
  void saveSessionName({reset: true});
});

for (const dialog of [el['session-info'], el['session-name-dialog'], el['deck-settings']].filter(Boolean)) {
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    closeDialog(dialog);
  });
  dialog.addEventListener('click', event => {
    const bounds = dialog.getBoundingClientRect();
    if (event.target === dialog && (
      event.clientX < bounds.left || event.clientX > bounds.right
      || event.clientY < bounds.top || event.clientY > bounds.bottom
    )) closeDialog(dialog);
  });
  dialog.addEventListener('close', () => {
    const trigger = dialogReturnFocus.get(dialog);
    dialogReturnFocus.delete(dialog);
    if (trigger?.isConnected && trigger.getClientRects().length) trigger.focus({preventScroll: true});
  });
}

window.addEventListener('omp-deck-auth-required', clearPrivateDialogState);
