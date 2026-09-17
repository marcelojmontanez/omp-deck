function createComposer({
  elements,
  selectedAgent,
  thinkingLevel,
  commandActions,
  executeAction,
  setThinkingLevel,
  thinkingChoices,
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
  const outboundQueues = new Map();
  const retryBatches = new Map();
  const revokedAttachmentUrls = new Set();
  const queueTokens = new Map();
  const reconciledMessageIds = new Map();
  const attachmentReaders = new Set();
  const attachmentReservations = new Map();
  let sessionId = null;
  let dataGeneration = 0;
  const commands = createComposerCommands({
    elements,
    selectedAgent,
    commandActions,
    executeAction,
    setThinkingLevel,
    thinkingChoices,
    thinkingLevel,
    showToast,
    hasAttachments: () => attachmentsFor(sessionId).length > 0,
  });

  function queueToken(id) {
    if (!queueTokens.has(id)) queueTokens.set(id, {});
    return queueTokens.get(id);
  }

  function reconciledIdsFor(id) {
    if (!reconciledMessageIds.has(id)) reconciledMessageIds.set(id, new Set());
    return reconciledMessageIds.get(id);
  }

  function saveDraft(id, value) {
    if (!id) return;
    if (value) drafts.set(id, value);
    else drafts.delete(id);
  }

  function loadDraft(id) {
    return id ? drafts.get(id) || '' : '';
  }

  function clearLocalData() {
    dataGeneration += 1;
    for (const reader of attachmentReaders) reader.abort();
    attachmentReservations.clear();
    for (const items of attachments.values()) items.forEach(revokeAttachment);
    for (const items of pendingMessages.values()) {
      items.flatMap(item => item.attachments).forEach(revokeAttachment);
    }
    for (const jobs of outboundQueues.values()) {
      jobs.flatMap(job => job.outgoingAttachments).forEach(revokeAttachment);
    }
    drafts.clear();
    attachments.clear();
    pendingMessages.clear();
    outboundQueues.clear();
    retryBatches.clear();
    queueTokens.clear();
    reconciledMessageIds.clear();
    elements.message.value = '';
    elements.message.blur();
    elements['send-state'].textContent = '';
    renderAttachments();
    commands.clear();
    touchRender();
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
    return groups.flatMap(group => Array.isArray(group) ? group : []).filter(attachment => {
      if (!attachment?.url || seen.has(attachment.url)) return false;
      seen.add(attachment.url);
      return true;
    });
  }

  function revokeAttachment(attachment) {
    if (!attachment?.url || revokedAttachmentUrls.has(attachment.url)) return;
    const viewerImage = document.getElementById('image-viewer-image');
    if (viewerImage?.src === attachment.url && document.getElementById('image-viewer')?.open) {
      window.closeImageViewer?.();
    }
    revokedAttachmentUrls.add(attachment.url);
    URL.revokeObjectURL(attachment.url);
  }

  function renderAttachments() {
    elements.attachments.innerHTML = attachmentsFor(sessionId).map((attachment, index) =>
      `<span class="attachment">
        <button class="attachment-preview" type="button" data-view-image="${attachment.url}" aria-label="Enlarge ${escapeHtml(attachment.name)}"><img src="${attachment.url}" alt="${escapeHtml(attachment.name)}"></button>
        <button type="button" data-remove-attachment="${index}" aria-label="Remove attachment">×</button>
      </span>`
    ).join('');
    commands.sync({showSuggestions: false});
  }

  function select(id) {
    sessionId = id || null;
    elements.message.value = loadDraft(sessionId);
    if (!sessionId) elements.message.blur();
    renderAttachments();
    commands.reset();
  }

  function readAttachment(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      attachmentReaders.add(reader);
      const finish = () => attachmentReaders.delete(reader);
      reader.onload = () => {
        finish();
        resolve({
          name: file.name || 'Attachment',
          type: file.type,
          size: file.size,
          data: reader.result,
          url: URL.createObjectURL(file),
        });
      };
      reader.onerror = () => {
        finish();
        reject(reader.error);
      };
      reader.onabort = () => {
        finish();
        resolve(null);
      };
      reader.readAsDataURL(file);
    });
  }

  async function addAttachments(files) {
    const generation = dataGeneration;
    const targetSessionId = sessionId;
    const current = attachmentsFor(sessionId);
    const incoming = [...files];
    if (incoming.some(file => !file.type.startsWith('image/'))) {
      showToast('This transport accepts image attachments only.', 'error');
      return;
    }
    const reserved = attachmentReservations.get(targetSessionId) || {count: 0, bytes: 0};
    if (current.length + reserved.count + incoming.length > 4) {
      showToast('Attach at most 4 images.', 'error');
      return;
    }
    const incomingBytes = incoming.reduce((bytes, item) => bytes + item.size, 0);
    const currentBytes = current.reduce((bytes, item) => bytes + item.size, 0);
    if (currentBytes + reserved.bytes + incomingBytes > 700 * 1024) {
      showToast('Images must total 700 KB or less.', 'error');
      return;
    }
    if (!attachmentReservations.has(targetSessionId)) {
      attachmentReservations.set(targetSessionId, reserved);
    }
    reserved.count += incoming.length;
    reserved.bytes += incomingBytes;
    let loaded;
    try {
      loaded = (await Promise.all(incoming.map(readAttachment))).filter(Boolean);
    } finally {
      reserved.count -= incoming.length;
      reserved.bytes -= incomingBytes;
      if (
        attachmentReservations.get(targetSessionId) === reserved
        && reserved.count === 0
      ) {
        attachmentReservations.delete(targetSessionId);
      }
    }
    if (
      generation !== dataGeneration
      || attachments.get(targetSessionId) !== current
    ) {
      loaded.forEach(revokeAttachment);
      return;
    }
    current.push(...loaded);
    if (sessionId === targetSessionId) renderAttachments();
  }

  function reconcile(agent) {
    const pending = pendingMessages.get(agent.id);
    if (!pending?.length) return;
    const users = (agent.session?.activity || []).filter(event => event.kind === 'user');
    const reconciledIds = reconciledIdsFor(agent.id);
    const delivered = [];
    const remaining = [];
    for (let pendingIndex = 0; pendingIndex < pending.length; pendingIndex += 1) {
      const item = pending[pendingIndex];
      const baselineIndex = item.baselineMessageId
        ? users.findIndex(event => event.id === item.baselineMessageId)
        : -1;
      const available = item.baselineMessageId && baselineIndex >= 0
        ? users.slice(baselineIndex + 1)
        : users;
      const echo = available.find(event =>
        !item.knownMessageIds.has(event.id)
        && !reconciledIds.has(event.id)
        && event.detail === item.text
      );
      if (!echo) {
        remaining.push(...pending.slice(pendingIndex));
        break;
      }
      reconciledIds.add(echo.id);
      if (reconciledIds.size > 1000) {
        reconciledIds.delete(reconciledIds.values().next().value);
      }
      delivered.push(item);
    }
    if (delivered.length) {
      delivered.flatMap(item => item.attachments).forEach(revokeAttachment);
      pendingMessages.set(agent.id, remaining);
      touchRender();
    }
  }

  async function deliver(job) {
    const {
      id, message, outgoingAttachments, pending, generation, token,
    } = job;
    if (generation !== dataGeneration || queueTokens.get(id) !== token) return;
    const agent = state.agents.find(candidate => candidate.id === id);
    if (!agent) {
      const error = new Error('Session is no longer available');
      error.reuseRequestId = false;
      throw error;
    }
    const route = composerDeliveryRoute(agent);
    const response = await ompDeckApi.request(`/api/sessions/${encodeURIComponent(id)}/${route}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        requestId: pending?.id || requestId(),
        message,
        attachments: outgoingAttachments.map(({name, type, data}) => ({name, type, data})),
      }),
    });
    if (generation !== dataGeneration || queueTokens.get(id) !== token) return;
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const error = new Error(payload.error || `Send failed (${response.status})`);
      error.reuseRequestId = ompDeckApi.shouldReuseRequestId(response, payload);
      throw error;
    }
    if (pending) {
      pending.accepted = true;
      touchRender();
      renderDetail();
    }
  }

  function restoreUndelivered(jobs, error) {
    const active = jobs.filter(job =>
      job.generation === dataGeneration
      && queueTokens.get(job.id) === job.token
    );
    if (!active.length) return;
    const id = active[0].id;
    const pendingIds = new Set(active.map(job => job.pending?.id).filter(Boolean));
    pendingMessages.set(
      id,
      pendingFor(id).filter(item => !pendingIds.has(item.id)),
    );
    const currentDraft = loadDraft(id);
    const currentAttachments = attachmentsFor(id);
    const restoredDraft = [
      ...active.map(job => job.message),
      currentDraft,
    ].filter(Boolean).join('\n');
    saveDraft(id, restoredDraft);
    const restoredAttachments = mergeAttachments(
      ...active.map(job => job.outgoingAttachments),
      currentAttachments,
    );
    attachments.set(id, restoredAttachments);
    if (
      error.reuseRequestId !== false
      && !currentDraft
      && currentAttachments.length === 0
    ) {
      retryBatches.set(id, {draft: restoredDraft, jobs: active});
    } else {
      retryBatches.delete(id);
    }
    elements['send-state'].textContent = '';
    showToast(`Message not sent: ${error.message}`, 'error');
    touchRender();
    if (sessionId === id) {
      elements.message.value = loadDraft(id);
      renderAttachments();
    }
    renderDetail();
  }

  async function drain(id) {
    if (sending.has(id)) return;
    sending.add(id);
    try {
      const queue = outboundQueues.get(id);
      if (queue) await drainComposerQueue(queue, deliver, restoreUndelivered);
    } finally {
      sending.delete(id);
      if (!outboundQueues.get(id)?.length) outboundQueues.delete(id);
      else void drain(id);
    }
  }

  async function submit(event) {
    event.preventDefault();
    const agent = selectedAgent();
    const id = agent?.id;
    if (!agent || elements.send.disabled) return;
    const outgoingAttachments = [...attachmentsFor(id)];
    const commandDraft = elements.message.value;
    const commandRevision = commands.revision(agent);
    const command = commands.exactChoice(commandDraft, agent);
    if (command) {
      if (outgoingAttachments.length) {
        showToast('Remove image attachments before running a command.', 'error');
        commands.sync({showSuggestions: false});
        elements.message.focus();
        return;
      }
      commands.reset();
      const accepted = await commands.run(command, agent);
      const unchanged = commands.unchanged(agent, commandRevision);
      if (accepted && unchanged && loadDraft(id) === commandDraft) saveDraft(id, '');
      if (unchanged && sessionId === id && elements.message.value === commandDraft) {
        if (accepted) {
          elements.message.value = '';
          elements.message.dispatchEvent(new Event('input', {bubbles: true}));
        }
        commands.reset();
        elements.message.focus();
      }
      return;
    }
    const typedMessage = commandDraft.trim();
    const message = typedMessage || (outgoingAttachments.length ? 'Files attached.' : '');
    if (!message) return;
    elements.message.focus({preventScroll: true});
    const retry = retryBatches.get(id);
    const retryAttachments = retry
      ? mergeAttachments(...retry.jobs.map(job => job.outgoingAttachments))
      : [];
    const matchesRetry = retry
      && retry.draft === message
      && retryAttachments.length === outgoingAttachments.length
      && retryAttachments.every(
        (attachment, index) => attachment.url === outgoingAttachments[index]?.url
      );
    retryBatches.delete(id);
    if (matchesRetry) {
      saveDraft(id, '');
      attachments.set(id, []);
      elements.message.value = '';
      renderAttachments();
      for (const job of retry.jobs) {
        job.generation = dataGeneration;
        job.token = queueToken(id);
        job.pending.accepted = false;
        pendingFor(id).push(job.pending);
      }
      if (!outboundQueues.has(id)) outboundQueues.set(id, []);
      outboundQueues.get(id).push(...retry.jobs);
      touchRender();
      renderDetail();
      void drain(id);
      return;
    }
    const pending = {
      id: requestId(),
      text: message,
      attachments: outgoingAttachments,
      createdAt: Date.now(),
      accepted: false,
      knownMessageIds: new Set(
        (agent.session?.activity || []).filter(event => event.kind === 'user').map(event => event.id)
      ),
      baselineMessageId: [...(agent.session?.activity || [])]
        .reverse()
        .find(event => event.kind === 'user')?.id || null,
    };
    pendingFor(id).push(pending);
    saveDraft(id, '');
    attachments.set(id, []);
    elements.message.value = '';
    renderAttachments();
    elements['send-state'].textContent = '';
    touchRender();
    renderDetail();

    if (!outboundQueues.has(id)) outboundQueues.set(id, []);
    outboundQueues.get(id).push({
      id,
      message,
      outgoingAttachments,
      pending,
      generation: dataGeneration,
      token: queueToken(id),
    });
    void drain(id);
  }

  elements.message.addEventListener('input', () => {
    saveDraft(sessionId, elements.message.value);
    commands.inputChanged();
  });
  elements.attach.addEventListener('change', async event => {
    const pending = addAttachments(event.target.files);
    event.target.value = '';
    await pending;
  });
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
  elements.message.addEventListener('keydown', event => {
    if (commands.handleKeydown(event) || elements.send.disabled) return;
    const mobile = window.matchMedia('(max-width: 760px)').matches;
    if (!mobile && event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      elements.composer.requestSubmit();
    }
  });
  elements.composer.addEventListener('submit', submit);

  function prune(validIds) {
    const valid = new Set(validIds);
    commands.prune(validIds);
    for (const id of new Set([
      ...drafts.keys(), ...attachments.keys(), ...pendingMessages.keys(), ...outboundQueues.keys(),
      ...reconciledMessageIds.keys(),
    ])) {
      if (valid.has(id)) continue;
      queueTokens.delete(id);
      reconciledMessageIds.delete(id);
      (attachments.get(id) || []).forEach(revokeAttachment);
      (pendingMessages.get(id) || []).flatMap(item => item.attachments || []).forEach(revokeAttachment);
      (outboundQueues.get(id) || []).flatMap(job => job.outgoingAttachments || []).forEach(revokeAttachment);
      drafts.delete(id);
      attachments.delete(id);
      pendingMessages.delete(id);
      outboundQueues.delete(id);
      retryBatches.delete(id);
    }
  }

  return {
    pendingFor,
    clearLocalData,
    reconcile,
    prune,
    select,
  };
}
