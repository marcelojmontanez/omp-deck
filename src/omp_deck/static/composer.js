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
  const outboundQueues = new Map();
  const revokedAttachmentUrls = new Set();
  let sessionId = null;
  let dataGeneration = 0;

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
    elements.message.value = '';
    elements['send-state'].textContent = '';
    updateCommandMode();
    renderAttachments();
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
    if (incoming.some(file => !file.type.startsWith('image/'))) {
      showToast('This transport accepts image attachments only.', 'error');
      return;
    }
    if (current.length + incoming.length > 4) {
      showToast('Attach at most 4 images.', 'error');
      return;
    }
    const total = [...current, ...incoming].reduce((bytes, item) => bytes + item.size, 0);
    if (total > 700 * 1024) {
      showToast('Images must total 700 KB or less.', 'error');
      return;
    }
    current.push(...await Promise.all(incoming.map(readAttachment)));
    if (sessionId) renderAttachments();
  }

  function reconcile(agent) {
    const pending = pendingMessages.get(agent.id);
    if (!pending?.length) return;
    const available = (agent.session?.activity || []).filter(event => event.kind === 'user');
    const delivered = [];
    const remaining = [];
    for (const item of pending) {
      const index = available.findIndex(event =>
        !item.knownMessageIds.has(event.id)
        && event.detail === item.text
        && Date.parse(event.time || 0) >= item.createdAt - 5000
      );
      if (index < 0) remaining.push(item);
      else {
        available.splice(index, 1);
        delivered.push(item);
      }
    }
    if (delivered.length) {
      delivered.flatMap(item => item.attachments).forEach(revokeAttachment);
      pendingMessages.set(agent.id, remaining);
      touchRender();
    }
  }

  async function deliver(job) {
    const {agent, id, message, outgoingAttachments, pending, generation} = job;
    try {
      const useFollowUp = displayStatus(agent) === 'working' && agent.capabilities?.followUp;
      const route = useFollowUp ? 'follow-ups' : 'messages';
      const response = await ompDeckApi.request(`/api/sessions/${encodeURIComponent(id)}/${route}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          requestId: pending?.id || requestId(),
          message,
          attachments: outgoingAttachments.map(({name, type, data}) => ({name, type, data})),
        }),
      });
      if (generation !== dataGeneration) return;
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || `Send failed (${response.status})`);
      }
      if (pending) {
        pending.accepted = true;
        touchRender();
        renderDetail();
      }
    } catch (error) {
      if (generation !== dataGeneration) return;
      if (pending) pendingMessages.set(id, pendingFor(id).filter(item => item.id !== pending.id));
      const currentDraft = loadDraft(id);
      saveDraft(id, currentDraft ? `${message}\n${currentDraft}` : message);
      attachments.set(id, mergeAttachments(outgoingAttachments, attachmentsFor(id)));
      elements['send-state'].textContent = '';
      showToast(`Message not sent: ${error.message}`, 'error');
      touchRender();
      if (sessionId === id) {
        elements.message.value = loadDraft(id);
        renderAttachments();
        elements.message.focus();
      }
      renderDetail();
    }
  }

  async function drain(id) {
    if (sending.has(id)) return;
    sending.add(id);
    try {
      const queue = outboundQueues.get(id);
      while (queue?.length) await deliver(queue.shift());
    } finally {
      sending.delete(id);
      if (!outboundQueues.get(id)?.length) outboundQueues.delete(id);
      else void drain(id);
    }
  }

  function submit(event) {
    event.preventDefault();
    const agent = selectedAgent();
    const id = agent?.id;
    if (!agent) return;
    const outgoingAttachments = [...attachmentsFor(id)];
    const typedMessage = elements.message.value.trim();
    const message = typedMessage || (outgoingAttachments.length ? 'Files attached.' : '');
    if (!message) return;
    const pending = {
      id: requestId(),
      text: message,
      attachments: outgoingAttachments,
      createdAt: Date.now(),
      accepted: false,
      knownMessageIds: new Set(
        (agent.session?.activity || []).filter(event => event.kind === 'user').map(event => event.id)
      ),
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
      agent,
      id,
      message,
      outgoingAttachments,
      pending,
      generation: dataGeneration,
    });
    void drain(id);
  }

  elements.message.addEventListener('input', () => {
    saveDraft(sessionId, elements.message.value);
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
  elements.message.addEventListener('keydown', event => {
    const mobile = window.matchMedia('(max-width: 760px)').matches;
    if (!mobile && event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      elements.composer.requestSubmit();
    }
  });
  elements.composer.addEventListener('submit', submit);

  function prune(validIds) {
    const valid = new Set(validIds);
    for (const id of new Set([
      ...drafts.keys(), ...attachments.keys(), ...pendingMessages.keys(), ...outboundQueues.keys(),
    ])) {
      if (valid.has(id)) continue;
      (attachments.get(id) || []).forEach(revokeAttachment);
      (pendingMessages.get(id) || []).flatMap(item => item.attachments || []).forEach(revokeAttachment);
      (outboundQueues.get(id) || []).flatMap(job => job.outgoingAttachments || []).forEach(revokeAttachment);
      drafts.delete(id);
      attachments.delete(id);
      pendingMessages.delete(id);
      outboundQueues.delete(id);
      sending.delete(id);
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
