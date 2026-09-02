async function loadLaunchableProjects() {
  try {
    const response = await fetch('/api/projects', {cache: 'no-store'});
    if (!response.ok) throw new Error(`Projects returned ${response.status}`);
    const payload = await response.json();
    el['project-select'].innerHTML = '<option value="">Choose project…</option>'
      + '<option value="__new__">＋ New directory…</option>'
      + (payload.projects || []).map(project =>
        `<option value="${escapeHtml(project)}">${escapeHtml(project)}</option>`
      ).join('');
    syncProjectLauncher();
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

el['project-launch'].addEventListener('click', async () => {
  const creating = el['project-select'].value === '__new__';
  const project = selectedLaunchProject();
  if (!project) return;
  const previousIds = new Set(state.agents.map(agent => agent.id));
  el['project-launch'].disabled = true;
  el['project-select'].disabled = true;
  el['project-new'].disabled = true;
  el['launch-state'].textContent = creating ? `Creating ${project}…` : `Starting ${project}…`;
  try {
    const response = await fetch('/api/launch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({project, create: creating}),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `Launch failed (${response.status})`);
    }
    let launched = null;
    for (let attempt = 0; attempt < 20 && !launched; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 500));
      await refresh();
      launched = state.agents.find(agent => !previousIds.has(agent.id) && agent.project === project);
    }
    if (!launched) throw new Error('OMP started but did not appear in the roster');
    el['launch-state'].textContent = '';
    showToast(`Started OMP in ${project}`);
    if (creating) await loadLaunchableProjects();
    selectAgent(launched.id);
  } catch (error) {
    el['launch-state'].textContent = error.message;
  } finally {
    el['project-select'].disabled = false;
    el['project-new'].disabled = false;
    syncProjectLauncher();
  }
});

loadLaunchableProjects();
