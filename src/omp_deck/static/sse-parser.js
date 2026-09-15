(() => {
  'use strict';

  // Python retains 2 MiB of normalized messages per session. A 32 MiB data cap
  // admits a multi-session replacement snapshot while bounding a single frame.
  const MAX_DATA_LENGTH = 32 * 1024 * 1024;
  const MAX_FRAME_LENGTH = MAX_DATA_LENGTH + (4 * 1024);
  const MAX_FIELD_COUNT = 65_536;
  const LINE_BLOCK_SIZE = 4096;

  function createParser(onEvent) {
    let lineParts = [];
    let fragments = [];
    let fragmentLength = 0;
    let frameLength = 0;
    let event = 'message';
    let id = null;
    let data = [];
    let dataLength = 0;
    let fieldCount = 0;

    const reset = () => {
      frameLength = 0;
      event = 'message';
      id = null;
      data = [];
      dataLength = 0;
      fieldCount = 0;
    };

    // Compact small fragments once; never re-join an increasingly large line.
    const appendLine = segment => {
      fragments.push(segment);
      fragmentLength += segment.length;
      if (fragmentLength >= LINE_BLOCK_SIZE) {
        lineParts.push(fragments.join(''));
        fragments = [];
        fragmentLength = 0;
      }
    };

    const consumeLine = () => {
      if (fragments.length) lineParts.push(fragments.join(''));
      let value = lineParts.join('');
      lineParts = [];
      fragments = [];
      fragmentLength = 0;
      if (value.endsWith('\r')) value = value.slice(0, -1);
      if (!value) {
        if (data.length) onEvent({event, id, data: data.join('\n')});
        reset();
        return;
      }
      if (value.startsWith(':')) return;
      fieldCount += 1;
      if (fieldCount > MAX_FIELD_COUNT) throw new Error('Event frame has too many fields');
      const separator = value.indexOf(':');
      const field = separator < 0 ? value : value.slice(0, separator);
      let fieldValue = separator < 0 ? '' : value.slice(separator + 1);
      if (fieldValue.startsWith(' ')) fieldValue = fieldValue.slice(1);
      if (field === 'event') event = fieldValue;
      else if (field === 'id' && !fieldValue.includes('\0') && fieldValue.length <= 256) id = fieldValue;
      else if (field === 'data') {
        dataLength += fieldValue.length + (data.length ? 1 : 0);
        if (dataLength > MAX_DATA_LENGTH) throw new Error('Event data is too large');
        data.push(fieldValue);
      }
    };

    return Object.freeze({
      push(chunk) {
        let start = 0;
        while (start < chunk.length) {
          const newline = chunk.indexOf('\n', start);
          const end = newline < 0 ? chunk.length : newline;
          const segment = chunk.slice(start, end);
          frameLength += segment.length + (newline < 0 ? 0 : 1);
          if (frameLength > MAX_FRAME_LENGTH) throw new Error('Event frame is too large');
          if (segment) appendLine(segment);
          if (newline < 0) return;
          consumeLine();
          start = newline + 1;
        }
      },
      finish() {
        // An event without its terminating blank line is incomplete and is not dispatched.
        lineParts = [];
        fragments = [];
        fragmentLength = 0;
        reset();
      },
    });
  }

  window.ompDeckSse = Object.freeze({createParser, MAX_DATA_LENGTH, MAX_FRAME_LENGTH});
})();
