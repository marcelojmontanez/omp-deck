window.TodoUI = (() => {
  function groups(todos) {
    const phases = new Map();
    if (!Array.isArray(todos)) return [];
    for (const todo of todos) {
      if (!todo || typeof todo !== 'object') continue;
      const phase = String(todo.phase ?? todo.group ?? 'Tasks');
      if (!phases.has(phase)) phases.set(phase, []);
      const status = todo.status === 'in_progress' ? 'in progress' : String(todo.status ?? '');
      phases.get(phase).push({
        done: todo.done === true || ['done', 'completed'].includes(todo.status),
        text: String(todo.text ?? todo.title ?? todo.content ?? 'Task'),
        status,
      });
    }
    return [...phases].map(([name, tasks]) => ({name, tasks}));
  }

  function count(todos) {
    return Array.isArray(todos)
      ? todos.reduce((total, todo) => total + (todo && typeof todo === 'object' ? 1 : 0), 0)
      : 0;
  }

  function render(todos, escapeHtml) {
    const phases = groups(todos);
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
      <progress class="todo-progress" max="${tasks.length}" value="${complete}" aria-label="${progress}% of session tasks complete">${progress}%</progress>
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

  return {count, render};
})();
