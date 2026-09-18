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
    if (viewer?.open && typeof window.closeImageViewer === 'function') {
      window.closeImageViewer();
      return;
    }
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
    element.disabled = false;
    element.setAttribute('aria-label', 'Enlarge attached image');
    element.classList.remove('image-loading', 'image-failed');
    element.replaceChildren(Object.assign(document.createElement('img'), {
      src: url,
      alt: 'Attached image',
      loading: 'lazy',
    }));
  }

  function showFailure(element, message, retry = true) {
    if (!element.isConnected) return;
    delete element.dataset.viewImage;
    element.disabled = !retry;
    element.setAttribute('aria-label', retry ? 'Retry attached image' : message);
    element.classList.remove('image-loading');
    element.classList.add('image-failed');
    element.replaceChildren(Object.assign(document.createElement('span'), {
      textContent: retry ? `${message} · Retry` : message,
    }));
  }

  function showLoading(element) {
    if (!element.isConnected) return;
    delete element.dataset.viewImage;
    element.disabled = false;
    element.setAttribute('aria-label', 'Loading attached image');
    element.classList.remove('image-failed');
    element.classList.add('image-loading');
    element.replaceChildren(Object.assign(document.createElement('span'), {
      textContent: 'Loading image…',
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
        if (size > 20 * 1024 * 1024) throw new Error('Image is too large to display');
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
    const controller = record.controller;
    try {
      const blob = await ompDeckNetwork.runBounded(
        signal => fetchImage(record.machineId, record.path, signal),
        controller.signal,
        120_000,
      );
      if (
        generation !== currentGeneration
        || records.get(record.key) !== record
        || record.controller !== controller
        || controller.signal.aborted
        || !record.owners.size
      ) return;
      if (retainedBytes + blob.size > MAX_RETAINED_BYTES) {
        throw new Error('Image display memory limit reached');
      }
      record.url = URL.createObjectURL(blob);
      record.size = blob.size;
      retainedBytes += blob.size;
      for (const element of record.elements) showLoaded(element, record.url);
    } catch (error) {
      if (
        controller.signal.aborted
        || records.get(record.key) !== record
        || record.controller !== controller
      ) return;
      record.error = error.message;
      for (const element of record.elements) showFailure(element, 'Image unavailable');
    } finally {
      if (record.controller === controller) record.controller = null;
    }
  }

  function retry(record) {
    record.controller?.abort();
    record.controller = new AbortController();
    record.error = '';
    for (const element of record.elements) showLoading(element);
    void load(record);
  }

  function recover(machineId = null, {replacePending = true} = {}) {
    for (const record of records.values()) {
      if (machineId !== null && record.machineId !== machineId) continue;
      if (!record.url && record.owners.size && (record.error || (replacePending && record.controller))) {
        retry(record);
      }
    }
  }

  function hydrate(container, owner) {
    for (const record of records.values()) {
      for (const element of record.elements) {
        if (!element.isConnected) record.elements.delete(element);
      }
    }
    const nextKeys = new Set();
    const elements = [...container.querySelectorAll('[data-image-path][data-image-machine]')];
    for (const element of elements) {
      const machineId = element.dataset.imageMachine;
      const path = element.dataset.imagePath;
      const key = keyFor(machineId, path);
      nextKeys.add(key);
      let record = records.get(key);
      if (!record && records.size < MAX_OBJECT_URLS) {
        record = {
          key,
          machineId,
          path,
          owners: new Set(),
          elements: new Set(),
          size: 0,
          controller: null,
          url: null,
          error: '',
        };
        records.set(key, record);
      }
      if (!record) {
        showFailure(element, 'Image display limit reached', false);
        continue;
      }
      record.owners.add(owner);
      record.elements.add(element);
      if (record.url) showLoaded(element, record.url);
      else if (record.error) showFailure(element, 'Image unavailable');
      else if (record.controller) showLoading(element);
      else retry(record);
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

  document.addEventListener('click', event => {
    const element = event.target.closest('.image-failed[data-image-path][data-image-machine]');
    if (!element || element.disabled) return;
    const record = records.get(keyFor(element.dataset.imageMachine, element.dataset.imagePath));
    if (!record || record.url || record.controller) return;
    event.preventDefault();
    retry(record);
  });

  window.ompDeckImages = Object.freeze({hydrate, recover, releaseOwner, releaseMachine, clear});
})();
