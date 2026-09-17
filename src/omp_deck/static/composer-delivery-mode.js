function createComposerDeliveryMode({elements, selectedAgent}) {
  const preferences = new Map();
  const group = elements['delivery-mode'];

  const isBusy = agent => (agent?.executionState || agent?.status) === 'working';
  const isAvailable = agent => Boolean(
    agent?.lifecycle === 'ongoing'
    && agent.capabilities?.sendMessage === true
    && agent.capabilities?.followUp === true
    && isBusy(agent)
  );
  const modeFor = agent => agent && preferences.get(agent.id) === 'queue' ? 'queue' : 'steer';

  function sync(agent = selectedAgent()) {
    const available = isAvailable(agent);
    const mode = available ? modeFor(agent) : 'steer';
    if (group) {
      group.hidden = !available;
      group.querySelectorAll('[data-delivery-mode]').forEach(button => {
        const active = button.dataset.deliveryMode === mode;
        button.setAttribute('aria-pressed', String(active));
        button.disabled = !available;
      });
    }
    const send = elements.send;
    if (send) {
      const intent = available ? (mode === 'queue' ? 'Queue after current turn' : 'Steer current turn') : 'Send message';
      send.setAttribute('aria-label', intent);
      send.title = intent;
      if (send.textContent !== 'Run') send.textContent = available ? (mode === 'queue' ? 'Queue' : 'Steer') : 'Send';
    }
    return {available, mode, action: available && mode === 'queue' ? 'followUp' : 'sendMessage'};
  }

  group?.addEventListener('click', event => {
    const button = event.target.closest('[data-delivery-mode]');
    const agent = selectedAgent();
    if (!button || !isAvailable(agent)) return;
    const mode = button.dataset.deliveryMode;
    if (!['steer', 'queue'].includes(mode)) return;
    preferences.set(agent.id, mode);
    sync(agent);
    elements.message?.focus({preventScroll: true});
  });

  return {
    capture(agent) {
      return sync(agent).action;
    },
    sync,
    clear() { preferences.clear(); },
    remove(id) { preferences.delete(id); },
    prune(validIds) {
      for (const id of preferences.keys()) if (!validIds.has(id)) preferences.delete(id);
    },
  };
}
