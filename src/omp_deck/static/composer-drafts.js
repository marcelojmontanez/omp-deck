'use strict';

function createComposerDrafts({
  elements,
  selectedAgent,
  snapshot,
  replaceSnapshot,
  snapshotChanged,
  renderAttachments,
  showToast,
}) {
  const SAVE_DELAY_MS = 700;
  const records = new Map();
  let generation = 0;
  let selectedId = null;

  const {
    attemptedMatches,
    localMatches,
    request: requestDraft,
    validate: validateDraft,
    wireSnapshot,
  } = createComposerDraftProtocol(snapshot);
  const {
    bind: bindView,
    clearSelection,
    conflict,
    present,
  } = createComposerDraftView(elements, () => selectedId ? records.get(selectedId) : null);


  function recordFor(id) {
    if (!records.has(id)) {
      records.set(id, {
        id,
        loaded: false,
        loadPromise: null,
        desiredEnabled: false,
        confirmedEnabled: false,
        revision: null,
        mutation: 0,
        savedMutation: 0, // null keeps retained browser-only content fenced from remote replacement.
        imageMutation: 0,
        savedImageMutation: 0,
        dirty: false,
        timer: null,
        tail: Promise.resolve(),
        controller: null,
        writeControllers: new Set(),
        preparingSend: false,
        phase: 'loading',
        error: '',
        conflictDraft: null,
      });
    }
    return records.get(id);
  }



  function currentRecord() {
    return selectedId ? recordFor(selectedId) : null;
  }

  function abortRecord(record) {
    clearTimeout(record.timer);
    record.timer = null;
    record.controller?.abort();
    record.controller = null;
    for (const controller of record.writeControllers) controller.abort();
    record.writeControllers.clear();
  }


  function acceptRemote(record, remote, capturedMutation = record.mutation) {
    record.loaded = true;
    record.revision = remote.revision;
    record.confirmedEnabled = remote.enabled;
    record.desiredEnabled = remote.enabled;
    record.savedMutation = localMatches(record.id, remote) ? capturedMutation : null;
    record.savedImageMutation = record.imageMutation;
    record.dirty = false;
    record.error = '';
    record.conflictDraft = null;
    record.phase = remote.enabled ? 'saved' : 'disabled';
  }

  function restoreRemote(record, remote) {
    replaceSnapshot(record.id, remote.text, remote.images);
    record.mutation += 1;
    record.imageMutation += 1;
    snapshotChanged(record.id);
    acceptRemote(record, remote, record.mutation);
    if (selectedId === record.id) renderAttachments();
  }

  async function load(id, {replace = false} = {}) {
    const record = recordFor(id);
    if (record.loadPromise && !replace) return record.loadPromise;
    if (record.loaded && !replace) return record;
    const requestGeneration = generation;
    const capturedMutation = record.mutation;
    const controller = new AbortController();
    record.controller?.abort();
    record.controller = controller;
    record.phase = 'loading';
    record.error = '';
    present(record);
    const operation = (async () => {
      try {
        const remote = await requestDraft(id, {signal: controller.signal});
        if (controller.signal.aborted || requestGeneration !== generation || records.get(id) !== record) return record;
        record.loaded = true;
        record.revision = remote.revision;
        record.confirmedEnabled = remote.enabled;
        const localChanged = record.mutation !== capturedMutation || record.mutation !== record.savedMutation;
        if (localChanged) {
          if (localMatches(id, remote)) {
            acceptRemote(record, remote, record.mutation);
          } else if (remote.enabled) {
            record.desiredEnabled = true;
            record.dirty = true;
            record.conflictDraft = remote;
            record.phase = 'conflict';
          } else {
            record.desiredEnabled = false;
            record.dirty = false;
            record.phase = 'disabled';
          }
        } else if (remote.enabled) {
          restoreRemote(record, remote);
        } else {
          acceptRemote(record, remote, record.mutation);
        }
      } catch (error) {
        if (controller.signal.aborted || requestGeneration !== generation || records.get(id) !== record) return record;
        record.phase = 'error';
        record.error = error.message || 'could not load saved draft';
      } finally {
        if (record.controller === controller) record.controller = null;
        if (record.loadPromise === operation) record.loadPromise = null;
        present(record);
      }
      return record;
    })();
    record.loadPromise = operation;
    return operation;
  }

  function schedule(record, delay = SAVE_DELAY_MS) {
    clearTimeout(record.timer);
    record.timer = null;
    if (!record.desiredEnabled || record.conflictDraft) {
      present(record);
      return;
    }
    record.dirty = true;
    if (record.phase !== 'saving') record.phase = 'dirty';
    record.timer = setTimeout(() => {
      record.timer = null;
      void queueSave(record);
    }, delay);
    present(record);
  }

  function acceptWrite(record, remote, attempted, capturedMutation, capturedImageMutation) {
    record.revision = remote.revision;
    record.confirmedEnabled = remote.enabled;
    record.savedMutation = attempted.enabled || localMatches(record.id, attempted) ? capturedMutation : null;
    if (Object.hasOwn(attempted, 'images')) record.savedImageMutation = capturedImageMutation;
    record.error = '';
    if (record.mutation === capturedMutation && record.desiredEnabled === attempted.enabled) {
      record.dirty = false;
      record.phase = attempted.enabled ? 'saved' : 'disabled';
    } else {
      schedule(record, 0);
    }
  }

  async function reconcileUncertain(record, attempted, capturedMutation, capturedImageMutation, signal) {
    const remote = await requestDraft(record.id, {signal});
    if (attemptedMatches(record.id, attempted, remote)) {
      acceptWrite(record, remote, attempted, capturedMutation, capturedImageMutation);
      return true;
    }
    if (remote.revision === attempted.revision) return false;
    record.revision = remote.revision;
    record.confirmedEnabled = remote.enabled;
    record.conflictDraft = remote;
    record.dirty = true;
    record.phase = 'conflict';
    return false;
  }

  async function postSnapshot(record, attempted, capturedMutation, capturedImageMutation) {
    if (records.get(record.id) !== record) throw new DOMException('Saved draft request is stale', 'AbortError');
    const requestGeneration = generation;
    const controller = new AbortController();
    record.writeControllers.add(controller);
    const body = {revision: record.revision, ...attempted};
    try {
      const remote = await requestDraft(record.id, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
        signal: controller.signal,
      }, {imagesRequired: false, fallbackImages: attempted.images || []});
      if (
        controller.signal.aborted || requestGeneration !== generation
        || records.get(record.id) !== record
      ) throw new DOMException('Saved draft request is stale', 'AbortError');
      acceptWrite(record, remote, attempted, capturedMutation, capturedImageMutation);
      return remote;
    } catch (error) {
      if (
        controller.signal.aborted || requestGeneration !== generation
        || records.get(record.id) !== record
      ) throw error;
      if (error.response?.status === 409 && error.payload?.draft) {
        const remote = validateDraft(error.payload.draft);
        if (attemptedMatches(record.id, attempted, remote)) {
          acceptWrite(record, remote, attempted, capturedMutation, capturedImageMutation);
          return remote;
        }
        record.revision = remote.revision;
        record.confirmedEnabled = remote.enabled;
        record.conflictDraft = remote;
        record.dirty = true;
        record.phase = 'conflict';
        throw new Error(error.payload.error || 'Saved draft changed on another device');
      }
      try {
        if (await reconcileUncertain(
          record,
          body,
          capturedMutation,
          capturedImageMutation,
          controller.signal,
        )) return null;
      } catch (reconcileError) {
        if (
          controller.signal.aborted || requestGeneration !== generation
          || records.get(record.id) !== record
        ) throw reconcileError;
        // Keep the original write error: its outcome is unknown until a later retry.
      }
      record.dirty = true;
      record.phase = 'error';
      record.error = error.message || 'could not save draft';
      throw error;
    } finally {
      present(record);
      record.writeControllers.delete(controller);
    }
  }

  function queueOperation(record, operation) {
    const run = record.tail.catch(() => {}).then(operation);
    record.tail = run.catch(() => {});
    return run;
  }

  function queueSave(record) {
    return queueOperation(record, async () => {
      if (!record.desiredEnabled || record.conflictDraft || !record.dirty) return true;
      const capturedMutation = record.mutation;
      const capturedImageMutation = record.imageMutation;
      let attempted;
      try {
        attempted = wireSnapshot(
          record.id,
          true,
          !record.confirmedEnabled || record.imageMutation !== record.savedImageMutation,
        );
      } catch (error) {
        record.phase = 'error';
        record.error = error.message;
        present(record);
        return false;
      }
      record.phase = 'saving';
      present(record);
      try {
        await postSnapshot(record, attempted, capturedMutation, capturedImageMutation);
        return true;
      } catch {
        return false;
      }
    });
  }

  function changed(id, {images = false} = {}) {
    if (!id) return;
    const record = recordFor(id);
    record.mutation += 1;
    if (images) record.imageMutation += 1;
    if (record.desiredEnabled) schedule(record);
  }

  async function setEnabled(record, enabled) {
    clearTimeout(record.timer);
    record.timer = null;
    record.desiredEnabled = enabled;
    record.conflictDraft = null;
    record.mutation += 1;
    record.dirty = true;
    record.phase = 'saving';
    record.error = '';
    present(record);
    const capturedMutation = record.mutation;
    const capturedImageMutation = record.imageMutation;
    let attempted;
    try {
      attempted = wireSnapshot(record.id, enabled);
    } catch (error) {
      record.phase = 'error';
      record.error = error.message;
      present(record);
      return false;
    }
    const saved = await queueOperation(record, async () => {
      try {
        await postSnapshot(record, attempted, capturedMutation, capturedImageMutation);
        return true;
      } catch {
        return false;
      }
    });
    if (records.get(record.id) !== record) return null;
    if (!saved && !enabled && !record.conflictDraft) {
      record.desiredEnabled = record.confirmedEnabled;
      present(record);
    }
    return saved;
  }

  async function ensureLoadedForSend(record) {
    if (record.loaded) return true;
    await load(record.id, {replace: true});
    return record.loaded;
  }

  async function beforeSend(id) {
    const record = recordFor(id);
    if (record.preparingSend) return false;
    record.preparingSend = true;
    try {
      clearTimeout(record.timer);
      record.timer = null;
      if (!(await ensureLoadedForSend(record))) {
        record.phase = 'error';
        record.error = 'could not check the saved draft; message was not sent';
        present(record);
        return false;
      }
      await record.tail.catch(() => {});
      if (records.get(id) !== record) return false;
      clearTimeout(record.timer);
      record.timer = null;
      if (record.conflictDraft) {
        present(record);
        return false;
      }
      if (!record.confirmedEnabled && !record.desiredEnabled) return true;
      if (!record.desiredEnabled) {
        record.error = 'saved draft deletion is not confirmed; message was not sent';
        record.phase = 'error';
        present(record);
        return false;
      }
      const capturedMutation = record.mutation;
      const capturedImageMutation = record.imageMutation;
      record.phase = 'saving';
      present(record);
      const cleared = await queueOperation(record, async () => {
        try {
          await postSnapshot(
            record,
            {enabled: true, text: '', images: []},
            capturedMutation,
            capturedImageMutation,
          );
          return true;
        } catch {
          return false;
        }
      });
      if (!cleared) {
        record.error = record.error || 'could not clear the saved draft; message was not sent';
        record.phase = record.conflictDraft ? 'conflict' : 'error';
        present(record);
        return false;
      }
      return records.get(id) === record;
    } finally {
      record.preparingSend = false;
    }
  }

  async function prepareRemove(id) {
    const record = recordFor(id);
    if (!(await ensureLoadedForSend(record))) return false;
    if (!record.confirmedEnabled && !record.desiredEnabled) return true;
    return await setEnabled(record, false);
  }

  function select(id) {
    selectedId = id || null;
    if (!selectedId) {
      clearSelection();
      return;
    }
    const record = recordFor(selectedId);
    present(record);
    void load(selectedId, {replace: record.loaded && !record.dirty && record.phase !== 'saving'});
  }

  function sync(agent = selectedAgent()) {
    const active = Boolean(agent?.id && agent.id === selectedId && agent.lifecycle === 'ongoing');
    elements['draft-save-control'].hidden = !active;
    if (!active) return;
    present(recordFor(agent.id));
  }

  async function restoreConflict() {
    const record = currentRecord();
    const remote = record?.conflictDraft;
    if (!record || !remote) return;
    try {
      restoreRemote(record, remote);
      present(record);
      elements.message.focus({preventScroll: true});
    } catch (error) {
      record.phase = 'error';
      record.error = error.message || 'could not restore saved draft';
      present(record);
    }
  }

  function overwriteConflict() {
    const record = currentRecord();
    if (!record?.conflictDraft) return;
    record.revision = record.conflictDraft.revision;
    record.confirmedEnabled = record.conflictDraft.enabled;
    record.conflictDraft = null;
    record.desiredEnabled = true;
    record.imageMutation += 1;
    record.dirty = true;
    schedule(record, 0);
  }

  bindView({
    records,
    currentRecord,
    overwriteConflict,
    restoreConflict,
    schedule,
    setEnabled,
    showToast,
  });

  function remove(id) {
    const record = records.get(id);
    if (!record) return;
    abortRecord(record);
    records.delete(id);
    if (selectedId === id) select(null);
  }

  function clear({forgetSelection = false} = {}) {
    generation += 1;
    const retainedSelection = forgetSelection ? null : selectedId;
    for (const record of records.values()) abortRecord(record);
    records.clear();
    conflict.hidden = true;
    if (!retainedSelection) {
      select(null);
      return;
    }
    selectedId = retainedSelection;
    const record = recordFor(retainedSelection);
    record.phase = 'forgotten';
    present(record);
  }

  return Object.freeze({
    beforeSend,
    changed,
    clear,
    prepareRemove,
    remove,
    select,
    sync,
  });
}
