async function loadLaunchableProjects() {
  try {
    const response = await ompDeckApi.request('/api/projects', {cache: 'no-store'});
    if (!response.ok) throw new Error(`Projects returned ${response.status}`);
    const payload = await response.json();
    document.getElementById('project-root-label').textContent = `Start OMP in ${payload.rootLabel || 'configured project root'}`;
    el['project-select'].innerHTML = '<option value="">Choose project…</option>'
      + '<option value="__new__">＋ New directory…</option>'
      + (payload.projects || []).map(project =>
        `<option value="${escapeHtml(project)}">${escapeHtml(project)}</option>`
      ).join('');
    syncProjectLauncher();
    el['launch-state'].textContent = '';
  } catch (error) {
    el['launch-state'].textContent = error.message;
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
