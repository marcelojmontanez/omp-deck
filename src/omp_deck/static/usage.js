let usageLoadedAt = 0;
let usageMachineId = null;

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
      <div class="usage-bar"><div class="usage-fill" style="width:${Math.min(100, Math.max(0, used))}%"></div></div>
    </div>`;
  }).join('');
}

async function refreshUsage() {
  const machineId = state.selectedMachineId;
  const machineOrigin = ompDeckApi.machine(machineId)?.origin;
  if (!machineId) {
    renderUsage({reports: []});
    return;
  }
  if (state.reloadRequiredMachines.has(machineId)) {
    renderUsage({error: 'Reload the dashboard to authorize this machine origin.'});
    return;
  }
  if (usageMachineId === machineId && Date.now() - usageLoadedAt < 60_000) return;
  usageMachineId = machineId;
  usageLoadedAt = Date.now();
  try {
    const response = await ompDeckApi.request('/api/usage', {machineId, cache: 'no-store'});
    if (!response.ok) throw new Error(`Usage returned ${response.status}`);
    const payload = await response.json();
    if (
      ompDeckApi.authenticated()
      && !state.reloadRequiredMachines.has(machineId)
      && state.selectedMachineId === machineId
      && ompDeckApi.machine(machineId)?.origin === machineOrigin
    ) renderUsage(payload);
  } catch (error) {
    if (
      ompDeckApi.authenticated()
      && !state.reloadRequiredMachines.has(machineId)
      && state.selectedMachineId === machineId
      && ompDeckApi.machine(machineId)?.origin === machineOrigin
    ) renderUsage({error: error.message});
  }
}
