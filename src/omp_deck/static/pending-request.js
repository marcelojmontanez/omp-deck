const pendingRequestAttempts = new Map();
const pendingRequestOperations = new Map();
let pendingRequestGeneration = 0;
function clearPendingRequestAttempts() {
  pendingRequestGeneration += 1;
  for (const operation of pendingRequestOperations.values()) {
    operation.controller.abort(new DOMException('Local data cleared', 'AbortError'));
  }
  pendingRequestOperations.clear();
  pendingRequestAttempts.clear();
  const form = el['pending-request'].querySelector('[data-pending-form]');
  form?.querySelectorAll('button,input,textarea').forEach(control => { control.disabled = false; });
  const status = form?.querySelector('[role="status"]');
  if (status) status.textContent = '';
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
  const checkboxes = question.multi || question.toggleSelection;
  let choices = options.map((option, index) => `<label class="request-option">
    <input type="${checkboxes ? 'checkbox' : 'radio'}" name="pending-choice" data-choice-index="${index}" ${question.checkedIndices?.includes(index) ? 'checked' : ''}>
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
  const form = el['pending-request'].querySelector('[data-pending-form]');
  if (question.toggleSelection) {
    const checked = new Set(Array.isArray(question.checkedIndices) ? question.checkedIndices : []);
    form.addEventListener('change', event => {
      const input = event.target.closest('[name="pending-choice"][data-choice-index]');
      if (!input) return;
      const index = Number(input.dataset.choiceIndex);
      form.dataset.toggleChoiceIndex = input.dataset.choiceIndex;
      for (const choice of form.querySelectorAll('[name="pending-choice"][data-choice-index]')) {
        const choiceIndex = Number(choice.dataset.choiceIndex);
        choice.checked = choiceIndex === index ? !checked.has(choiceIndex) : checked.has(choiceIndex);
        choice.nextElementSibling?.classList.toggle('selected', choice.checked);
      }
      const option = options[index] || {};
      form.querySelector('[role="status"]').textContent = `Ready to send ${option.label || option.value || 'this action'}.`;
    });
  }
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
    } else if (question.toggleSelection) {
      const index = Number(form?.dataset.toggleChoiceIndex);
      const option = Number.isSafeInteger(index) ? question.options?.[index] : null;
      value = option?.label || option?.value;
      if (typeof value !== 'string' || !value) {
        if (status) status.textContent = 'Choose one action to send.';
        return;
      }
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
  const operationKey = JSON.stringify([agent.id, pending.id]);
  if (pendingRequestOperations.has(operationKey)) return;
  const signature = JSON.stringify(cancel ? null : value);
  const previousAttempt = pendingRequestAttempts.get(attemptKey);
  const operationRequestId = previousAttempt?.signature === signature
    ? previousAttempt.requestId
    : requestId();
  pendingRequestAttempts.set(
    attemptKey,
    {signature, requestId: operationRequestId},
  );
  const generation = pendingRequestGeneration;
  const operation = {controller: new AbortController(), form};
  pendingRequestOperations.set(operationKey, operation);
  form?.querySelectorAll('button,input,textarea').forEach(control => { control.disabled = true; });
  if (status) status.textContent = cancel ? 'Cancelling…' : 'Sending…';
  try {
    const requestPath = `/api/sessions/${encodeURIComponent(agent.id)}/ui-requests/${encodeURIComponent(pending.id)}/${cancel ? 'cancel' : 'respond'}`;
    const {response, payload} = await ompDeckNetwork.fetchJsonBounded(
      requestPath,
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({requestId: operationRequestId, ...(cancel ? {} : {value})}),
        signal: operation.controller.signal,
      },
      ompDeckNetwork.STARTUP_TIMEOUT_MS,
      ompDeckApi.request,
    );
    if (
      generation !== pendingRequestGeneration
      || pendingRequestOperations.get(operationKey) !== operation
    ) return;
    if (!response.ok) {
      const error = new Error(payload?.error || `Response failed (${response.status})`);
      error.reuseRequestId = ompDeckApi.shouldReuseRequestId(response, payload || {});
      throw error;
    }
    if (payload?.requestId !== operationRequestId || payload.status !== 'accepted') {
      const error = new Error('Server did not confirm response acceptance');
      error.reuseRequestId = true;
      throw error;
    }
    await refresh(agent.machineId);
    if (
      generation !== pendingRequestGeneration
      || pendingRequestOperations.get(operationKey) !== operation
    ) return;
    pendingRequestAttempts.delete(attemptKey);
  } catch (error) {
    if (
      generation !== pendingRequestGeneration
      || pendingRequestOperations.get(operationKey) !== operation
    ) return;
    const uncertain = error?.reuseRequestId !== false;
    if (!uncertain) pendingRequestAttempts.delete(attemptKey);
    const message = error?.message || 'Response failed';
    const current = selectedAgent();
    const currentForm = el['pending-request'].querySelector('[data-pending-form]');
    if (
      current?.id === agent.id
      && current?.pendingUiRequest?.id === pending.id
      && currentForm === form
    ) {
      form.querySelectorAll('button,input,textarea').forEach(control => { control.disabled = false; });
      if (status) {
        status.textContent = `${message}${uncertain ? '. Outcome uncertain; retry will use the same request.' : ''}`;
      }
    }
  } finally {
    if (pendingRequestOperations.get(operationKey) === operation) {
      pendingRequestOperations.delete(operationKey);
    }
  }
}
