function createComposerAttachmentReads({
  attachments,
  attachmentsFor,
  selectedAgent,
  selectedSessionId,
  dataGeneration,
  revokeAttachment,
  renderAttachments,
  renderDetail,
  inputChanged,
  attachmentChanged,
  showToast,
}) {
  const readers = new Map();
  const reservations = new Map();
  const submissions = new Map();
  const failures = new Set();
  const failureNotice = 'One or more selected images could not be read. Review the attachments and send again.';

  function readAttachment(file, id) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      readers.set(reader, id);
      const finish = () => readers.delete(reader);
      reader.onload = () => {
        finish();
        try {
          resolve({
            name: file.name || 'Attachment',
            type: file.type,
            size: file.size,
            data: reader.result,
            url: URL.createObjectURL(file),
          });
        } catch (error) {
          reject(error);
        }
      };
      reader.onerror = () => {
        finish();
        reject(reader.error);
      };
      reader.onabort = () => {
        finish();
        resolve(null);
      };
      try {
        reader.readAsDataURL(file);
      } catch (error) {
        finish();
        reject(error);
      }
    });
  }

  async function add(files) {
    const generation = dataGeneration();
    const id = selectedSessionId();
    const target = selectedAgent();
    const limits = target?.attachmentLimits;
    if (target?.id !== id || !target?.capabilities?.attachments || !limits || target.lifecycle !== 'ongoing') {
      showToast('Image attachments are unavailable for this session.', 'error');
      return false;
    }
    const current = attachmentsFor(id);
    const incoming = [...files];
    if (!incoming.length) return false;
    if (incoming.some(file => !file.type.startsWith('image/'))) {
      showToast('This transport accepts image attachments only.', 'error');
      return false;
    }
    let reserved = reservations.get(id);
    if (reserved?.failed && reserved.count === 0) reserved = null;
    if (current.length + (reserved?.count || 0) + incoming.length > limits.maxImages) {
      showToast(`Attach at most ${limits.maxImages} images.`, 'error');
      return false;
    }
    const incomingBytes = incoming.reduce((bytes, item) => bytes + item.size, 0);
    const currentBytes = current.reduce((bytes, item) => bytes + item.size, 0);
    if (currentBytes + (reserved?.bytes || 0) + incomingBytes > limits.maxTotalBytes) {
      showToast(`Images must total ${formatImageLimit(limits.maxTotalBytes)} or less.`, 'error');
      return false;
    }
    if (!reserved) {
      reserved = {count: 0, bytes: 0, failed: false, tail: Promise.resolve()};
      reservations.set(id, reserved);
    }
    failures.delete(id);
    inputChanged();
    reserved.count += incoming.length;
    reserved.bytes += incomingBytes;
    const reads = Promise.allSettled(incoming.map(file => readAttachment(file, id)));
    const previous = reserved.tail;
    const operation = (async () => {
      await previous;
      const results = await reads;
      const loaded = results.flatMap(result =>
        result.status === 'fulfilled' && result.value ? [result.value] : []
      );
      const failed = results.some(result => result.status === 'rejected');
      try {
        if (
          generation !== dataGeneration()
          || attachments.get(id) !== current
          || reservations.get(id) !== reserved
        ) {
          loaded.forEach(revokeAttachment);
          return false;
        }
        current.push(...loaded);
        attachmentChanged(id);
        if (failed) {
          reserved.failed = true;
          failures.add(id);
          showToast(failureNotice, 'error');
        }
        if (selectedSessionId() === id) {
          renderAttachments();
          renderDetail();
        }
        return !failed;
      } finally {
        reserved.count -= incoming.length;
        reserved.bytes -= incomingBytes;
        if (reservations.get(id) === reserved && reserved.count === 0 && !reserved.failed) {
          reservations.delete(id);
        }
      }
    })();
    reserved.tail = operation;
    return await operation;
  }

  async function prepare(id) {
    const reserved = reservations.get(id);
    if (submissions.has(id)) return false;
    if (!reserved) return true;
    const operation = {};
    const generation = dataGeneration();
    submissions.set(id, operation);
    if (selectedSessionId() === id) renderDetail();
    try {
      let tail;
      do {
        tail = reserved.tail;
        await tail;
      } while (reservations.get(id) === reserved && reserved.tail !== tail);
      return (
        !reserved.failed
        && generation === dataGeneration()
        && submissions.get(id) === operation
        && selectedAgent()?.id === id
        && selectedSessionId() === id
      );
    } finally {
      if (submissions.get(id) === operation) submissions.delete(id);
      if (reservations.get(id) === reserved && reserved.failed && reserved.count === 0) {
        reservations.delete(id);
      }
      if (selectedSessionId() === id) renderDetail();
    }
  }

  function remove(id) {
    submissions.delete(id);
    reservations.delete(id);
    failures.delete(id);
    for (const [reader, owner] of readers) {
      if (owner === id) reader.abort();
    }
  }

  return {
    add,
    prepare,
    pending: id => reservations.has(id),
    status: id => submissions.has(id)
      ? 'Preparing selected images before sending…'
      : failures.has(id) ? failureNotice : '',
    clearStatus: id => failures.delete(id),
    remove,
    clear() {
      submissions.clear();
      reservations.clear();
      failures.clear();
      for (const reader of readers.keys()) reader.abort();
      readers.clear();
    },
  };
}

