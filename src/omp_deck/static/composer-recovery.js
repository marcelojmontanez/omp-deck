function setComposerDeliveryState(item, state) {
  item.state = state;
  item.accepted = state === 'accepted';
}

function mergeComposerAttachments(...groups) {
  const seen = new Set();
  return groups.flatMap(group => Array.isArray(group) ? group : []).filter(attachment => {
    if (!attachment?.url || seen.has(attachment.url)) return false;
    seen.add(attachment.url);
    return true;
  });
}

function createComposerRecovery({
  elements,
  selectedAgent,
  pendingFor,
  loadDraft,
  saveDraft,
  attachmentsFor,
  setAttachments,
  mergeAttachments,
  revokeAttachment,
  deliveryState,
  renderAttachments,
  touchRender,
  renderDetail,
  queueToken,
  dismissRetryBatch,
  setRetryBatch,
  dataGeneration,
  selectedSessionId,
}) {
  const checks = new Map();
  const recovered = new Set();
  const inFlight = new Map();
  const epochs = new Map();
  const records = new Map();

  function recoveredAttachment(raw) {
    const match = /^data:(image\/[A-Za-z0-9.+-]{1,64});base64,([A-Za-z0-9+/]*={0,2})$/.exec(raw?.data || '');
    if (!match || raw.type !== match[1] || typeof raw.name !== 'string') return null;
    let bytes;
    try {
      const decoded = atob(match[2]);
      bytes = Uint8Array.from(decoded, character => character.charCodeAt(0));
    } catch {
      return null;
    }
    const blob = new Blob([bytes], {type: match[1]});
    return {
      name: raw.name || 'Recovered image',
      type: match[1],
      size: blob.size,
      data: raw.data,
      url: URL.createObjectURL(blob),
    };
  }

  function validate(value, id, localId) {
    if (value?.version !== 1 || !Array.isArray(value.deliveries) || value.deliveries.length > 1_000) {
      throw new Error('Invalid delivery recovery response');
    }
    const states = new Set(['submitting', 'uncertain', 'accepted', 'failed', 'cancelled', 'stale']);
    return value.deliveries.map(record => {
      if (
        !record || typeof record !== 'object'
        || record.sessionId !== localId
        || typeof record.requestId !== 'string' || !record.requestId
        || !['sendMessage', 'followUp'].includes(record.action)
        || typeof record.message !== 'string'
        || !states.has(record.state)
        || !Number.isSafeInteger(record.createdAt) || record.createdAt < 0
        || !Number.isSafeInteger(record.updatedAt) || record.updatedAt < record.createdAt
        || !Number.isSafeInteger(record.expiresAt) || record.expiresAt < record.updatedAt
        || !Array.isArray(record.attachments) || record.attachments.length > 4
        || record.attachments.some(attachment => (
          !attachment || typeof attachment !== 'object'
          || typeof attachment.name !== 'string'
          || typeof attachment.type !== 'string'
          || typeof attachment.data !== 'string'
        ))
        || record.messageId !== undefined
      ) throw new Error('Invalid delivery recovery response');
      return {...record, sessionId: id};
    });
  }


  function apply(agent, incoming) {
    const pending = pendingFor(agent.id);
    const byId = new Map(pending.map(item => [item.id, item]));
    const retainedIds = new Set(incoming.map(record => record.requestId));
    for (let index = pending.length - 1; index >= 0; index -= 1) {
      const item = pending[index];
      const key = `${agent.id}\0${item.id}`;
      if (!records.has(key) || retainedIds.has(item.id)) continue;
      records.delete(key);
      pending.splice(index, 1);
      const drafted = new Set(attachmentsFor(agent.id));
      item.attachments.filter(attachment => !drafted.has(attachment)).forEach(revokeAttachment);
      dismissRetryBatch(agent.id, item.id);
    }
    for (const record of incoming) {
      records.set(`${agent.id}\0${record.requestId}`, record);
      let item = byId.get(record.requestId);
      const recoveredSubmitting = record.state === 'submitting' && (!item || item.state === 'uncertain');
      if (!item) {
        item = {
          id: record.requestId,
          text: record.message,
          attachments: record.attachments.map(recoveredAttachment).filter(Boolean),
          action: record.action,
          createdAt: record.createdAt,
          accepted: false,
          state: record.state,
        };
        pending.push(item);
        byId.set(item.id, item);
      }
      if (item.state === 'accepted' && record.state !== 'accepted') continue;
      deliveryState(item, recoveredSubmitting ? 'uncertain' : record.state);
    }
    touchRender();
    if (selectedAgent()?.id === agent.id) renderDetail();
  }

  async function recover(agent) {
    if (!agent || inFlight.has(agent.id)) return;
    const now = Date.now();
    const generation = dataGeneration();
    const epoch = epochs.get(agent.id) || 0;
    if (now - (checks.get(agent.id) || 0) < 2_000) return;
    checks.set(agent.id, now);
    const operation = (async () => {
      const {response, payload} = await ompDeckNetwork.fetchJsonBounded(
        `/api/sessions/${encodeURIComponent(agent.id)}/deliveries`,
        {cache: 'no-store'}, 120_000, ompDeckApi.request,
      );
      if (!response.ok) throw new Error(`Delivery recovery failed (${response.status})`);
      const incoming = validate(payload, agent.id, agent.localId || agent.id);
      if (generation !== dataGeneration() || epoch !== (epochs.get(agent.id) || 0)) return;
      apply(agent, incoming);
      recovered.add(agent.id);
    })().catch(error => {
      if (generation !== dataGeneration() || epoch !== (epochs.get(agent.id) || 0)) return;
      if (selectedAgent()?.id === agent.id) {
        elements['send-state'].textContent = `Delivery recovery unavailable: ${error.message}`;
        elements['send-state'].className = 'send-state error';
      }
    }).finally(() => {
      if (inFlight.get(agent.id) === operation) inFlight.delete(agent.id);
    });
    inFlight.set(agent.id, operation);
    await operation;
  }

  function reconcile(agent) {
    if (!recovered.has(agent.id) && selectedAgent()?.id === agent.id) void recover(agent);
  }

  function restoreDelivery(requestId) {
    const agent = selectedAgent();
    if (!agent) return false;
    const item = pendingFor(agent.id).find(candidate => candidate.id === requestId);
    const record = records.get(`${agent.id}\0${requestId}`);
    if (!record && !item) return false;
    const message = record?.message ?? item.text;
    const recoveredAttachments = item?.attachments
      || record.attachments.map(recoveredAttachment).filter(Boolean);
    const current = loadDraft(agent.id);
    if (current !== message) {
      saveDraft(agent.id, [current, message].filter(Boolean).join('\n'));
    }
    setAttachments(agent.id, mergeAttachments(recoveredAttachments, attachmentsFor(agent.id)));
    if (!current && ['uncertain', 'submitting'].includes(record?.state || item.state)) {
      setRetryBatch(agent.id, {jobs: [{
        id: agent.id, action: record?.action || 'sendMessage', message,
        outgoingAttachments: recoveredAttachments, pending: item,
        generation: dataGeneration(), token: queueToken(agent.id),
      }]});
    }
    if (selectedSessionId() === agent.id) {
      elements.message.value = loadDraft(agent.id);
      renderAttachments();
      elements.message.focus({preventScroll: true});
    }
    return true;
  }

  async function dismissDelivery(requestId) {
    const agent = selectedAgent();
    if (!agent || typeof requestId !== 'string' || !requestId) return false;
    const generation = dataGeneration(), epoch = epochs.get(agent.id) || 0;
    const {response, payload} = await ompDeckNetwork.fetchJsonBounded(
      `/api/sessions/${encodeURIComponent(agent.id)}/deliveries/${encodeURIComponent(requestId)}/dismiss`,
      {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'},
      12_000, ompDeckApi.request,
    );
    if (generation !== dataGeneration() || epoch !== (epochs.get(agent.id) || 0)) return false;
    if (!response.ok || payload.requestId !== requestId || payload.status !== 'dismissed') {
      throw new Error(payload?.error || `Dismissal failed (${response.status})`);
    }
    epochs.set(agent.id, epoch + 1);
    records.delete(`${agent.id}\0${requestId}`);
    const pending = pendingFor(agent.id);
    const item = pending.find(candidate => candidate.id === requestId);
    if (item) {
      pending.splice(pending.indexOf(item), 1);
      const draftsForSession = new Set(attachmentsFor(agent.id));
      item.attachments.filter(attachment => !draftsForSession.has(attachment)).forEach(revokeAttachment);
    }
    dismissRetryBatch(agent.id, requestId);
    touchRender();
    renderDetail();
    return true;
  }

  return {
    clear() {
      checks.clear();
      recovered.clear();
      inFlight.clear();
      epochs.clear();
      records.clear();
    },
    remove(id) {
      checks.delete(id);
      recovered.delete(id);
      epochs.set(id, (epochs.get(id) || 0) + 1);
      inFlight.delete(id);
      for (const key of records.keys()) {
        if (key.startsWith(`${id}\0`)) records.delete(key);
      }
    },
    recover,
    reconcile,
    restoreDelivery,
    dismissDelivery,
  };
}
