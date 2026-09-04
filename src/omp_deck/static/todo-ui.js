window.TodoUI = (() => {
  const TASK = /^\s{4}- \[([X ])\] (.+?)(?: \((in progress|blocked)\))?\s*$/i;
  const PHASE = /^\s{2}([^:\n]+):\s*$/;

  function parse(text) {
    const phases = [];
    let phase = null;
    for (const line of String(text || '').split('\n')) {
      const phaseMatch = line.match(PHASE);
      if (phaseMatch) {
        phase = {name: phaseMatch[1].trim(), tasks: []};
        phases.push(phase);
        continue;
      }
      const taskMatch = line.match(TASK);
      if (!taskMatch || !phase) continue;
      phase.tasks.push({
        done: taskMatch[1].toUpperCase() === 'X',
        text: taskMatch[2].trim(),
        status: taskMatch[3] || '',
      });
    }
    return phases.filter(item => item.tasks.length);
  }

  function render(text, escapeHtml) {
    const phases = parse(text);
    if (!phases.length) {
      return `<section class="todo-panel" id="todo-panel" aria-label="Session tasks">
        <div class="todo-panel-head"><span class="todo-panel-icon">✓</span><strong>Tasks</strong></div>
        <div class="todo-empty">No structured tasks available.</div>
      </section>`;
    }
    const tasks = phases.flatMap(phase => phase.tasks);
    const complete = tasks.filter(task => task.done).length;
    const progress = tasks.length ? Math.round((complete / tasks.length) * 100) : 0;
    return `<section class="todo-panel" id="todo-panel" aria-label="Session tasks">
      <div class="todo-panel-head">
        <span class="todo-panel-icon">✓</span>
        <div><strong>Session tasks</strong><span>${complete} of ${tasks.length} complete</span></div>
        <span class="todo-percent">${progress}%</span>
      </div>
      <div class="todo-progress" aria-hidden="true"><i style="width:${progress}%"></i></div>
      <div class="todo-phases">${phases.map(phase => `<section class="todo-phase">
        <h3>${escapeHtml(phase.name)}</h3>
        <ul>${phase.tasks.map(task => `<li class="${task.done ? 'done' : ''} ${task.status === 'in progress' ? 'current' : ''}">
          <span class="todo-check">${task.done ? '✓' : task.status === 'in progress' ? '•' : ''}</span>
          <span>${escapeHtml(task.text)}</span>
          ${task.status ? `<small>${escapeHtml(task.status)}</small>` : ''}
        </li>`).join('')}</ul>
      </section>`).join('')}</div>
    </section>`;
  }

  return {parse, render};
})();
