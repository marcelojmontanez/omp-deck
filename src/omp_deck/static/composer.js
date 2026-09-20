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
  const queueTokens = new Map();
  let sessionId = null;
  let dataGeneration = 0;
  let serverDrafts = null;
  const commands = createComposerCommands({
    elements,
    selectedAgent,
    commandActions,
    executeAction,
    setThinkingLevel,
    thinkingChoices,
    thinkingLevel,
    showToast,
    hasAttachments: () => (attachments.get(sessionId) || []).length > 0,
  });
  const deliveryMode = createComposerDeliveryMode({elements, selectedAgent});
  const attachmentStore = createComposerAttachmentStore({
    attachments,
    elements,
    escapeHtml,
    commands,
    deliveryMode,
    selectedAgent,
    selectedSessionId: () => sessionId,
    attachmentsFor,
    pendingFor,
    outboundFor: id => outboundQueues.get(id) || [],
    saveDraft,
  });
  const revokeAttachment = attachmentStore.revoke;
  const replaceDraftSnapshot = attachmentStore.replace;
  const renderAttachments = attachmentStore.render;
  const queueToken = id => queueTokens.get(id) || queueTokens.set(id, {}).get(id);
  const recovery = createComposerRecovery({
    elements,
    selectedAgent,
    pendingFor,
    loadDraft,
    saveDraft,
    draftChanged: noteDraftChanged,
    attachmentsFor,
    setAttachments: (id, items) => attachments.set(id, items),
    mergeAttachments: mergeComposerAttachments,
    revokeAttachment,
    deliveryState: setComposerDeliveryState,
    renderAttachments,
    touchRender,
    renderDetail,
    queueToken,
    dismissRetryBatch: (id, requestId) => retryBatches.get(id)?.jobs.some(job => job.pending?.id === requestId) && retryBatches.delete(id),
    setRetryBatch: (id, value) => {
      if (value) retryBatches.set(id, {...value, revision: commands.revision({id})});
      else retryBatches.delete(id);
    },
    dataGeneration: () => dataGeneration,
    selectedSessionId: () => sessionId,
  });
  const attachmentReads = createComposerAttachmentReads({
    attachments,
    attachmentsFor,
    selectedAgent,
    selectedSessionId: () => sessionId,
    dataGeneration: () => dataGeneration,
    revokeAttachment,
    renderAttachments,
    renderDetail,
    inputChanged: () => {
      recovery.inputChanged();
      serverDrafts?.changed(sessionId, {images: true});
      commands.draftChanged(sessionId);
    },
    attachmentChanged: id => serverDrafts?.changed(id, {images: true}),
    showToast,
  });
  serverDrafts = createComposerDrafts({
    elements,
    selectedAgent,
    snapshot: id => ({
      text: loadDraft(id),
      attachments: [...attachmentsFor(id)],
    }),
    replaceSnapshot: replaceDraftSnapshot,
    snapshotChanged: id => commands.draftChanged(id),
    renderAttachments,
    showToast,
  });
  function noteDraftChanged(id) {
    commands.draftChanged(id);
    serverDrafts?.changed(id, {images: true});
  }
  function saveDraft(id, value) {
    if (!id) return;
    if (value) drafts.set(id, value);
    else drafts.delete(id);
  }
  function loadDraft(id) {
    return id ? drafts.get(id) || '' : '';
  }
  function clearLocalData({forgetSelection = false} = {}) {
    dataGeneration += 1;
    serverDrafts.clear({forgetSelection});
    attachmentReads.clear();
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
    recovery.clear();
    deliveryMode.clear();
    elements.message.value = '';
    elements.attach.value = '';
    attachmentStore.clearRevokedUrls();
    renderAttachments();
    commands.clear();
    touchRender();
    renderDetail();
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

  function select(id) {
    if (sessionId !== (id || null)) recovery.select(id || null);
    sessionId = id || null;
    elements.message.value = loadDraft(sessionId);
    renderAttachments();
    serverDrafts.select(sessionId);
    commands.reset();
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
    job.attempted = true;
    const suffix = job.action === 'followUp' ? 'follow-ups' : 'messages';
    const {response, payload: body} = await ompDeckNetwork.fetchJsonBounded(
      `/api/sessions/${encodeURIComponent(id)}/${suffix}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          requestId: pending?.id || requestId(),
          message,
          attachments: outgoingAttachments.map(({name, type, data}) => ({name, type, data})),
        }),
      }, outgoingAttachments.length ? 135_000 : 15_000, ompDeckApi.request,
    );
    if (generation !== dataGeneration || queueTokens.get(id) !== token) return;
    const payload = body || {};
    if (!response.ok) {
      const error = new Error(payload.error || (
        payload.status === 'uncertain'
          ? 'Delivery outcome is uncertain'
          : `Send failed (${response.status})`
      ));
      error.reuseRequestId = ompDeckApi.shouldReuseRequestId(response, payload);
      throw error;
    }
    if (
      payload.requestId !== pending?.id
      || payload.status !== 'accepted'
      || payload.acceptance !== 'accepted'
    ) {
      const error = new Error('Server did not confirm message acceptance');
      error.reuseRequestId = true;
      throw error;
    }
    recovery.accept(id, pending);
    touchRender();
    renderDetail();
  }

  function restoreUndelivered(jobs, error) {
    const active = jobs.filter(job =>
      job.generation === dataGeneration
      && queueTokens.get(job.id) === job.token
      && job.pending?.state !== 'accepted'
    );
    if (!active.length) return;
    const id = active[0].id;
    const uncertain = error.reuseRequestId !== false && active.some(job => job.attempted);
    const pendingIds = new Set(active.map(job => job.pending?.id).filter(Boolean));
    if (uncertain) {
      for (const job of active) setComposerDeliveryState(job.pending, job.attempted ? 'uncertain' : 'failed');
    } else {
      pendingMessages.set(
        id,
        pendingFor(id).filter(item => !pendingIds.has(item.id)),
      );
    }
    const currentDraft = loadDraft(id);
    const currentAttachments = attachmentsFor(id);
    const messages = active.map(job => job.message);
    const restoredDraft = [...messages, currentDraft].filter(Boolean).join('\n');
    saveDraft(id, restoredDraft);
    const restoredAttachments = mergeComposerAttachments(
      ...active.map(job => job.outgoingAttachments),
      currentAttachments,
    );
    attachments.set(id, restoredAttachments);
    noteDraftChanged(id);
    if (uncertain && !currentDraft) {
      retryBatches.set(id, {jobs: active, revision: commands.revision({id})});
    } else {
      retryBatches.delete(id);
    }
    recovery.showState(id, uncertain ? 'uncertain' : 'failed', active.at(-1).pending.id);
    showToast(
      uncertain
        ? `Delivery outcome is uncertain: ${error.message}. Retry only after checking its status.`
        : `Message not sent: ${error.message}`,
      'error',
    );
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
      void recovery.recover(state.agents.find(agent => agent.id === id));
    }
  }


  async function submit(event) {
    event.preventDefault();
    let agent = selectedAgent();
    const id = agent?.id;
    if (!agent || elements.send.disabled) return;
    const commandRevision = commands.revision(agent);
    const action = deliveryMode.capture(agent);
    if (attachmentReads.pending(id)) {
      if (!(await attachmentReads.prepare(id))) return;
      agent = selectedAgent();
      if (agent?.id !== id || elements.send.disabled) return;
      if (!commands.unchanged(agent, commandRevision)) {
        showToast('Draft changed while images were preparing. Review it and send again.', 'error');
        return;
      }
    }
    attachmentReads.clearStatus(id);
    const outgoingAttachments = [...attachmentsFor(id)];
    const commandDraft = elements.message.value;
    const command = commands.exactChoice(commandDraft, agent);
    if (command) {
      if (outgoingAttachments.length) {
        showToast('Remove image attachments before running a command.', 'error');
        commands.sync({showSuggestions: false});
        elements.message.focus();
        return;
      }
      if (!(await serverDrafts.beforeSend(id))) return;
      agent = state.agents.find(candidate => candidate.id === id);
      if (!agent || !commands.unchanged(agent, commandRevision)) {
        serverDrafts.changed(id, {images: true});
        showToast('Draft changed while its saved copy was clearing. Review it and send again.', 'error');
        return;
      }
      if (sessionId === id) commands.reset();
      const accepted = await commands.run(command, agent);
      if (!accepted) serverDrafts.changed(id);
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
    const message = commandDraft.trim();
    if (!message && !outgoingAttachments.length) return;
    elements.message.focus({preventScroll: true});
    const retry = retryBatches.get(id);
    const retryAttachments = retry
      ? mergeComposerAttachments(...retry.jobs.map(job => job.outgoingAttachments))
      : [];
    const retryDraft = retry
      ? retry.jobs.map(job => job.message).filter(Boolean).join('\n')
      : '';
    const matchesRetry = retry
      && retry.revision === commandRevision
      && retryDraft === commandDraft
      && retryAttachments.length === outgoingAttachments.length
      && retryAttachments.every(
        (attachment, index) => attachment.url === outgoingAttachments[index]?.url
      );
    const receiptBytes = items => items.reduce((size, item) => size + item.text.length
      + item.attachments.reduce((sum, attachment) => sum + attachment.data.length, 0), 0);
    if ((outboundQueues.get(id)?.length || 0) + (matchesRetry ? retry.jobs.length : 1) > 32
        || (!matchesRetry && (pendingFor(id).length >= 1000
          || receiptBytes(pendingFor(id)) + receiptBytes([{text: message, attachments: outgoingAttachments}])
            > 24 * 1024 * 1024))) {
      showToast('Send queue is full. Wait for pending sends or dismiss old receipts.', 'error');
      return;
    }
    if (!(await serverDrafts.beforeSend(id))) return;
    agent = state.agents.find(candidate => candidate.id === id);
    if (!agent || !commands.unchanged(agent, commandRevision)) {
      serverDrafts.changed(id, {images: true});
      showToast('Draft changed while its saved copy was clearing. Review it and send again.', 'error');
      return;
    }
    retryBatches.delete(id);
    saveDraft(id, '');
    attachments.set(id, []);
    if (sessionId === id) {
      elements.message.value = '';
      renderAttachments();
    }
    if (matchesRetry) {
      for (const job of retry.jobs) {
        job.generation = dataGeneration;
        job.token = queueToken(id);
        setComposerDeliveryState(job.pending, 'submitting');
        if (!pendingFor(id).includes(job.pending)) pendingFor(id).push(job.pending);
      }
      if (!outboundQueues.has(id)) outboundQueues.set(id, []);
      outboundQueues.get(id).push(...retry.jobs);
      recovery.submitting(id, retry.jobs.at(-1).pending);
      touchRender();
      renderDetail();
      void drain(id);
      return;
    }
    const pending = {
      id: requestId(),
      text: message,
      attachments: outgoingAttachments,
      action,
      createdAt: Date.now(),
      state: 'submitting',
    };
    pendingFor(id).push(pending);
    recovery.submitting(id, pending);
    touchRender();
    renderDetail();
    if (!outboundQueues.has(id)) outboundQueues.set(id, []);
    outboundQueues.get(id).push({
      id,
      message,
      outgoingAttachments,
      action,
      pending,
      generation: dataGeneration,
      token: queueToken(id),
    });
    void drain(id);
  }

  elements.message.addEventListener('input', () => {
    recovery.inputChanged();
    saveDraft(sessionId, elements.message.value);
    commands.inputChanged();
    serverDrafts.changed(sessionId);
    deliveryMode.sync(selectedAgent());
  });
  elements.attach.addEventListener('change', async event => {
    const pending = attachmentReads.add(event.target.files);
    event.target.value = '';
    await pending;
  });
  elements.attachments.addEventListener('click', event => {
    const button = event.target.closest('[data-remove-attachment]');
    if (!button) return;
    recovery.inputChanged();
    const current = attachmentsFor(sessionId);
    const index = Number(button.dataset.removeAttachment);
    const removed = current[index];
    current.splice(index, 1);
    noteDraftChanged(sessionId);
    const retained = pendingFor(sessionId).some(item => item.attachments.includes(removed))
      || (outboundQueues.get(sessionId) || []).some(job => job.outgoingAttachments.includes(removed));
    if (!retained) revokeAttachment(removed);
    renderAttachments();
  });
  elements.message.addEventListener('paste', async event => {
    const clipboard = event.clipboardData;
    const files = [...(clipboard?.files || [])].filter(file => file.type.startsWith('image/'));
    const images = files.length ? files : [...(clipboard?.items || [])]
      .filter(item => item.kind === 'file' && item.type.startsWith('image/'))
      .map(item => item.getAsFile())
      .filter(Boolean);
    if (!images.length) return;
    event.preventDefault();
    await attachmentReads.add(images);
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

  function remove(id) {
    recovery.remove(id);
    serverDrafts.remove(id);
    (attachments.get(id) || []).forEach(revokeAttachment);
    (pendingMessages.get(id) || []).flatMap(item => item.attachments || []).forEach(revokeAttachment);
    (outboundQueues.get(id) || []).flatMap(job => job.outgoingAttachments || []).forEach(revokeAttachment);
    drafts.delete(id);
    attachments.delete(id);
    deliveryMode.remove(id);
    pendingMessages.delete(id);
    outboundQueues.delete(id);
    queueTokens.delete(id);
    attachmentReads.remove(id);
    retryBatches.delete(id);
  }

  function prune(validIds) {
    commands.prune(validIds);
    deliveryMode.prune(new Set(validIds));
  }

  return {
    prepareRemove: id => serverDrafts.prepareRemove(id),
    syncDraftSaving: agent => serverDrafts.sync(agent),
    pendingFor,
    clearLocalData,
    reconcile: recovery.reconcile,
    recover: recovery.recover,
    restoreDelivery: recovery.restoreDelivery,
    dismissDelivery: recovery.dismissDelivery,
    syncDeliveryMode: deliveryMode.sync,
    attachmentStatus: attachmentReads.status,
    remove,
    prune,
    select,
  };
}
