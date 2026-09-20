'use strict';

function createComposerDraftView(elements, selectedRecord) {
  const checkbox = elements['save-draft'];
  const status = elements['draft-save-status'];
  const conflict = elements['draft-conflict'];
  const restoreButton = elements['draft-restore-saved'];
  const overwriteButton = elements['draft-overwrite-saved'];

  function present(record) {
    if (record !== selectedRecord()) return;
    checkbox.checked = record.desiredEnabled;
    checkbox.disabled = record.phase === 'loading';
    conflict.hidden = !record.conflictDraft;
    let text = 'Not saved on this machine';
    let kind = '';
    if (record.phase === 'loading') text = 'Checking for a saved draft…';
    else if (record.phase === 'forgotten') {
      text = 'Browser-only draft cleared; reload or switch sessions to check saved state';
    } else if (record.conflictDraft) {
      text = 'Unsaved — this draft changed on another device';
      kind = 'error';
    } else if (record.phase === 'saving') text = 'Saving…';
    else if (record.phase === 'saved' && record.desiredEnabled) {
      text = 'Saved';
      kind = 'ok';
    } else if (record.phase === 'error') {
      text = `Unsaved — ${record.error || 'could not save draft'}`;
      kind = 'error';
    } else if (record.desiredEnabled && record.dirty) text = 'Unsaved';
    status.textContent = text;
    status.className = `draft-save-status${kind ? ` ${kind}` : ''}`;
  }

  function clearSelection() {
    conflict.hidden = true;
    checkbox.checked = false;
    checkbox.disabled = true;
    status.textContent = '';
  }

  function bind({
    records,
    currentRecord,
    overwriteConflict,
    restoreConflict,
    schedule,
    setEnabled,
    showToast,
  }) {
    checkbox.addEventListener('change', () => {
      const record = currentRecord();
      if (!record) return;
      void setEnabled(record, checkbox.checked).then(saved => {
        if (saved === false) showToast('Draft preference was not saved. Your draft remains in this browser tab.', 'error');
      });
    });
    restoreButton.addEventListener('click', () => { void restoreConflict(); });
    overwriteButton.addEventListener('click', overwriteConflict);
    window.addEventListener('online', () => {
      for (const record of records.values()) {
        if (record.desiredEnabled && record.dirty && !record.conflictDraft) schedule(record, 0);
      }
    });
    window.addEventListener('beforeunload', event => {
      if (![...records.values()].some(record =>
        record.desiredEnabled && (record.dirty || record.phase === 'saving')
      )) return;
      event.preventDefault();
      event.returnValue = '';
    });
  }

  return Object.freeze({bind, clearSelection, conflict, present});
}
