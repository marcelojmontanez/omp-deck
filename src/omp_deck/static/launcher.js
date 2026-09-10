'use strict';

function renderProjectOptions(projects) {
  const select = el['project-select'];
  select.replaceChildren(
    new Option('Choose project…', ''),
    new Option('＋ New directory…', '__new__'),
  );
  const sections = new Map();
  for (const project of projects) {
    if (typeof project !== 'string' || !project) continue;
    const section = project.split('/', 1)[0];
    if (!sections.has(section)) sections.set(section, []);
    sections.get(section).push(project);
  }
  for (const [section, entries] of sections) {
    if (entries.length === 1 && entries[0] === section) {
      select.append(new Option(`${section}/`, section));
      continue;
    }
    const group = document.createElement('optgroup');
    group.label = `${section}/`;
    for (const project of entries) {
      const label = project === section ? `Use ${section}/` : project.slice(section.length + 1);
      group.append(new Option(label, project));
    }
    select.append(group);
  }
}

let projectLoadGeneration = 0;
async function loadLaunchableProjects() {
  const generation = ++projectLoadGeneration;
  const machineId = state.selectedMachineId;
  const machine = ompDeckApi.machine(machineId);
  const select = el['project-select'];
  select.disabled = true;
  select.replaceChildren(new Option(machine ? 'Loading project directories…' : 'No machine selected', ''));
  el['project-root-label'].textContent = machine
    ? `Start OMP on ${machine.name}`
    : 'Start OMP on an enrolled machine';
  el['launch-state'].textContent = machine ? 'Loading directories…' : 'No machine is enrolled.';
  if (!machine) {
    syncProjectLauncher();
    return;
  }
  if (state.reloadRequiredMachines.has(machineId)) {
    select.replaceChildren(new Option('Reload dashboard to connect', ''));
    el['launch-state'].textContent = 'Reload the dashboard before using this newly enrolled machine.';
    syncProjectLauncher();
    return;
  }
  try {
    const response = await ompDeckApi.request('/api/projects', {machineId, cache: 'no-store'});
    if (!response.ok) throw new Error(`Projects returned ${response.status}`);
    const payload = await response.json();
    if (generation !== projectLoadGeneration || machineId !== state.selectedMachineId) return;
    const projects = Array.isArray(payload.projects) ? payload.projects : [];
    const depth = Number.isSafeInteger(payload.discoveryDepth) ? payload.discoveryDepth : 2;
    el['project-root-label'].textContent =
      `Start OMP on ${machine.name} · ${payload.rootLabel || 'configured project root'} · ${depth} ${depth === 1 ? 'level' : 'levels'}`;
    renderProjectOptions(projects);
    el['launch-state'].textContent =
      `${projects.length} ${projects.length === 1 ? 'directory' : 'directories'} available`;
  } catch (error) {
    if (generation !== projectLoadGeneration || machineId !== state.selectedMachineId) return;
    select.replaceChildren(new Option('Projects unavailable', ''));
    el['launch-state'].textContent = error.message;
  } finally {
    if (generation === projectLoadGeneration && machineId === state.selectedMachineId) {
      select.disabled = false;
      syncProjectLauncher();
    }
  }
}

function selectedLaunchProject() {
  return el['project-select'].value === '__new__'
    ? el['project-new'].value.trim()
    : el['project-select'].value;
}

function syncProjectLauncher() {
  const creating = el['project-select'].value === '__new__';
  el['project-new'].hidden = !creating;
  el['project-launch'].disabled = !state.selectedMachineId || !selectedLaunchProject();
}

el['project-select'].addEventListener('change', () => {
  syncProjectLauncher();
  if (el['project-select'].value === '__new__') el['project-new'].focus();
  el['launch-state'].textContent = '';
});
el['project-new'].addEventListener('input', syncProjectLauncher);
const pendingLaunches = new Map();

