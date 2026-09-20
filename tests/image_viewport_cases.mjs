import assert from 'node:assert/strict';

const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM1sAAAAASUVORK5CYII=';
const gallerySelector = '[data-image-viewport-gallery]';
const imageSelector = index => `[data-image-viewport-index="${index}"]`;

async function waitForImage(page, index) {
  await page.waitForFunction(selector => {
    const button = document.querySelector(selector);
    const image = button?.querySelector('img');
    return Boolean(button?.dataset.viewImage && image?.complete && image.naturalWidth === 1);
  }, {timeout: 5000}, imageSelector(index));
}

async function scrollToImage(page, index) {
  await page.evaluate(({gallerySelector, selector}) => {
    const gallery = document.querySelector(gallerySelector);
    const button = document.querySelector(selector);
    gallery.scrollTop = button.offsetTop;
    gallery.dispatchEvent(new Event('scroll'));
  }, {gallerySelector, selector: imageSelector(index)});
  await waitForImage(page, index);
}

async function installGallery(page) {
  await page.evaluate(({png}) => {
    const originalRequest = ompDeckApi.request;
    const fixture = {
      originalRequest,
      owner: 'viewport-image-gallery',
      sharedOwner: 'viewport-image-shared',
      staleOwner: 'viewport-image-stale',
      visibleOwner: 'viewport-image-visible-pressure',
      calls: new Map(),
      held: null,
      largePaths: new Set(),
    };
    const base = Uint8Array.from(atob(png), character => character.charCodeAt(0));
    const paddedPng = size => {
      const bytes = new Uint8Array(size);
      const iendOffset = base.length - 12;
      bytes.set(base.subarray(0, iendOffset));
      const view = new DataView(bytes.buffer);
      const payloadLength = bytes.length - base.length - 12;
      view.setUint32(iendOffset, payloadLength);
      bytes.set([110, 112, 65, 100], iendOffset + 4); // Valid ancillary npAd chunk.
      const crcTable = Uint32Array.from({length: 256}, (_, value) => {
        let crc = value;
        for (let bit = 0; bit < 8; bit += 1) {
          crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
        }
        return crc;
      });
      let crc = 0xffffffff;
      for (let offset = iendOffset + 4; offset < bytes.length - 16; offset += 1) {
        crc = crcTable[(crc ^ bytes[offset]) & 0xff] ^ (crc >>> 8);
      }
      view.setUint32(bytes.length - 16, (crc ^ 0xffffffff) >>> 0);
      bytes.set(base.subarray(iendOffset), bytes.length - 12);
      return bytes;
    };
    const bytes = paddedPng(3 * 1024 * 1024);
    const largeBytes = paddedPng(12 * 1024 * 1024);
    const response = body => new Response(body, {headers: {'Content-Type': 'image/png'}});
    const pathFor = number => `/api/blob/${number.toString(16).padStart(64, '0')}`;
    ompDeckApi.request = async (path, options = {}) => {
      fixture.calls.set(path, (fixture.calls.get(path) || 0) + 1);
      if (fixture.held?.path === path) {
        fixture.held.signal = options.signal;
        fixture.held.started = true;
        return new Promise(resolve => {
          fixture.held.resolve = () => resolve(response(bytes));
        });
      }
      return response(fixture.largePaths.has(path) ? largeBytes : bytes);
    };
    const gallery = document.createElement('div');
    gallery.dataset.imageViewportGallery = '';
    Object.assign(gallery.style, {
      position: 'fixed', top: '8px', left: '8px', zIndex: '10000',
      width: '330px', height: '288px', overflowY: 'auto', background: '#000',
    });
    const button = number => {
      const element = document.createElement('button');
      element.type = 'button';
      element.className = 'message-image image-loading';
      element.dataset.imageMachine = state.selectedMachineId || 'local';
      element.dataset.imagePath = pathFor(number);
      element.dataset.imageViewportIndex = String(number);
      Object.assign(element.style, {
        display: 'block', width: '100%', height: '96px', minHeight: '96px',
      });
      return element;
    };
    gallery.replaceChildren(...Array.from({length: 48}, (_, index) => button(index)));
    document.body.append(gallery);
    fixture.gallery = gallery;
    fixture.button = button;
    window.imageViewportFixture = fixture;
    ompDeckImages.clear();
    ompDeckImages.hydrate(gallery, fixture.owner);
  }, {png: PNG});
}