function createComposerAttachmentStore({
  attachments,
  elements,
  escapeHtml,
  commands,
  deliveryMode,
  selectedAgent,
  selectedSessionId,
  attachmentsFor,
  pendingFor,
  outboundFor,
  saveDraft,
}) {
  const revokedUrls = new Set();

  function revoke(attachment) {
    if (!attachment?.url || revokedUrls.has(attachment.url)) return;
    const viewerImage = document.getElementById('image-viewer-image');
    if (viewerImage?.src === attachment.url && document.getElementById('image-viewer')?.open) {
      window.closeImageViewer?.();
    }
    revokedUrls.add(attachment.url);
    URL.revokeObjectURL(attachment.url);
  }

  function replace(id, text, images) {
    const restored = [];
    try {
      for (const [index, image] of images.entries()) {
        const binary = atob(image.data);
        const bytes = new Uint8Array(binary.length);
        for (let offset = 0; offset < binary.length; offset += 1) bytes[offset] = binary.charCodeAt(offset);
        const blob = new Blob([bytes], {type: image.mimeType});
        restored.push({
          name: image.name || `Restored image ${index + 1}`,
          type: image.mimeType,
          size: blob.size,
          data: `data:${image.mimeType};base64,${image.data}`,
          url: URL.createObjectURL(blob),
        });
      }
    } catch (error) {
      restored.forEach(revoke);
      throw error;
    }
    for (const attachment of attachmentsFor(id)) {
      const retained = pendingFor(id).some(item => item.attachments.includes(attachment))
        || outboundFor(id).some(job => job.outgoingAttachments.includes(attachment));
      if (!retained) revoke(attachment);
    }
    saveDraft(id, text);
    attachments.set(id, restored);
    if (selectedSessionId() === id) elements.message.value = text;
  }

  function render() {
    const current = attachments.get(selectedSessionId()) || [];
    elements.attachments.innerHTML = current.map((attachment, index) =>
      `<span class="attachment" role="group" aria-label="Attachment: ${escapeHtml(attachment.name)}">
        <button class="attachment-preview" type="button" data-view-image="${attachment.url}" aria-label="Enlarge attachment: ${escapeHtml(attachment.name)}"><img src="${attachment.url}" alt="${escapeHtml(attachment.name)}"></button>
        <button type="button" data-remove-attachment="${index}" aria-label="Remove attachment: ${escapeHtml(attachment.name)}">×</button>
      </span>`
    ).join('');
    commands.sync({showSuggestions: false});
    deliveryMode.sync(selectedAgent());
  }

  return Object.freeze({
    clearRevokedUrls: () => revokedUrls.clear(),
    render,
    replace,
    revoke,
  });
}
