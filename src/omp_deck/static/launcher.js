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


async function loadLaunchableProjects() {
  const select = el['project-select'];
  select.disabled = true;
  select.replaceChildren(new Option('Loading project directories…', ''));
  el['launch-state'].textContent = 'Loading directories…';
  try {
    const response = await ompDeckApi.request('/api/projects', {cache: 'no-store'});
    if (!response.ok) throw new Error(`Projects returned ${response.status}`);
    const payload = await response.json();
    const projects = Array.isArray(payload.projects) ? payload.projects : [];
    const depth = Number.isSafeInteger(payload.discoveryDepth) ? payload.discoveryDepth : 2;
    document.getElementById('project-root-label').textContent =
      `Start OMP in ${payload.rootLabel || 'configured project root'} · ${depth} ${depth === 1 ? 'level' : 'levels'}`;
    renderProjectOptions(projects);
    el['launch-state'].textContent =
      `${projects.length} ${projects.length === 1 ? 'directory' : 'directories'} available`;
  } catch (error) {
    select.replaceChildren(new Option('Projects unavailable', ''));
    el['launch-state'].textContent = error.message;
  } finally {
    select.disabled = false;
    syncProjectLauncher();
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
  el['project-launch'].disabled = !selectedLaunchProject();
}

el['project-select'].addEventListener('change', () => {
  syncProjectLauncher();
  if (el['project-select'].value === '__new__') el['project-new'].focus();
  el['launch-state'].textContent = '';
});
el['project-new'].addEventListener('input', syncProjectLauncher);
let pendingLaunch = null;



el['project-launch'].addEventListener('click', async () => {
  const creating = el['project-select'].value === '__new__';
  const project = selectedLaunchProject();
  if (!project) return;
  el['project-launch'].disabled = true;
  el['project-select'].disabled = true;
  el['project-new'].disabled = true;
  el['launch-state'].textContent = creating ? `Creating ${project}…` : `Starting ${project}…`;
  const signature = JSON.stringify({project, creating});
  const launchRequestId = pendingLaunch?.signature === signature
    ? pendingLaunch.requestId
    : requestId();
  pendingLaunch = {signature, requestId: launchRequestId};
  try {
    const response = await ompDeckApi.request(`/api/projects/${encodeURIComponent(project)}/sessions`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({requestId: launchRequestId, create: creating}),
    });
    const payload = await response.json();
    if (!response.ok || !payload.session) {
      const error = new Error(payload.error || `Launch failed (${response.status})`);
      error.reuseRequestId = ompDeckApi.shouldReuseRequestId(response, payload);
      error.projectCreated = payload.created === true;
      throw error;
    }
    pendingLaunch = null;
    applyFleetSession(payload.session);
    el['launch-state'].textContent = '';
    showToast(`Started OMP in ${project}`, 'success');
    if (creating) await loadLaunchableProjects();
    selectAgent(payload.session.id);
  } catch (error) {
    if (error.reuseRequestId === false) pendingLaunch = null;
    if (error.projectCreated) await loadLaunchableProjects();
    el['launch-state'].textContent = error.message;
  } finally {
    el['project-select'].disabled = false;
    el['project-new'].disabled = false;
    syncProjectLauncher();
  }
});

const collabLink = document.getElementById('collab-link');
const collabConnect = document.getElementById('collab-connect');
const collabState = document.getElementById('collab-state');
let pendingCollab = null;

collabConnect.addEventListener('click', async () => {
  const link = collabLink.value.trim();
  if (!link) {
    collabState.textContent = 'Paste a writable Collab link.';
    return;
  }
  const collabRequestId = pendingCollab?.link === link
    ? pendingCollab.requestId
    : requestId();
  pendingCollab = {link, requestId: collabRequestId};
  collabConnect.disabled = true;
  collabLink.disabled = true;
  collabState.textContent = 'Connecting…';
  try {
    const response = await ompDeckApi.request('/api/terminal-sessions/collab', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({requestId: collabRequestId, link}),
    });
    const payload = await response.json();
    if (!response.ok || !payload.session) {
      const error = new Error(payload.error || `Connection failed (${response.status})`);
      error.reuseRequestId = ompDeckApi.shouldReuseRequestId(response, payload);
      throw error;
    }
    pendingCollab = null;
    collabLink.value = '';
    collabState.textContent = '';
    applyFleetSession(payload.session);
    selectAgent(payload.session.id);
    showToast('Terminal connected through Collab', 'success');
  } catch (error) {
    if (error.reuseRequestId === false) pendingCollab = null;
    collabState.textContent = error.message;
  } finally {
    collabLink.disabled = false;
    collabConnect.disabled = false;
  }
});

loadLaunchableProjects();