async function verifySharedOwner(page, index) {
  await page.evaluate(index => {
    const fixture = window.imageViewportFixture;
    const shared = document.createElement('div');
    Object.assign(shared.style, {
      position: 'fixed', top: '310px', left: '8px', zIndex: '10000', width: '80px', height: '80px',
    });
    const button = fixture.button(index);
    shared.append(button);
    document.body.append(shared);
    fixture.shared = shared;
    ompDeckImages.hydrate(shared, fixture.sharedOwner);
  }, index);
  await page.waitForFunction(() => {
    const image = imageViewportFixture.shared.querySelector('img');
    return image?.complete && image.naturalWidth === 1;
  });
  const retained = await page.evaluate(index => {
    const fixture = window.imageViewportFixture;
    const visible = document.querySelector(`[data-image-viewport-index="${index}"]`);
    const url = visible.dataset.viewImage;
    ompDeckImages.releaseOwner(fixture.sharedOwner);
    fixture.shared.remove();
    return new Promise(resolve => requestAnimationFrame(() => resolve({
      sameUrl: visible.dataset.viewImage === url,
      decoded: visible.querySelector('img')?.naturalWidth === 1,
    })));
  }, index);
  assert.deepEqual(retained, {sameUrl: true, decoded: true},
    'Removing one owner must not invalidate the remaining visible shared image');
  await page.click(imageSelector(index));
  await page.waitForFunction(() => {
    const viewer = document.getElementById('image-viewer');
    const image = document.getElementById('image-viewer-image');
    return viewer.open && image.complete && image.naturalWidth === 1;
  });
  assert.equal(await page.$eval('#image-viewer-image', image => image.src),
    await page.$eval(imageSelector(index), button => button.dataset.viewImage),
    'The remaining owner must still open the shared image after its peer is released');
  await page.evaluate(() => window.closeImageViewer());
}

async function verifyVisiblePressure(page) {
  await page.evaluate(() => {
    const fixture = window.imageViewportFixture;
    ompDeckImages.releaseOwner(fixture.owner);
    fixture.gallery.remove();
    const container = document.createElement('div');
    container.dataset.imageViewportVisible = '';
    Object.assign(container.style, {
      position: 'fixed', top: '8px', left: '8px', zIndex: '10000',
      width: '330px', height: '288px', overflowY: 'auto', background: '#000',
    });
    for (const index of [200, 201, 202]) {
      const button = fixture.button(index);
      fixture.largePaths.add(button.dataset.imagePath);
      container.append(button);
    }
    document.body.append(container);
    fixture.visiblePressure = container;
    ompDeckImages.hydrate(container, fixture.visibleOwner);
  });
  try {
    await page.waitForFunction(() => {
      const buttons = [...imageViewportFixture.visiblePressure.children];
      const loaded = button => Boolean(button.dataset.viewImage);
      const deferred = button => button.classList.contains('image-deferred');
      return buttons.some(loaded) && buttons.some(deferred)
        && buttons.every(button => loaded(button) || deferred(button));
    }, {timeout: 8000});
    const snapshot = () => page.evaluate(() => {
      const fixture = window.imageViewportFixture;
      const buttons = [...fixture.visiblePressure.children];
      return {
        calls: buttons.reduce((total, button) =>
          total + (fixture.calls.get(button.dataset.imagePath) || 0), 0),
        deferred: Number(buttons.find(button => button.classList.contains('image-deferred'))
          ?.dataset.imageViewportIndex),
      };
    });
    const settle = () => page.evaluate(() => new Promise(resolve => {
      let frames = 4;
      const next = () => { if ((frames -= 1) === 0) resolve(); else requestAnimationFrame(next); };
      requestAnimationFrame(next);
    }));
    await settle();
    const before = await snapshot();
    await settle();
    assert.equal((await snapshot()).calls, before.calls,
      'Visible images that exceed the byte budget must settle without retrying in a loop');
    await page.evaluate(() => {
      const container = imageViewportFixture.visiblePressure;
      const spacer = document.createElement('div');
      spacer.style.height = '120px';
      container.append(spacer);
      container.scrollTop = 97;
    });
    await waitForImage(page, before.deferred);
    await page.evaluate(() => {
      const container = imageViewportFixture.visiblePressure;
      container.lastElementChild.remove();
      container.scrollTop = 0;
    });
    await settle();
    await page.waitForFunction(() => {
      const container = imageViewportFixture.visiblePressure;
      return container.querySelector('.image-deferred') && !container.querySelector('.image-loading');
    });
    const manual = await snapshot();
    await page.evaluate(index => {
      const button = document.querySelector(`[data-image-viewport-index=\"${index}\"]`);
      button.click();
    }, manual.deferred);
    await waitForImage(page, manual.deferred);
    assert.equal(await page.$eval('[data-image-viewport-gallery], [data-image-viewport-visible]',
      container => container.scrollTop), 0,
    'Explicitly requesting a deferred visible image must not depend on scrolling');
    await settle();
    const after = await snapshot();
    await settle();
    assert.equal((await snapshot()).calls, after.calls,
      'Manual progress under visible byte pressure must not trigger fetch thrashing');
  } finally {
    await page.evaluate(() => {
      const fixture = window.imageViewportFixture;
      ompDeckImages.releaseOwner(fixture.visibleOwner);
      fixture.visiblePressure?.remove();
      fixture.largePaths.clear();
    });
  }
}

