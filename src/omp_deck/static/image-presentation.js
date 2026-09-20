(() => {
  'use strict';

  function create(viewport) {
    const renderedStates = new WeakMap();

    function preserveLayout(element, measuredHeight = null) {
      if (!element.dataset.viewImage) return;
      const height = measuredHeight ?? Math.ceil(element.getBoundingClientRect().height);
      if (height > 44) element.style.setProperty('--image-placeholder-height', `${height}px`);
    }

    function showLoaded(element, url) {
      const state = `loaded:${url}`;
      if (!element.isConnected || renderedStates.get(element) === state) return;
      element.dataset.viewImage = url;
      element.disabled = false;
      element.setAttribute('aria-label', 'Enlarge attached image');
      element.classList.remove('image-loading', 'image-failed', 'image-deferred');
      element.replaceChildren(Object.assign(document.createElement('img'), {
        src: url,
        alt: 'Attached image',
        loading: 'lazy',
      }));
      renderedStates.set(element, state);
    }

    function showFailure(element) {
      if (!element.isConnected || renderedStates.get(element) === 'failed') return;
      preserveLayout(element);
      delete element.dataset.viewImage;
      element.disabled = false;
      element.setAttribute('aria-label', 'Retry attached image');
      element.classList.remove('image-loading', 'image-deferred');
      element.classList.add('image-failed');
      element.replaceChildren(Object.assign(document.createElement('span'), {
        textContent: 'Image unavailable · Retry',
      }));
      renderedStates.set(element, 'failed');
    }

    function showLoading(element) {
      if (!element.isConnected || renderedStates.get(element) === 'loading') return;
      preserveLayout(element);
      delete element.dataset.viewImage;
      element.disabled = true;
      element.setAttribute('aria-label', 'Loading attached image');
      element.classList.remove('image-failed', 'image-deferred');
      element.classList.add('image-loading');
      element.replaceChildren(Object.assign(document.createElement('span'), {
        textContent: 'Loading image…',
      }));
      renderedStates.set(element, 'loading');
    }

    function showDeferred(element, layoutPreserved = false) {
      if (!element.isConnected || renderedStates.get(element) === 'deferred') return;
      if (!layoutPreserved) preserveLayout(element);
      delete element.dataset.viewImage;
      element.disabled = false;
      element.setAttribute('aria-label', 'Load attached image');
      element.classList.remove('image-loading', 'image-failed');
      element.classList.add('image-deferred');
      element.replaceChildren(Object.assign(document.createElement('span'), {
        textContent: 'Load image',
      }));
      renderedStates.set(element, 'deferred');
    }

    function refresh(record, layoutsPreserved = false) {
      for (const element of viewport.elements(record)) {
        if (record.url && (record.pressurePinned || viewport.nearElement(element))) {
          showLoaded(element, record.url);
        } else if (record.error) {
          showFailure(element);
        } else if (record.controller) {
          showLoading(element);
        } else {
          showDeferred(element, layoutsPreserved);
        }
      }
    }

    function preserveTransitionLayouts(records) {
      const layouts = [];
      for (const record of records) {
        for (const element of viewport.elements(record)) {
          const staysLoaded = record.url
            && (record.pressurePinned || viewport.nearElement(element));
          if (element.dataset.viewImage && !staysLoaded) {
            layouts.push([element, Math.ceil(element.getBoundingClientRect().height)]);
          }
        }
      }
      for (const [element, height] of layouts) preserveLayout(element, height);
    }

    return Object.freeze({preserveTransitionLayouts, refresh, showLoaded});
  }

  window.ompDeckImagePresentation = Object.freeze({create});
})();
