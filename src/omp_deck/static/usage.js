const USAGE_REFRESH_INTERVAL_MS = 5 * 60_000;
const USAGE_REFRESH_THROTTLE_MS = 60_000;
let usageLoadedAt = 0;
let usageMachineId = null;
let usageMachineOrigin = null;
let usageRequest = null;
let usageTimer = null;
let usageActive = false;

function usageResetTime(value) {
  const seconds = Math.max(0, Math.round((value - Date.now()) / 1000));
  if (seconds < 60) return '<1m';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
}

function renderUsage(payload) {
  const list = document.getElementById('usage-list');
  if (payload.error) {
    list.innerHTML = `<span class="usage-error">${escapeHtml(payload.error)}</span>`;
    return;
  }
  const limits = (payload.reports || []).flatMap(report =>
    (report.limits || []).map(limit => ({...limit, provider: report.provider}))
  );
  list.innerHTML = limits.map(limit => {
    const used = Math.round((limit.amount?.usedFraction || 0) * 100);
    const reset = limit.window?.resetsAt ? ` · ${usageResetTime(limit.window.resetsAt)}` : '';
    return `<div class="usage-item">
      <div class="usage-label"><strong>${escapeHtml(limit.label)}</strong><span>${used}%${reset}</span></div>
      <progress class="usage-bar" max="100" value="${Math.min(100, Math.max(0, used))}" aria-label="${escapeHtml(limit.label)} usage: ${used}%">${used}%</progress>
    </div>`;
  }).join('');
}

function cancelUsageRequest() {
  usageRequest?.controller.abort();
  usageRequest = null;
}

function usageContextIsCurrent(machineId, machineOrigin) {
  return ompDeckApi.authenticated()
    && !state.reloadRequiredMachines.has(machineId)
    && state.selectedMachineId === machineId
    && ompDeckApi.machine(machineId)?.origin === machineOrigin;
}

async function refreshUsage({replace = false} = {}) {
  if (!ompDeckApi.authenticated()) return;
  const machineId = state.selectedMachineId;
  const machineOrigin = ompDeckApi.machine(machineId)?.origin;
  if (!machineId) {
    cancelUsageRequest();
    usageMachineId = null;
    usageLoadedAt = 0;
    usageMachineOrigin = null;
    renderUsage({reports: []});
    return;
  }
  if (state.reloadRequiredMachines.has(machineId)) {
    cancelUsageRequest();
    renderUsage({error: 'Reload the dashboard to authorize this machine origin.'});
    return;
  }
  if (
    !replace
    && usageMachineId === machineId
    && usageMachineOrigin === machineOrigin
    && Date.now() - usageLoadedAt < USAGE_REFRESH_THROTTLE_MS
  ) return;
  if (usageRequest) {
    if (
      !replace
      && usageRequest.machineId === machineId
      && usageRequest.machineOrigin === machineOrigin
    ) return usageRequest.promise;
    cancelUsageRequest();
  }

  usageMachineId = machineId;
  usageLoadedAt = Date.now();
  usageMachineOrigin = machineOrigin;
  const request = {
    machineId,
    machineOrigin,
    controller: new AbortController(),
    promise: null,
  };
  usageRequest = request;
  request.promise = (async () => {
    try {
      const {response, payload} = await ompDeckNetwork.fetchJsonBounded('/api/usage', {
        machineId,
        cache: 'no-store',
        signal: request.controller.signal,
      }, ompDeckNetwork.STARTUP_TIMEOUT_MS, ompDeckApi.request);
      if (!response.ok) throw new Error(`Usage returned ${response.status}`);
      if (usageRequest === request && usageContextIsCurrent(machineId, machineOrigin)) {
        renderUsage(payload);
      }
    } catch (error) {
      if (
        !request.controller.signal.aborted
        && usageRequest === request
        && usageContextIsCurrent(machineId, machineOrigin)
      ) renderUsage({error: error.message});
    } finally {
      if (usageRequest === request) usageRequest = null;
    }
  })();
  return request.promise;
}

function startUsageRefresh() {
  usageActive = true;
  if (!usageTimer) {
    usageTimer = setInterval(() => {
      if (document.visibilityState === 'visible') void refreshUsage();
    }, USAGE_REFRESH_INTERVAL_MS);
  }
  return refreshUsage();
}

function stopUsageRefresh() {
  usageActive = false;
  clearInterval(usageTimer);
  usageTimer = null;
  cancelUsageRequest();
  usageLoadedAt = 0;
  usageMachineId = null;
  usageMachineOrigin = null;
}

function invalidateUsageRefresh() {
  cancelUsageRequest();
  usageLoadedAt = 0;
  usageMachineId = null;
  usageMachineOrigin = null;
  if (usageActive) return refreshUsage();
}

function foregroundUsageRefresh() {
  if (!usageActive || document.visibilityState !== 'visible') return;
  return refreshUsage();
}

function usageMachineChanged() {
  return invalidateUsageRefresh();
}

window.ompDeckUsage = Object.freeze({
  start: startUsageRefresh,
  stop: stopUsageRefresh,
  foreground: foregroundUsageRefresh,
  machineChanged: usageMachineChanged,
  refresh: refreshUsage,
  invalidate: invalidateUsageRefresh,
});
