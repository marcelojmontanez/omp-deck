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
