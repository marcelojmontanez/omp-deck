(() => {
  'use strict';

  const MAX_OBJECT_URLS = 32;
  const MAX_RETAINED_BYTES = 32 * 1024 * 1024;
  const MAX_IN_FLIGHT = 4;
  const records = new Map();
  let generation = 0;
  let retainedBytes = 0;
  let capacityEpoch = 0;
  let queueScheduled = false;
  let accessSequence = 0;

  const viewport = ompDeckImageViewport.create(handleViewportChange);

  function keyFor(machineId, path) {
    return JSON.stringify([machineId, path]);
  }

  function closeViewer(url) {
    if (!url) return;
    const viewer = document.getElementById('image-viewer');
    const image = document.getElementById('image-viewer-image');
    if (image?.src !== url) return;
    if (viewer?.open && typeof window.closeImageViewer === 'function') {
      window.closeImageViewer();
      return;
    }
    image.removeAttribute('src');
    if (viewer?.open) viewer.close();
  }

  function preserveLayout(element) {
    if (!element.dataset.viewImage) return;
    const height = Math.ceil(element.getBoundingClientRect().height);
    if (height > 44) element.style.setProperty('--image-placeholder-height', `${height}px`);
  }

  function showLoaded(element, url) {
    if (!element.isConnected) return;
    const image = element.firstElementChild;
    const alreadyLoaded = element.dataset.viewImage === url
      && image?.tagName === 'IMG'
      && image.src === url;
    element.dataset.viewImage = url;
    element.disabled = false;
    element.setAttribute('aria-label', 'Enlarge attached image');
    element.classList.remove('image-loading', 'image-failed', 'image-deferred');
    if (alreadyLoaded) return;
    element.replaceChildren(Object.assign(document.createElement('img'), {
      src: url,
      alt: 'Attached image',
      loading: 'lazy',
    }));
  }

  function showFailure(element) {
    if (!element.isConnected) return;
    preserveLayout(element);
    delete element.dataset.viewImage;
    element.disabled = false;
    element.setAttribute('aria-label', 'Retry attached image');
    element.classList.remove('image-loading', 'image-deferred');
    element.classList.add('image-failed');
    element.replaceChildren(Object.assign(document.createElement('span'), {
      textContent: 'Image unavailable · Retry',
    }));
  }

  function showLoading(element) {
    if (!element.isConnected) return;
    preserveLayout(element);
    delete element.dataset.viewImage;
    element.disabled = true;
    element.setAttribute('aria-label', 'Loading attached image');
    element.classList.remove('image-failed', 'image-deferred');
    element.classList.add('image-loading');
    element.replaceChildren(Object.assign(document.createElement('span'), {
      textContent: 'Loading image…',
    }));
  }

  function showDeferred(element) {
    if (!element.isConnected) return;
    preserveLayout(element);
    delete element.dataset.viewImage;
    element.disabled = false;
    element.setAttribute('aria-label', 'Load attached image');
    element.classList.remove('image-loading', 'image-failed');
    element.classList.add('image-deferred');
    element.replaceChildren(Object.assign(document.createElement('span'), {
      textContent: 'Load image',
    }));
  }

  function refresh(record) {
    for (const element of viewport.elements(record)) {
      if (record.url) showLoaded(element, record.url);
      else if (record.error) showFailure(element);
      else if (record.controller) showLoading(element);
      else showDeferred(element);
    }
  }

  function viewerProtects(record) {
    if (!record.url) return false;
    const viewer = document.getElementById('image-viewer');
    const image = document.getElementById('image-viewer-image');
    return Boolean(viewer?.open && image?.src === record.url);
  }

  function disposeResource(record) {
    const controller = record.controller;
    const url = record.url;
    const hadResource = Boolean(controller || url);
    record.controller = null;
    record.url = null;
    record.pressurePinned = false;
    controller?.abort();
    if (url) {
      closeViewer(url);
      retainedBytes -= record.size;
      URL.revokeObjectURL(url);
    }
    record.size = 0;
    refresh(record);
    return hadResource;
  }

  function resourceCount() {
    let count = 0;
    for (const record of records.values()) {
      if (record.url || record.controller) count += 1;
    }
    return count;
  }

  function activeFetches() {
    let count = 0;
    for (const record of records.values()) {
      if (record.controller) count += 1;
    }
    return count;
  }

  function priority(record, geometry = viewport.geometry(record)) {
    return {
      explicit: record.explicit,
      visible: geometry.visible,
      distance: geometry.distance,
      lastUsed: record.lastUsed,
    };
  }

  function comparePriority(left, right) {
    if (left.explicit !== right.explicit) return left.explicit ? -1 : 1;
    if (left.visible !== right.visible) return left.visible ? -1 : 1;
    if (left.distance !== right.distance) return left.distance - right.distance;
    return left.lastUsed - right.lastUsed;
  }
  function compareEviction(left, right) {
    if (left.near !== right.near) return left.near ? 1 : -1;
    if (left.visible !== right.visible) return left.visible ? 1 : -1;
    if (left.distance !== right.distance) return right.distance - left.distance;
    return left.lastUsed - right.lastUsed;
  }

  function evictionCandidate(target) {
    const targetExplicit = target.explicit;
    const targetVisible = viewport.geometry(target).visible;
    let selected = null;
    let selectedPriority = null;

    for (const record of records.values()) {
      if (record === target || !record.url || viewerProtects(record)) continue;
      if (!targetExplicit && record.pressurePinned) continue;

      const geometry = viewport.geometry(record);
      if (!targetExplicit) {
        if (targetVisible && geometry.visible) continue;
        if (!targetVisible && geometry.near) continue;
      }
      const candidatePriority = {...geometry, lastUsed: record.lastUsed};
      if (!selected || compareEviction(candidatePriority, selectedPriority) < 0) {
        selected = record;
        selectedPriority = candidatePriority;
      }
    }
    return selected;
  }

  function evict(record) {
    if (!record || viewerProtects(record)) return false;
    if (!disposeResource(record)) return false;
    record.blockedEpoch = capacityEpoch;
    return true;
  }

  function makeResourceSlot(target) {
    while (resourceCount() >= MAX_OBJECT_URLS) {
      if (!evict(evictionCandidate(target))) return false;
    }
    return true;
  }

  function makeByteRoom(target, size) {
    if (size > MAX_RETAINED_BYTES) return false;
    while (retainedBytes + size > MAX_RETAINED_BYTES) {
      if (!evict(evictionCandidate(target))) return false;
    }
    return true;
  }

  async function fetchImage(machineId, path, signal) {
    if (typeof path !== 'string' || !/^\/api\/blob\/[a-f0-9]{64}$/.test(path)) {
      throw new Error('Invalid image path');
    }
    const response = await ompDeckApi.request(path, {machineId, cache: 'no-store', signal});
    if (!response.ok) throw new Error(`Image returned ${response.status}`);
    const type = response.headers.get('Content-Type') || '';
    if (!type.toLowerCase().startsWith('image/')) throw new Error('Invalid image response');
    if (!response.body) throw new Error('Image response has no body');
    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    try {
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 20 * 1024 * 1024) throw new Error('Image is too large to display');
        chunks.push(value);
      }
      return new Blob(chunks, {type});
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  function schedule() {
    if (queueScheduled) return;
    queueScheduled = true;
    queueMicrotask(runQueue);
  }

  async function load(record, controller) {
    const currentGeneration = generation;
    let capacityBlocked = false;
    try {
      const blob = await ompDeckNetwork.runBounded(
        signal => fetchImage(record.machineId, record.path, signal),
        controller.signal,
        120_000,
      );
      if (generation !== currentGeneration
          || records.get(record.key) !== record
          || record.controller !== controller
          || controller.signal.aborted
          || !viewport.hasElements(record)) return;
      if (!makeByteRoom(record, blob.size)) {
        capacityBlocked = true;
        record.explicit = false;
        return;
      }
      record.url = URL.createObjectURL(blob);
      record.size = blob.size;
      retainedBytes += blob.size;
      record.controller = null;
      record.pressurePinned = record.explicit;
      record.explicit = false;
      record.blockedEpoch = -1;
      record.lastUsed = ++accessSequence;
      refresh(record);
    } catch (error) {
      if (controller.signal.aborted
          || records.get(record.key) !== record
          || record.controller !== controller) return;
      record.error = error.message;
      record.explicit = false;
    } finally {
      if (record.controller === controller) {
        record.controller = null;
        if (capacityBlocked) record.blockedEpoch = capacityEpoch;
        refresh(record);
      }
      schedule();
    }
  }

  function start(record) {
    record.error = '';
    record.blockedEpoch = -1;
    record.controller = new AbortController();
    refresh(record);
    void load(record, record.controller);
  }

  function nextCandidate() {
    let selected = null;
    let selectedPriority = null;
    for (const record of records.values()) {
      if (record.url || record.controller || record.error
          || record.blockedEpoch === capacityEpoch) continue;
      const geometry = viewport.geometry(record);
      if (!record.explicit && !geometry.near) continue;
      const candidatePriority = priority(record, geometry);
      if (!selected || comparePriority(candidatePriority, selectedPriority) < 0) {
        selected = record;
        selectedPriority = candidatePriority;
      }
    }
    return selected;
  }

  function runQueue() {
    queueScheduled = false;
    while (activeFetches() < MAX_IN_FLIGHT) {
      const record = nextCandidate();
      if (!record) return;
      if (!makeResourceSlot(record)) {
        record.explicit = false;
        record.blockedEpoch = capacityEpoch;
        refresh(record);
        continue;
      }
      start(record);
    }
  }

  function handleViewportChange(affected) {
    capacityEpoch += 1;
    for (const record of affected) {
      record.pressurePinned = false;
      if (!record.explicit && !viewport.near(record) && record.controller) {
        record.controller.abort();
        record.controller = null;
        record.blockedEpoch = capacityEpoch;
      }
      refresh(record);
    }
    schedule();
  }

  function discardUnusedRecords() {
    let released = false;
    for (const record of [...records.values()]) {
      if (viewport.hasElements(record)) continue;
      released = disposeResource(record) || released;
      records.delete(record.key);
    }
    return released;
  }

  function admit(machineId, path) {
    const key = keyFor(machineId, path);
    let record = records.get(key);
    if (record) return record;
    record = {
      key,
      machineId,
      path,
      controller: null,
      url: null,
      size: 0,
      error: '',
      explicit: false,
      pressurePinned: false,
      blockedEpoch: -1,
      lastUsed: 0,
    };
    records.set(key, record);
    return record;
  }

  function hydrate(container, owner) {
    const entries = [...container.querySelectorAll('[data-image-path][data-image-machine]')]
      .map(element => ({
        element,
        record: admit(element.dataset.imageMachine, element.dataset.imagePath),
      }));
    viewport.replace(container, owner, entries);
    for (const {record} of entries) refresh(record);
    if (discardUnusedRecords()) capacityEpoch += 1;
    schedule();
  }

  function releaseOwner(owner) {
    const affected = viewport.releaseOwner(owner);
    if (!affected.size) return;
    if (discardUnusedRecords()) capacityEpoch += 1;
    schedule();
  }

  function releaseMachine(machineId) {
    viewport.releaseWhere(record => record.machineId === machineId);
    let released = false;
    for (const record of [...records.values()]) {
      if (record.machineId !== machineId) continue;
      released = disposeResource(record) || released;
      records.delete(record.key);
    }
    if (released) capacityEpoch += 1;
    schedule();
  }

  function recover(machineId = null, {replacePending = true} = {}) {
    let released = false;
    for (const record of records.values()) {
      if (machineId !== null && record.machineId !== machineId) continue;
      if (record.url) continue;
      if (record.controller && !replacePending) continue;
      if (record.controller) {
        record.controller.abort();
        record.controller = null;
        released = true;
      }
      record.error = '';
      record.blockedEpoch = -1;
      refresh(record);
    }
    if (released) capacityEpoch += 1;
    schedule();
  }

  function clear() {
    generation += 1;
    viewport.clear();
    for (const record of records.values()) disposeResource(record);
    records.clear();
    retainedBytes = 0;
    capacityEpoch += 1;
  }

  function leastImportantPending(except) {
    let selected = null;
    let selectedPriority = null;
    for (const record of records.values()) {
      if (record === except || !record.controller || record.explicit) continue;
      const candidatePriority = priority(record);
      if (!selected || comparePriority(candidatePriority, selectedPriority) > 0) {
        selected = record;
        selectedPriority = candidatePriority;
      }
    }
    return selected;
  }

  function prioritize(record) {
    record.error = '';
    record.explicit = true;
    record.blockedEpoch = -1;
    record.lastUsed = ++accessSequence;
    if (activeFetches() >= MAX_IN_FLIGHT) {
      const victim = leastImportantPending(record);
      if (victim) {
        victim.controller.abort();
        victim.controller = null;
        victim.blockedEpoch = capacityEpoch;
        refresh(victim);
      }
    }
    refresh(record);
    schedule();
  }

  document.addEventListener('click', event => {
    const element = event.target.closest('[data-image-path][data-image-machine]');
    if (!element) return;
    const record = records.get(keyFor(element.dataset.imageMachine, element.dataset.imagePath));
    if (!record || !viewport.has(record, element)) return;
    if (element.dataset.viewImage) {
      record.lastUsed = ++accessSequence;
      return;
    }
    if (!element.classList.contains('image-deferred')
        && !element.classList.contains('image-failed')) return;
    event.preventDefault();
    prioritize(record);
  });

  document.getElementById('image-viewer')?.addEventListener('close', () => {
    capacityEpoch += 1;
    schedule();
  });

  window.ompDeckImages = Object.freeze({hydrate, recover, releaseOwner, releaseMachine, clear});
})();
