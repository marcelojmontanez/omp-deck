const pendingRequestAttempts = new Map();
function clearPendingRequestAttempts() {
  pendingRequestAttempts.clear();
}


function renderPendingRequest(agent) {
  const pending = agent?.pendingUiRequest;
  const key = pending ? JSON.stringify([
    agent?.id,
    pending,
    Boolean(agent?.nodeAvailable !== false),
    Boolean(agent?.bridgeConnected),
    Boolean(agent?.capabilities?.answerUiRequest),
    Boolean(agent?.capabilities?.cancelUiRequest),
  ]) : '';
  if (!pending) {
    delete el['pending-request'].dataset.requestKey;
    el['pending-request'].hidden = true;
    el['pending-request'].replaceChildren();
    return;
  }
  if (el['pending-request'].dataset.requestKey === key) return;
  el['pending-request'].dataset.requestKey = key;
  if (pending.status === 'terminal_only' || pending.kind === 'terminal_only') {
    el['pending-request'].innerHTML = `<div class="request-heading"><strong>${escapeHtml(pending.title || 'Input required')}</strong><span>Terminal only</span></div><p>${escapeHtml(pending.message || 'Answer this request in the terminal')}</p>`;
    el['pending-request'].hidden = false;
    return;
  }
  const canAnswer = Boolean(agent.nodeAvailable !== false && agent.bridgeConnected && agent.capabilities?.answerUiRequest);
  const canCancel = Boolean(agent.nodeAvailable !== false && agent.bridgeConnected && agent.capabilities?.cancelUiRequest && pending.allowCancel);
  if (!canAnswer) {
    el['pending-request'].innerHTML = `<div class="request-heading"><strong>${escapeHtml(pending.title || 'Input required')}</strong><span>Unavailable</span></div><p>This request cannot be answered from Deck while this session is disconnected or read-only.</p>`;
    el['pending-request'].hidden = false;
    return;
  }
  const question = pending.questions?.[pending.questionIndex || 0] || {};
  const options = Array.isArray(question.options) ? question.options : [];
  let choices = options.map((option, index) => `<label class="request-option">
    <input type="${question.multi ? 'checkbox' : 'radio'}" name="pending-choice" data-choice-index="${index}" ${question.checkedIndices?.includes(index) ? 'checked' : ''}>
    <span class="${question.checkedIndices?.includes(index) ? 'selected' : ''}"><strong>${escapeHtml(option.label || option.value || '')}</strong>${option.description ? `<small>${escapeHtml(option.description)}</small>` : ''}</span>
    ${question.recommended === index ? '<em>Recommended</em>' : ''}
  </label>`).join('');
  if (pending.kind === 'confirm' && !choices) {
    choices = `<label class="request-option"><input type="radio" name="pending-choice" data-confirm="true"><span><strong>Yes</strong></span></label>
      <label class="request-option"><input type="radio" name="pending-choice" data-confirm="false"><span><strong>No</strong></span></label>`;
  }
  const custom = question.allowCustom || ['input', 'editor'].includes(pending.kind)
    ? `<label class="request-custom"><span>Your answer</span><textarea rows="${pending.kind === 'editor' ? 4 : 2}" data-request-custom placeholder="${escapeHtml(question.customPlaceholder || '')}">${escapeHtml(question.prefill || '')}</textarea></label>`
    : '';
  el['pending-request'].innerHTML = `<form data-pending-form data-session-id="${escapeHtml(agent.id)}" data-request-id="${escapeHtml(pending.id)}">
    <div class="request-heading"><strong>${escapeHtml(pending.title || 'Input required')}</strong><span>${escapeHtml(pending.kind)}</span></div>
    ${pending.message ? `<p>${escapeHtml(pending.message)}</p>` : ''}
    <fieldset><legend>${escapeHtml(question.prompt || pending.title || 'Choose a response')}</legend>${choices}</fieldset>
    ${custom}
    <div class="request-actions"><button type="submit">Continue</button>${canCancel ? '<button type="button" data-cancel-request>Cancel</button>' : ''}<span role="status"></span></div>
  </form>`;
  el['pending-request'].hidden = false;
}

async function settlePendingRequest({cancel = false} = {}) {
  const agent = selectedAgent();
  const pending = agent?.pendingUiRequest;
  if (!agent || !pending || pending.status === 'terminal_only' || agent.nodeAvailable === false || !agent.bridgeConnected) return;
  if ((!cancel && !agent.capabilities?.answerUiRequest) || (cancel && !agent.capabilities?.cancelUiRequest)) return;
  const form = el['pending-request'].querySelector('[data-pending-form]');
  if (
    form?.dataset.sessionId !== agent.id
    || form?.dataset.requestId !== pending.id
  ) {
    renderPendingRequest(agent);
    return;
  }
  const status = form?.querySelector('[role="status"]');
  const question = pending.questions?.[pending.questionIndex || 0] || {};
  let value;
  if (!cancel) {
    const custom = form?.querySelector('[data-request-custom]')?.value;
    const selected = [...(form?.querySelectorAll('[name="pending-choice"]:checked') || [])];
    if (typeof custom === 'string' && custom.trim()) {
      value = custom;
    } else if (pending.kind === 'confirm') {
      if (!selected.length) {
        if (status) status.textContent = 'Choose Yes or No.';
        return;
      }
      value = selected[0].dataset.confirm === 'true';
    } else {
      const values = selected.map(input => {
        const option = question.options?.[Number(input.dataset.choiceIndex)] || {};
        return option.value || option.label || '';
      });
      if (!values.length) {
        if (status) status.textContent = 'Choose an answer.';
        return;
      }
      value = question.multi ? values : values[0];
    }
  }
  const attemptKey = JSON.stringify([agent.id, pending.id, cancel]);
  const signature = JSON.stringify(cancel ? null : value);
  const previousAttempt = pendingRequestAttempts.get(attemptKey);
  const operationRequestId = previousAttempt?.signature === signature
    ? previousAttempt.requestId
    : requestId();
  pendingRequestAttempts.set(
    attemptKey,
    {signature, requestId: operationRequestId},
  );
  form?.querySelectorAll('button,input,textarea').forEach(control => { control.disabled = true; });
  if (status) status.textContent = cancel ? 'Cancelling…' : 'Sending…';
  try {
    const requestPath = `/api/sessions/${encodeURIComponent(agent.id)}/ui-requests/${encodeURIComponent(pending.id)}/${cancel ? 'cancel' : 'respond'}`;
    const response = await ompDeckApi.request(requestPath, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({requestId: operationRequestId, ...(cancel ? {} : {value})}),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.error || `Response failed (${response.status})`);
      error.reuseRequestId = ompDeckApi.shouldReuseRequestId(response, payload);
      throw error;
    }
    await refresh(agent.machineId);
    pendingRequestAttempts.delete(attemptKey);
  } catch (error) {
    if (error.reuseRequestId === false) pendingRequestAttempts.delete(attemptKey);
    form?.querySelectorAll('button,input,textarea').forEach(control => { control.disabled = false; });
    if (status) status.textContent = error.message;
  }
}
