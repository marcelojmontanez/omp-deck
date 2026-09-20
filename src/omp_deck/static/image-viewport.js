(() => {
  'use strict';

  function create(onChange) {
    const bindings = new Map();
    const recordElements = new Map();
    const owners = new Map();
    const observers = new Map();

    function observerFor(container) {
      let manager = observers.get(container);
      if (manager) return manager;

      manager = {elements: new Set(), nearObserver: null, visibleObserver: null};
      if (typeof IntersectionObserver === 'function') {
        const update = property => entries => {
          const affected = new Set();
          for (const entry of entries) {
            const binding = bindings.get(entry.target);
            if (!binding || binding.container !== container) continue;
            const value = entry.isIntersecting;
            if (binding[property] === value) continue;
            binding[property] = value;
            affected.add(binding.record);
          }
          if (affected.size) onChange(affected);
        };
        manager.nearObserver = new IntersectionObserver(update('near'), {
          root: container,
          rootMargin: '600px 0px',
        });
        manager.visibleObserver = new IntersectionObserver(update('visible'), {
          root: container,
        });
      }
      observers.set(container, manager);
      return manager;
    }

    function observe(binding) {
      const manager = observerFor(binding.container);
      manager.elements.add(binding.element);
      if (manager.nearObserver) {
        manager.nearObserver.observe(binding.element);
        manager.visibleObserver.observe(binding.element);
      } else {
        binding.near = true;
        binding.visible = true;
      }
    }

    function unobserve(binding) {
      const manager = observers.get(binding.container);
      if (!manager) return;
      manager.nearObserver?.unobserve(binding.element);
      manager.visibleObserver?.unobserve(binding.element);
      manager.elements.delete(binding.element);
      if (!manager.elements.size) {
        manager.nearObserver?.disconnect();
        manager.visibleObserver?.disconnect();
        observers.delete(binding.container);
      }
    }

    function detach(element) {
      const binding = bindings.get(element);
      if (!binding) return null;

      unobserve(binding);
      bindings.delete(element);
      owners.get(binding.owner)?.delete(element);
      const elements = recordElements.get(binding.record);
      elements?.delete(element);
      if (!elements?.size) recordElements.delete(binding.record);
      return binding.record;
    }

    function attach(element, owner, container, record) {
      const binding = {element, owner, container, record, near: false, visible: false};
      bindings.set(element, binding);
      let ownerElements = owners.get(owner);
      if (!ownerElements) {
        ownerElements = new Set();
        owners.set(owner, ownerElements);
      }
      ownerElements.add(element);
      let elements = recordElements.get(record);
      if (!elements) {
        elements = new Set();
        recordElements.set(record, elements);
      }
      elements.add(element);
      observe(binding);
    }

    function replace(container, owner, entries) {
      const affected = new Set();
      for (const element of [...bindings.keys()]) {
        if (!element.isConnected) affected.add(detach(element));
      }

      const next = new Map(entries.map(({element, record}) => [element, record]));
      for (const element of [...(owners.get(owner) || [])]) {
        const binding = bindings.get(element);
        if (next.get(element) !== binding?.record || binding.container !== container) {
          affected.add(detach(element));
        }
      }
      for (const {element, record} of entries) {
        const binding = bindings.get(element);
        if (binding?.owner === owner
            && binding.container === container
            && binding.record === record) continue;
        if (binding) affected.add(detach(element));
        attach(element, owner, container, record);
        affected.add(record);
      }
      for (const [previousOwner, elements] of owners) {
        if (!elements.size) owners.delete(previousOwner);
      }
      affected.delete(null);
      return affected;
    }

    function releaseOwner(owner) {
      const affected = new Set();
      const elements = owners.get(owner);
      if (!elements) return affected;

      owners.delete(owner);
      for (const element of [...elements]) affected.add(detach(element));
      affected.delete(null);
      return affected;
    }

    function releaseWhere(predicate) {
      const affected = new Set();
      for (const [record, elements] of [...recordElements]) {
        if (!predicate(record)) continue;
        affected.add(record);
        for (const element of [...elements]) detach(element);
      }
      for (const [owner, elements] of owners) {
        if (!elements.size) owners.delete(owner);
      }
      return affected;
    }

    function elements(record) {
      return recordElements.get(record) || [];
    }

    function has(record, element) {
      return bindings.get(element)?.record === record;
    }

    function hasElements(record) {
      return Boolean(recordElements.get(record)?.size);
    }

    function near(record) {
      for (const element of elements(record)) {
        if (bindings.get(element)?.near) return true;
      }
      return false;
    }

    function geometry(record, rootRects = new Map()) {
      let near = false;
      let visible = false;
      let distance = Number.POSITIVE_INFINITY;
      for (const element of elements(record)) {
        const binding = bindings.get(element);
        if (!binding?.near || !element.isConnected) continue;
        near = true;
        visible ||= binding.visible;
        const item = element.getBoundingClientRect();
        let root = rootRects.get(binding.container);
        if (!root) {
          root = binding.container.getBoundingClientRect();
          rootRects.set(binding.container, root);
        }
        distance = Math.min(distance, Math.max(root.top - item.bottom, item.top - root.bottom, 0));
      }
      return {near, visible, distance};
    }

    function measure() {
      const rootRects = new Map();
      const geometries = new Map();
      return Object.freeze({
        get(record) {
          let measured = geometries.get(record);
          if (!measured) {
            measured = geometry(record, rootRects);
            geometries.set(record, measured);
          }
          return measured;
        },
      });
    }

    function nearElement(element) {
      return Boolean(bindings.get(element)?.near);
    }

    function clear() {
      for (const manager of observers.values()) {
        manager.nearObserver?.disconnect();
        manager.visibleObserver?.disconnect();
      }
      observers.clear();
      bindings.clear();
      recordElements.clear();
      owners.clear();
    }

    return Object.freeze({
      clear,
      elements,
      geometry,
      has,
      hasElements,
      measure,
      near,
      nearElement,
      releaseOwner,
      releaseWhere,
      replace,
    });
  }

  window.ompDeckImageViewport = Object.freeze({create});
})();