el['project-launch'].addEventListener('click', async () => {
  const machineId = state.selectedMachineId;
  const machine = ompDeckApi.machine(machineId);
  const creating = el['project-select'].value === '__new__';
  const project = selectedLaunchProject();
  if (!machine || !project) return;
  el['project-launch'].disabled = true;
  el['project-select'].disabled = true;
  el['project-new'].disabled = true;
  el['launch-state'].textContent = creating ? `Creating ${project} on ${machine.name}…` : `Starting ${project} on ${machine.name}…`;
  const signature = JSON.stringify({project, creating});
  const previous = pendingLaunches.get(machineId);
  const launchRequestId = previous?.signature === signature ? previous.requestId : requestId();
  pendingLaunches.set(machineId, {signature, requestId: launchRequestId});
  try {
    const response = await ompDeckApi.request(`/api/projects/${encodeURIComponent(project)}/sessions`, {
      machineId,
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({requestId: launchRequestId, create: creating}),
    });
    const payload = await response.json().catch(() => ({}));
    if (
      !ompDeckApi.authenticated()
      || ompDeckApi.machine(machineId)?.origin !== machine.origin
    ) return;
    if (!response.ok || !payload.session) {
      const error = new Error(payload.error || `Launch failed (${response.status})`);
      error.reuseRequestId = ompDeckApi.shouldReuseRequestId(response, payload);
      error.projectCreated = payload.created === true;
      throw error;
    }
    pendingLaunches.delete(machineId);
    applyFleetSession(payload.session, undefined, machineId);
    if (machineId === state.selectedMachineId) el['launch-state'].textContent = '';
    showToast(`Started OMP in ${project} on ${machine.name}`, 'success');
    if (creating && machineId === state.selectedMachineId) await loadLaunchableProjects();
    selectAgent(ompDeckApi.sessionKey(machineId, payload.session.id));
  } catch (error) {
    if (error.reuseRequestId === false) pendingLaunches.delete(machineId);
    if (error.projectCreated && machineId === state.selectedMachineId) await loadLaunchableProjects();
    if (machineId === state.selectedMachineId) el['launch-state'].textContent = error.message;
    else showToast(`${machine.name}: ${error.message}`, 'error');
  } finally {
    if (machineId === state.selectedMachineId) {
      el['project-select'].disabled = false;
      el['project-new'].disabled = false;
      syncProjectLauncher();
    }
  }
});

const collabLink = document.getElementById('collab-link');
const collabConnect = document.getElementById('collab-connect');
const collabState = document.getElementById('collab-state');
const pendingCollab = new Map();
function clearLauncherAttempts() {
  projectLoadGeneration += 1;
  pendingLaunches.clear();
  pendingCollab.clear();
  el['project-new'].value = '';
  collabLink.value = '';
  collabState.textContent = '';
}

collabConnect.addEventListener('click', async () => {
  const machineId = state.selectedMachineId;
  const machine = ompDeckApi.machine(machineId);
  const link = collabLink.value.trim();
  if (!machine) {
    collabState.textContent = 'Choose a machine.';
    return;
  }
  if (state.reloadRequiredMachines.has(machineId)) {
    collabState.textContent = 'Reload the dashboard before using this newly enrolled machine.';
    return;
  }
  if (!link) {
    collabState.textContent = 'Paste a writable Collab link.';
    return;
  }
  const previous = pendingCollab.get(machineId);
  const collabRequestId = previous?.link === link ? previous.requestId : requestId();
  pendingCollab.set(machineId, {link, requestId: collabRequestId});
  collabConnect.disabled = true;
  collabLink.disabled = true;
  collabState.textContent = `Connecting on ${machine.name}…`;
  try {
    const response = await ompDeckApi.request('/api/terminal-sessions/collab', {
      machineId,
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({requestId: collabRequestId, link}),
    });
    const payload = await response.json().catch(() => ({}));
    if (
      !ompDeckApi.authenticated()
      || ompDeckApi.machine(machineId)?.origin !== machine.origin
    ) return;
    if (!response.ok || !payload.session) {
      const error = new Error(payload.error || `Connection failed (${response.status})`);
      error.reuseRequestId = ompDeckApi.shouldReuseRequestId(response, payload);
      throw error;
    }
    pendingCollab.delete(machineId);
    if (machineId === state.selectedMachineId) {
      collabLink.value = '';
      collabState.textContent = '';
    }
    applyFleetSession(payload.session, undefined, machineId);
    selectAgent(ompDeckApi.sessionKey(machineId, payload.session.id));
    showToast(`Terminal connected on ${machine.name} through Collab`, 'success');
  } catch (error) {
    if (error.reuseRequestId === false) pendingCollab.delete(machineId);
    if (machineId === state.selectedMachineId) collabState.textContent = error.message;
    else showToast(`${machine.name}: ${error.message}`, 'error');
  } finally {
    collabLink.disabled = false;
    collabConnect.disabled = false;
  }
});
