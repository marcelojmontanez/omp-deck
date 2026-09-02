function createComposer({
  elements,
  selectedAgent,
  thinkingLevel,
  showToast,
  touchRender,
  renderDetail,
  refresh,
  escapeHtml,
}) {
  const drafts = new Map();
  const attachments = new Map();
  const pendingMessages = new Map();
  const sending = new Set();
  const revokedAttachmentUrls = new Set();
  let sessionId = null;
  const commands = [
    {name: '/clear', description: 'Clear context and archive earlier history'},
    {name: '/new', description: 'Start a new session'},
    {name: '/quit', description: 'Stop this OMP session'},
    {name: '/usage', description: 'Show current subscription usage'},
    {name: '/model', description: 'Choose the active model'},
    {name: '/thinking', description: 'Set the thinking level'},
    {name: '/help', description: 'Show native OMP commands'},
  ];
  let commandIndex = 0;

  function updateCommandMode() {
    const value = elements.message.value.trimStart();
    const commandMode = value.startsWith('/');
    const matches = commandMode
      ? commands.filter(command => command.name.startsWith(value.split(/\s/, 1)[0].toLowerCase()))
      : [];
    elements['command-mode'].hidden = !commandMode;
    elements.send.textContent = commandMode ? 'Run' : 'Send';
    elements['command-menu'].hidden = !matches.length;
    commandIndex = Math.min(commandIndex, Math.max(0, matches.length - 1));
    elements['command-menu'].innerHTML = matches.map((command, index) =>
      `<button type="button" role="option" aria-selected="${index === commandIndex}" data-command="${command.name}"><strong>${command.name}</strong><span>${command.description}</span></button>`
    ).join('');
    elements.message.setAttribute('aria-expanded', String(Boolean(matches.length)));
  }

  function chooseCommand(command) {
    elements.message.value = `${command} `;
    saveDraft(sessionId, elements.message.value);
    commandIndex = 0;
    updateCommandMode();
    elements.message.focus();
  }

  const draftKey = id => `omp:draft:${id}`;

  function saveDraft(id, value) {
    if (!id) return;
    drafts.set(id, value);
    if (value) sessionStorage.setItem(draftKey(id), value);
    else sessionStorage.removeItem(draftKey(id));
  }

  function loadDraft(id) {
    if (!id) return '';
    return drafts.get(id) ?? sessionStorage.getItem(draftKey(id)) ?? '';
  }

  function attachmentsFor(id) {
    if (!id) return [];
    if (!attachments.has(id)) attachments.set(id, []);
    return attachments.get(id);
  }

  function pendingFor(id) {
    if (!pendingMessages.has(id)) pendingMessages.set(id, []);
    return pendingMessages.get(id);
  }

  function mergeAttachments(...groups) {
    const seen = new Set();
    return groups.flat().filter(attachment => {
      if (seen.has(attachment.url)) return false;
      seen.add(attachment.url);
      return true;
    });
  }

  function revokeAttachment(attachment) {
    if (!attachment?.url || revokedAttachmentUrls.has(attachment.url)) return;
    revokedAttachmentUrls.add(attachment.url);
    URL.revokeObjectURL(attachment.url);
  }

  function renderAttachments() {
    elements.attachments.innerHTML = attachmentsFor(sessionId).map((attachment, index) =>
      `<span class="attachment ${attachment.type.startsWith('image/') ? '' : 'file'}">
        ${attachment.type.startsWith('image/')
          ? `<button class="attachment-preview" type="button" data-view-image="${attachment.url}" aria-label="Enlarge ${escapeHtml(attachment.name)}"><img src="${attachment.url}" alt="${escapeHtml(attachment.name)}"></button>`
          : `<span class="file-name">${escapeHtml(attachment.name)}</span>`}
        <button type="button" data-remove-attachment="${index}" aria-label="Remove attachment">×</button>
      </span>`
    ).join('');
  }

  function select(id) {
    sessionId = id || null;
    elements.message.value = loadDraft(sessionId);
    renderAttachments();
    updateCommandMode();
  }

  function readAttachment(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve({
        name: file.name || 'Attachment',
        type: file.type,
        size: file.size,
        data: reader.result,
        url: URL.createObjectURL(file),
      });
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  async function addAttachments(files) {
    const current = attachmentsFor(sessionId);
    const incoming = [...files];
    if (current.length + incoming.length > 4) {
      showToast('Attach at most 4 files.');
      return;
    }
    const total = [...current, ...incoming].reduce((bytes, item) => bytes + item.size, 0);
    if (total > 12 * 1024 * 1024) {
      showToast('Files must total 12 MB or less.');
      return;
    }
    current.push(...await Promise.all(incoming.map(readAttachment)));
    if (sessionId) renderAttachments();
  }

  function reconcile(agent) {
    const pending = pendingMessages.get(agent.id);
    if (!pending?.length) return;
    const userEvents = (agent.session?.activity || []).filter(event => event.kind === 'user');
    const delivered = [];
    const remaining = pending.filter(item => {
      const matched = userEvents.some(event =>
        event.detail === item.text && Date.parse(event.time || 0) >= item.createdAt - 5000
      );
      if (matched) delivered.push(item);
      return !matched;
    });
    if (delivered.length) {
      delivered.flatMap(item => item.attachments).forEach(revokeAttachment);
      pendingMessages.set(agent.id, remaining);
      touchRender();
    }
  }

  async function submit(event) {
    event.preventDefault();
    const agent = selectedAgent();
    const id = agent?.id;
    if (!agent || sending.has(id)) return;
    const outgoingAttachments = [...attachmentsFor(id)];
    const typedMessage = elements.message.value.trim();
    const message = typedMessage || (outgoingAttachments.length ? 'Files attached.' : '');
    if (!message) return;
    const isCommand = message.startsWith('/') && !outgoingAttachments.length;

    const pending = isCommand ? null : {
      id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
      text: message,
      attachments: outgoingAttachments,
      createdAt: Date.now(),
      accepted: false,
    };
    if (pending) pendingFor(id).push(pending);
    saveDraft(id, '');
    attachments.set(id, []);
    elements.message.value = '';
    renderAttachments();
    sending.add(id);
    elements.send.disabled = true;
    elements['send-state'].textContent = isCommand ? `Running ${message.split(/\s/, 1)[0]}…` : '';
    touchRender();
    renderDetail();

    try {
      const response = await fetch('/api/message', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          session: id,
          message,
          thinking: thinkingLevel(agent),
          attachments: outgoingAttachments.map(({name, type, data}) => ({name, type, data})),
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || `Send failed (${response.status})`);
      }
      const payload = await response.json().catch(() => ({}));
      if (pending) {
        pending.accepted = true;
        touchRender();
        renderDetail();
      } else {
        const command = payload.command || message.split(/\s/, 1)[0];
        const feedback = {
          '/clear': 'Context cleared',
          '/new': 'New session started',
          '/quit': 'Session closed',
          '/usage': 'Usage opened',
          '/model': 'Model command completed',
          '/thinking': 'Thinking level command completed',
          '/help': 'Command help opened',
        }[command] || `${command} completed`;
        elements['send-state'].textContent = feedback;
        showToast(feedback);
      }
      await refresh();
    } catch (error) {
      if (pending) pendingMessages.set(id, pendingFor(id).filter(item => item.id !== pending.id));
      const currentDraft = loadDraft(id);
      saveDraft(id, currentDraft ? `${message}\n${currentDraft}` : message);
      attachments.set(id, mergeAttachments(outgoingAttachments, attachmentsFor(id)));
      elements['send-state'].textContent = '';
      showToast(`Message not sent: ${error.message}`);
      touchRender();
      if (sessionId === id) {
        elements.message.value = loadDraft(id);
        renderAttachments();
        elements.message.focus();
      }
      renderDetail();
    } finally {
      sending.delete(id);
      if (sessionId === id) {
        elements.send.disabled = false;
        updateCommandMode();
      }
    }
  }

  elements.message.addEventListener('input', () => {
    saveDraft(sessionId, elements.message.value);
    commandIndex = 0;
    updateCommandMode();
  });
  elements.attach.addEventListener('click', () => elements['attachment-input'].click());
  elements.gallery.addEventListener('click', () => elements['gallery-input'].click());
  for (const input of [elements['gallery-input'], elements['attachment-input']]) {
    input.addEventListener('change', async event => {
      await addAttachments(event.target.files);
      event.target.value = '';
    });
  }
  elements.attachments.addEventListener('click', event => {
    const button = event.target.closest('[data-remove-attachment]');
    if (!button) return;
    const current = attachmentsFor(sessionId);
    const index = Number(button.dataset.removeAttachment);
    revokeAttachment(current[index]);
    current.splice(index, 1);
    renderAttachments();
  });
  elements.message.addEventListener('paste', async event => {
    const images = [...event.clipboardData.items]
      .filter(item => item.kind === 'file' && item.type.startsWith('image/'))
      .map(item => item.getAsFile())
      .filter(Boolean);
    if (!images.length) return;
    event.preventDefault();
    await addAttachments(images);
  });
  elements['command-menu'].addEventListener('click', event => {
    const button = event.target.closest('[data-command]');
    if (button) chooseCommand(button.dataset.command);
  });
  elements.message.addEventListener('keydown', event => {
    const options = [...elements['command-menu'].querySelectorAll('[data-command]')];
    if (!elements['command-menu'].hidden && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      event.preventDefault();
      commandIndex = (commandIndex + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
      options.forEach((option, index) => option.setAttribute('aria-selected', String(index === commandIndex)));
      return;
    }
    if (!elements['command-menu'].hidden && event.key === 'Tab') {
      event.preventDefault();
      chooseCommand(options[commandIndex].dataset.command);
      return;
    }
    if (event.key === 'Escape') {
      elements['command-menu'].hidden = true;
      return;
    }
    const mobile = window.matchMedia('(max-width: 760px)').matches;
    if (!mobile && event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      elements.composer.requestSubmit();
    }
  });
  elements.composer.addEventListener('submit', submit);

  return {
    isSending: id => sending.has(id),
    pendingFor,
    reconcile,
    select,
  };
}
