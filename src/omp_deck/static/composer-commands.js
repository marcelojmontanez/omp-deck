function createComposerCommands({
  elements,
  selectedAgent,
  commandActions,
  executeAction,
  setThinkingLevel,
  thinkingChoices,
  thinkingLevel,
  showToast,
  hasAttachments,
}) {
  const menu = elements['command-menu'];
  const mode = elements['command-mode'];
  const input = elements.message;
  const send = elements.send;
  const defaultSendLabel = send.textContent || 'Send';
  let suggestions = [];
  let activeIndex = -1;
  let suppressSuggestions = false;
  const revisions = new Map();
  let editRevision = 0;

  function allChoices(agent = selectedAgent()) {
    if (!agent) return [];
    const actions = (commandActions?.(agent) || [])
      .filter(option => /^\/[a-z][a-z-]*$/i.test(option?.command || ''))
      .map(option => ({
        kind: 'action',
        action: option.action,
        command: option.command,
        label: option.label || option.command,
        enabled: option.enabled === true,
      }));
    const canSetThinking = agent.lifecycle === 'ongoing' && agent.nodeAvailable !== false
      && agent.bridgeConnected
      && agent.capabilities?.setThinking;
    const levels = typeof thinkingChoices === 'function' ? thinkingChoices(agent) : [];
    const seenLevels = new Set();
    const thinking = (Array.isArray(levels) ? levels : [])
      .filter(level => {
        if (typeof level !== 'string' || !level || seenLevels.has(level)) return false;
        seenLevels.add(level);
        return true;
      })
      .map(level => ({
        kind: 'thinking',
        level,
        command: `/thinking ${level}`,
        label: `Set thinking to ${level}`,
        enabled: canSetThinking,
      }));
    return [...actions, ...thinking];
  }

  function choices(agent = selectedAgent()) {
    return allChoices(agent).filter(choice => choice.enabled);
  }

  function exactChoice(value, agent = selectedAgent()) {
    const command = value.trim();
    const available = allChoices(agent);
    const exact = available.find(choice => choice.command === command);
    if (exact) return exact;
    const level = /^\/thinking\s+(\S+)$/.exec(command)?.[1];
    if (level && !available.some(choice => choice.enabled && choice.command.startsWith(command))) {
      return {kind: 'thinking', command, level, label: 'Thinking level unavailable', enabled: false};
    }
    return null;
  }

  function hideSuggestions() {
    suggestions = [];
    activeIndex = -1;
    menu.replaceChildren();
    menu.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  }

  function updateMode(agent = selectedAgent()) {
    const choice = exactChoice(input.value, agent);
    mode.hidden = !choice;
    mode.textContent = choice
      ? `Command: ${choice.label}${choice.enabled ? '' : ' · unavailable'}${hasAttachments() ? ' · remove images to run' : ''}`
      : '';
    send.textContent = choice ? 'Run' : defaultSendLabel;
    send.dataset.composerMode = choice ? 'command' : 'message';
    return choice;
  }

  function renderSuggestions(items) {
    suggestions = items;
    activeIndex = items.length ? Math.min(Math.max(activeIndex, 0), items.length - 1) : -1;
    menu.replaceChildren(...items.map((choice, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.id = `composer-command-${index}`;
      button.dataset.commandIndex = String(index);
      button.setAttribute('role', 'option');
      button.setAttribute('aria-selected', String(index === activeIndex));
      const command = document.createElement('span');
      command.className = 'command-name';
      command.textContent = choice.command;
      const label = document.createElement('span');
      label.className = 'command-label';
      label.textContent = choice.label;
      if (choice.kind === 'thinking' && choice.level === thinkingLevel?.(selectedAgent())) {
        label.textContent += ' (current)';
      }
      button.append(command, label);
      return button;
    }));
    menu.hidden = !items.length;
    input.setAttribute('aria-expanded', String(Boolean(items.length)));
    if (activeIndex >= 0) input.setAttribute('aria-activedescendant', `composer-command-${activeIndex}`);
    else input.removeAttribute('aria-activedescendant');
  }

  function sync({showSuggestions = true} = {}) {
    const agent = selectedAgent();
    updateMode(agent);
    const value = input.value.trimStart();
    if (
      !showSuggestions
      || suppressSuggestions
      || !value.startsWith('/')
      || value.includes('\n')
    ) {
      hideSuggestions();
      return;
    }
    const query = value.toLowerCase();
    renderSuggestions(choices(agent).filter(choice => choice.command.toLowerCase().startsWith(query)));
  }

  function choose(index) {
    const choice = suggestions[index];
    if (!choice) return;
    input.value = choice.command;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    suppressSuggestions = true;
    hideSuggestions();
    updateMode();
    input.focus();
  }

  function moveActive(offset) {
    if (!suggestions.length) return;
    activeIndex = (activeIndex + offset + suggestions.length) % suggestions.length;
    renderSuggestions(suggestions);
    menu.querySelector(`[data-command-index="${activeIndex}"]`)?.scrollIntoView({block: 'nearest'});
  }

  function handleKeydown(event) {
    if (window.matchMedia('(max-width: 760px)').matches && event.key === 'Enter') return false;
    if (event.key === 'Escape' && !menu.hidden) {
      event.preventDefault();
      suppressSuggestions = true;
      hideSuggestions();
      return true;
    }
    if (menu.hidden) return false;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      moveActive(event.key === 'ArrowDown' ? 1 : -1);
      return true;
    }
    if (event.key === 'Tab' && activeIndex >= 0) {
      event.preventDefault();
      choose(activeIndex);
      return true;
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !exactChoice(input.value)) {
      event.preventDefault();
      choose(activeIndex);
      return true;
    }
    return false;
  }

  async function run(choice, agent) {
    if (!choice.enabled) {
      showToast('This command is not currently available.', 'error');
      return false;
    }
    try {
      const accepted = choice.kind === 'thinking'
        ? await setThinkingLevel(choice.level, agent)
        : await executeAction(choice.action, agent);
      return accepted === true;
    } catch (error) {
      showToast(`Command failed: ${error.message}`, 'error');
      return false;
    }
  }

  menu.addEventListener('click', event => {
    const button = event.target.closest('[data-command-index]');
    if (button) choose(Number(button.dataset.commandIndex));
  });

  input.setAttribute('aria-controls', menu.id);
  input.setAttribute('aria-autocomplete', 'list');
  input.setAttribute('aria-expanded', 'false');

  return {
    exactChoice,
    handleKeydown,
    run,
    sync,
    revision(agent) {
      const id = agent?.id;
      if (!revisions.has(id)) revisions.set(id, ++editRevision);
      return revisions.get(id);
    },
    unchanged(agent, revision) {
      return revisions.get(agent?.id) === revision;
    },
    reset() {
      suppressSuggestions = true;
      hideSuggestions();
      updateMode();
    },
    clear() {
      revisions.clear();
      this.reset();
    },
    prune(validIds) {
      const valid = new Set(validIds);
      for (const id of revisions.keys()) {
        if (!valid.has(id)) revisions.delete(id);
      }
    },
    inputChanged() {
      const id = selectedAgent()?.id;
      if (id) revisions.set(id, ++editRevision);
      suppressSuggestions = false;
      sync();
    },
  };
}
