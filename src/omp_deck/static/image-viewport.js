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
      owners.get(owner).add(element);
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
      for (const element of [...(owners.get(owner) || [])]) {
        affected.add(detach(element));
      }
      for (const [previousOwner, elements] of owners) {
        if (!elements.size) owners.delete(previousOwner);
      }

      owners.set(owner, new Set());
      for (const {element, record} of entries) {
        attach(element, owner, container, record);
        affected.add(record);
      }
      if (!owners.get(owner).size) owners.delete(owner);
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

    function geometry(record) {
      let near = false;
      let visible = false;
      let distance = Number.POSITIVE_INFINITY;
      for (const element of elements(record)) {
        const binding = bindings.get(element);
        if (!binding?.near || !element.isConnected) continue;
        near = true;
        visible ||= binding.visible;
        const item = element.getBoundingClientRect();
        const root = binding.container.getBoundingClientRect();
        distance = Math.min(distance, Math.max(root.top - item.bottom, item.top - root.bottom, 0));
      }
      return {near, visible, distance};
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
      near,
      releaseOwner,
      releaseWhere,
      replace,
    });
  }

  window.ompDeckImageViewport = Object.freeze({create});
})();
