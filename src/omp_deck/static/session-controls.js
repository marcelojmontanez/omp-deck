
let sessionActionGeneration = 0;
const sessionActionAttempts = new Map();
const dialogReturnFocus = new WeakMap();
let settingsBusy = false;

function clearSessionActionAttempts() {
  sessionActionAttempts.clear();
  sessionActionGeneration += 1;
}


function sessionActionOptions(agent) {
  const ongoing = agent?.lifecycle === 'ongoing';
  const nodeAvailable = agent?.nodeAvailable !== false;
  const bridgeConnected = Boolean(agent?.bridgeConnected);
  const capabilities = agent?.capabilities || {};
  const working = (agent?.executionState || agent?.status) === 'working';
  return [
    {
      action: 'compact',
      command: '/compact',
      label: 'Compact context',
      enabled: Boolean(ongoing && nodeAvailable && bridgeConnected && capabilities.compact),
    },
    {
      action: 'interrupt',
      command: '/interrupt',
      label: 'Interrupt',
      enabled: Boolean(
        ongoing && nodeAvailable && bridgeConnected && capabilities.interrupt && working
      ),
    },
    {
      action: 'quit',
      command: '/quit',
      label: 'Quit',
      enabled: Boolean(ongoing && nodeAvailable && capabilities.shutdown),
    },
  ];
}

async function runSessionAction(action, agent = selectedAgent()) {
  const option = sessionActionOptions(agent).find(item => item.action === action);
  if (!agent || !option?.enabled) return false;
  if (
    (action === 'compact' && !confirm('Compact this session context?'))
    || (action === 'quit' && !confirm('Shut down this OMP session?'))
  ) return false;

  const route = {compact: 'compact', interrupt: 'interrupt', quit: 'shutdown'}[action];
  const generation = sessionActionGeneration;
  const attemptKey = `${agent.id}:${route}`;
  const operationRequestId = sessionActionAttempts.get(attemptKey) || requestId();
  sessionActionAttempts.set(attemptKey, operationRequestId);
  try {
    const response = await ompDeckApi.request(
      `/api/sessions/${encodeURIComponent(agent.id)}/${route}`,
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({requestId: operationRequestId}),
      }
    );
    if (
      generation !== sessionActionGeneration
      || sessionActionAttempts.get(attemptKey) !== operationRequestId
      || !ompDeckApi.authenticated()
    ) return false;
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const error = new Error(payload.error || `Action failed (${response.status})`);
      error.reuseRequestId = ompDeckApi.shouldReuseRequestId(response, payload);
      throw error;
    }
    sessionActionAttempts.delete(attemptKey);
    showToast(`${option.label} requested`, 'success');
    return true;
  } catch (error) {
    if (
      generation !== sessionActionGeneration
      || sessionActionAttempts.get(attemptKey) !== operationRequestId
    ) return false;
    if (error.reuseRequestId === false) sessionActionAttempts.delete(attemptKey);
    if (ompDeckApi.authenticated()) showToast(error.message, 'error');
    return false;
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
  const settings = el['deck-settings'];
  closeDialog(info, {restoreFocus: false});
  closeDialog(settings, {restoreFocus: false});
  el['session-metadata']?.replaceChildren();
  if (el['settings-status']) el['settings-status'].textContent = '';
}


function dialogReturnTarget(trigger) {
  return trigger.closest('#session-menu') ? el['overflow-button'] : trigger;
}

function showSessionInfo(trigger) {
  if (!selectedAgent()) return;
  openDialog(el['session-info'], dialogReturnTarget(trigger));
}

function showDeckSettings(trigger) {
  if (el['settings-status']) el['settings-status'].textContent = '';
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
  if (all && !confirm('Revoke access for every paired device?')) return;
  setSettingsBusy(true);
  if (status) status.textContent = all ? 'Revoking paired devices…' : 'Logging out…';
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
      authRecovery.requirePairing({clearToken: true});
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
  closeSessionMenu({restoreFocus: true});
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

for (const dialog of [el['session-info'], el['deck-settings']].filter(Boolean)) {
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