async function verifyStaleRelease(page) {
  await page.evaluate(() => {
    const fixture = window.imageViewportFixture;
    ompDeckImages.releaseOwner(fixture.owner);
    fixture.gallery.remove();
    const container = document.createElement('div');
    Object.assign(container.style, {
      position: 'fixed', top: '8px', left: '8px', zIndex: '10000', width: '120px', height: '120px',
    });
    const button = fixture.button(255);
    container.append(button);
    document.body.append(container);
    fixture.stale = {container, button};
    fixture.held = {path: button.dataset.imagePath, started: false, signal: null, resolve: null};
    ompDeckImages.hydrate(container, fixture.staleOwner);
  });
  await page.waitForFunction(() => imageViewportFixture.held?.started
    && imageViewportFixture.held.signal);
  const stale = await page.evaluate(async () => {
    const fixture = window.imageViewportFixture;
    const {button} = fixture.stale;
    ompDeckImages.releaseOwner(fixture.staleOwner);
    fixture.stale.container.remove();
    fixture.held.resolve();
    await Promise.resolve();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return {
      aborted: fixture.held.signal.aborted,
      resurrected: Boolean(button.dataset.viewImage || button.querySelector('img')),
    };
  });
  assert.deepEqual(stale, {aborted: true, resurrected: false},
    'A late response must not resurrect content after its owner is released');
}

async function cleanupGallery(page) {
  await page.evaluate(() => {
    const fixture = window.imageViewportFixture;
    if (!fixture) return;
    fixture.held?.resolve?.();
    for (const owner of [
      fixture.owner, fixture.sharedOwner, fixture.staleOwner, fixture.visibleOwner,
    ]) {
      ompDeckImages.releaseOwner(owner);
    }
    if (document.getElementById('image-viewer').open) window.closeImageViewer?.();
    fixture.gallery?.remove();
    fixture.shared?.remove();
    fixture.stale?.container.remove();
    fixture.visiblePressure?.remove();
    ompDeckApi.request = fixture.originalRequest;
    delete window.imageViewportFixture;
    ompDeckImages.clear();
    touchRender();
    renderDetail();
  });
}

export async function verifyViewportImages(page) {
  const originalViewport = page.viewport();
  await page.setViewport({width: 390, height: 844, deviceScaleFactor: 1});
  try {
    await installGallery(page);
    await waitForImage(page, 0);
    const initial = await page.evaluate(() => {
      const fixture = window.imageViewportFixture;
      const first = document.querySelector('[data-image-viewport-index="0"]');
      return {url: first.dataset.viewImage, calls: fixture.calls.get(first.dataset.imagePath)};
    });
    for (const index of [4, 8, 12, 16, 20, 24, 28]) await scrollToImage(page, index);
    const beforeManualLoad = await page.$eval(gallerySelector, gallery => gallery.scrollTop);
    await page.evaluate(selector => document.querySelector(selector).click(), imageSelector(44));
    await page.waitForFunction(selector => Boolean(
      document.querySelector(selector)?.dataset.viewImage,
    ), {timeout: 5000}, imageSelector(44));
    assert.equal(await page.$eval(imageSelector(44), async button => {
      const image = new Image();
      image.src = button.dataset.viewImage;
      await image.decode();
      return image.naturalWidth;
    }), 1, 'Explicit loading must produce a usable image even before its lazy thumbnail is visible');
    assert.equal(await page.$eval(gallerySelector, gallery => gallery.scrollTop), beforeManualLoad,
      'Explicitly requesting a deferred image must load it without requiring a scroll');
    for (const index of [32, 36, 40]) await scrollToImage(page, index);
    await verifySharedOwner(page, 40);

    await page.click(imageSelector(40));
    await page.waitForFunction(() => {
      const viewer = document.getElementById('image-viewer');
      const image = document.getElementById('image-viewer-image');
      return viewer.open && image.complete && image.naturalWidth === 1;
    });
    const enlargedUrl = await page.$eval('#image-viewer-image', image => image.src);
    for (const index of [44, 36, 28, 20, 12, 4, 0]) await scrollToImage(page, index);
    assert.deepEqual(await page.$eval('#image-viewer-image', image => ({
      open: image.closest('dialog').open, source: image.src, width: image.naturalWidth,
    })), {open: true, source: enlargedUrl, width: 1},
    'The enlarged image must survive capacity pressure while its gallery image is offscreen');
    await page.evaluate(() => window.closeImageViewer());
    await waitForImage(page, 0);
    const revisited = await page.evaluate(() => {
      const fixture = window.imageViewportFixture;
      const first = document.querySelector('[data-image-viewport-index="0"]');
      return {url: first.dataset.viewImage, calls: fixture.calls.get(first.dataset.imagePath)};
    });
    assert.notEqual(revisited.url, initial.url, 'An evicted image must decode from a fresh URL when revisited');
    assert(revisited.calls > initial.calls, 'Revisiting an evicted image must fetch it again');
    await verifyVisiblePressure(page);
    await verifyStaleRelease(page);
  } finally {
    await cleanupGallery(page);
    if (originalViewport) await page.setViewport(originalViewport);
  }
}
