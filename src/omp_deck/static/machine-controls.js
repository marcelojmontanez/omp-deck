function machineStatusLabel(status) {
  return {
    connecting: 'Connecting',
    live: 'Live',
    offline: 'Offline',
    unavailable: 'Access unavailable',
  }[status] || 'Unavailable';
}

function renderMachineControls() {
  const machines = ompDeckApi.machines();
  const coordinatorMode = ompDeckApi.mode() === 'coordinator';
  el['machine-panel'].hidden = !coordinatorMode;
  const previous = state.selectedMachineId;
  const machineIds = new Set(machines.map(machine => machine.id));
  for (const option of [...el['machine-select'].options]) {
    if (!machineIds.has(option.value)) option.remove();
  }
  machines.forEach((machine, index) => {
    let option = [...el['machine-select'].options].find(item => item.value === machine.id);
    if (!option) option = new Option('', machine.id);
    const label = `${machine.name} — ${machineStatusLabel(machine.status)}`;
    if (option.textContent !== label) option.textContent = label;
    const current = el['machine-select'].options[index];
    if (current !== option) el['machine-select'].insertBefore(option, current || null);
  });
  state.selectedMachineId = machines.some(machine => machine.id === previous)
    ? previous
    : machines[0]?.id || null;
  if (el['machine-select'].value !== (state.selectedMachineId || '')) {
    el['machine-select'].value = state.selectedMachineId || '';
  }
  el['machine-select'].disabled = machines.length === 0;
  const connectionTarget = machines.find(machine => machine.id === state.selectedMachineId);
  const collabLabel = `Writable Collab link${connectionTarget ? ` for ${connectionTarget.name}` : ''}`;
  const collabLabelElement = document.getElementById('collab-label');
  if (collabLabelElement.textContent !== collabLabel) collabLabelElement.textContent = collabLabel;
  const machineStatuses = machines.length
    ? machines.map(machine => `<span class="machine-status ${escapeHtml(machine.status)}" title="${escapeHtml(machine.error || `${machine.name} is ${machineStatusLabel(machine.status).toLowerCase()}`)}"><i aria-hidden="true"></i><strong>${escapeHtml(machine.name)}</strong> ${escapeHtml(machineStatusLabel(machine.status))}</span>`).join('')
    : '<span class="machine-empty">No machines are enrolled.</span>';
  if (el['machine-status-list'].innerHTML !== machineStatuses) {
    el['machine-status-list'].innerHTML = machineStatuses;
  }
  const live = machines.filter(machine => machine.status === 'live').length;
  let connectionText;
  if (navigator.onLine === false) {
    el.connection.classList.add('offline');
    connectionText = 'Browser offline; drafts are preserved';
  } else if (coordinatorMode && state.coordinatorAvailable === false) {
    el.connection.classList.add('offline');
    connectionText = 'Deck coordinator unavailable; machine access may expire';
  } else {
    el.connection.classList.toggle('offline', machines.length > 0 && live === 0);
    connectionText = coordinatorMode
      ? `Deck connected · ${live}/${machines.length} machines reachable`
      : (machines[0]?.status === 'live'
        ? 'Deck connected'
        : `Deck ${machineStatusLabel(machines[0]?.status).toLowerCase()}`);
  }
  if (el['connection-text'].textContent !== connectionText) {
    el['connection-text'].textContent = connectionText;
  }
}
