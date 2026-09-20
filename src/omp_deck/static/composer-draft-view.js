'use strict';

function createComposerDraftView(elements, selectedRecord) {
  const feedback = elements['draft-save-feedback'];
  const status = elements['draft-save-status'];
  const conflict = elements['draft-conflict'];
  const restoreButton = elements['draft-restore-saved'];
  const overwriteButton = elements['draft-overwrite-saved'];

  function present(record) {
    if (record !== selectedRecord()) return;
    conflict.hidden = !record.conflictDraft;
    let text = '';
    let kind = '';
    if (record.conflictDraft) {
      text = 'Unsaved — this draft changed on another device';
      kind = 'error';
    } else if (record.phase === 'error') {
      text = `Unsaved — ${record.error || 'could not save draft'}`;
      kind = 'error';
    }
    status.textContent = text;
    status.hidden = !text;
    status.className = `draft-save-status${kind ? ` ${kind}` : ''}`;
    feedback.hidden = !text && !record.conflictDraft;
  }

  function clearSelection() {
    feedback.hidden = true;
    conflict.hidden = true;
    status.hidden = true;
    status.textContent = '';
  }

  function bind({
    records,
    overwriteConflict,
    restoreConflict,
    schedule,
  }) {
    restoreButton.addEventListener('click', () => { void restoreConflict(); });
    overwriteButton.addEventListener('click', overwriteConflict);
    window.addEventListener('online', () => {
      for (const record of records.values()) {
        if (record.enabled && record.dirty && !record.conflictDraft) schedule(record, 0);
      }
    });
    window.addEventListener('beforeunload', event => {
      if (![...records.values()].some(record =>
        record.enabled && (record.dirty || record.phase === 'saving')
      )) return;
      event.preventDefault();
      event.returnValue = '';
    });
  }

  return Object.freeze({bind, clearSelection, conflict, present});
}
