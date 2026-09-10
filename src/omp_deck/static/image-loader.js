(() => {
  'use strict';

  const MAX_OBJECT_URLS = 32;
  const MAX_RETAINED_BYTES = 32 * 1024 * 1024;
  const records = new Map();
  const owners = new Map();
  let generation = 0;
  let retainedBytes = 0;

  function keyFor(machineId, path) {
    return JSON.stringify([machineId, path]);
  }

  function closeViewer(url) {
    if (!url) return;
    const viewer = document.getElementById('image-viewer');
    const image = document.getElementById('image-viewer-image');
    if (image?.src !== url) return;
    image.removeAttribute('src');
    if (viewer?.open) viewer.close();
  }

  function revoke(record) {
    record.controller?.abort();
    closeViewer(record.url);
    retainedBytes -= record.size || 0;
    if (record.url) URL.revokeObjectURL(record.url);
    records.delete(record.key);
  }

  function releaseOwner(owner) {
    const keys = owners.get(owner);
    if (!keys) return;
    owners.delete(owner);
    for (const key of keys) {
      const record = records.get(key);
      if (!record) continue;
      record.owners.delete(owner);
      if (!record.owners.size) revoke(record);
    }
  }

  function clear() {
    generation += 1;
    for (const record of records.values()) revoke(record);
    records.clear();
    retainedBytes = 0;
    owners.clear();
  }

  function releaseMachine(machineId) {
    for (const record of [...records.values()]) {
      if (record.machineId !== machineId) continue;
      for (const owner of record.owners) owners.get(owner)?.delete(record.key);
      revoke(record);
    }
  }

  function showLoaded(element, url) {
    if (!element.isConnected) return;
    element.dataset.viewImage = url;
    element.classList.remove('image-loading', 'image-failed');
    element.replaceChildren(Object.assign(document.createElement('img'), {
      src: url,
      alt: 'Attached image',
      loading: 'lazy',
    }));
  }

  function showFailure(element, message) {
    if (!element.isConnected) return;
    element.classList.remove('image-loading');
    element.classList.add('image-failed');
    element.replaceChildren(Object.assign(document.createElement('span'), {
      textContent: message,
    }));
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
        if (size > 16 * 1024 * 1024) throw new Error('Image is too large to display');
        chunks.push(value);
      }
      return new Blob(chunks, {type});
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  async function load(record) {
    const currentGeneration = generation;
    try {
      const blob = await fetchImage(record.machineId, record.path, record.controller.signal);
      if (generation !== currentGeneration || records.get(record.key) !== record || !record.owners.size) return;
      if (retainedBytes + blob.size > MAX_RETAINED_BYTES) {
        throw new Error('Image display memory limit reached');
      }
      record.size = blob.size;
      retainedBytes += blob.size;
      record.url = URL.createObjectURL(blob);
      record.controller = null;
      for (const element of record.elements) showLoaded(element, record.url);
    } catch (error) {
      if (record.controller?.signal.aborted || records.get(record.key) !== record) return;
      record.controller = null;
      record.error = error.message;
      for (const element of record.elements) showFailure(element, 'Image unavailable');
    } finally {
      record.elements.clear();
    }
  }

  function hydrate(container, owner) {
    const nextKeys = new Set();
    const elements = [...container.querySelectorAll('[data-image-path][data-image-machine]')];
    for (const element of elements) {
      const machineId = element.dataset.imageMachine;
      const path = element.dataset.imagePath;
      const key = keyFor(machineId, path);
      if (nextKeys.has(key)) {
        const existing = records.get(key);
        if (existing?.url) showLoaded(element, existing.url);
        else if (existing?.error) showFailure(element, 'Image unavailable');
        else existing?.elements.add(element);
        continue;
      }
      nextKeys.add(key);
      let record = records.get(key);
      if (record?.error && ompDeckApi.machine(machineId)?.status === 'live') {
        revoke(record);
        record = null;
      }
      if (!record && records.size < MAX_OBJECT_URLS) {
        record = {
          key,
          machineId,
          path,
          owners: new Set(),
          elements: new Set(),
          size: 0,
          controller: new AbortController(),
          url: null,
          error: '',
        };
        records.set(key, record);
        void load(record);
      }
      if (!record) {
        showFailure(element, 'Image not loaded');
        continue;
      }
      record.owners.add(owner);
      if (record.url) showLoaded(element, record.url);
      else if (record.error) showFailure(element, 'Image unavailable');
      else record.elements.add(element);
    }
    const previousKeys = owners.get(owner) || new Set();
    owners.set(owner, nextKeys);
    for (const key of previousKeys) {
      if (nextKeys.has(key)) continue;
      const record = records.get(key);
      record?.owners.delete(owner);
      if (record && !record.owners.size) revoke(record);
    }
  }

  window.ompDeckImages = Object.freeze({hydrate, releaseOwner, releaseMachine, clear});
})();
