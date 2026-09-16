import assert from 'node:assert/strict';
import {writeFile, rm} from 'node:fs/promises';
import path from 'node:path';

const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM1sAAAAASUVORK5CYII=';
const imageButton = '#activity [data-image-path]';

async function holdImage(page, stage, shortenDeadline = true) {
  await page.evaluate(({stage, shortenDeadline}) => {
    const originalFetch = window.fetch;
    const originalTimeout = window.setTimeout;
    const target = document.querySelector('#activity [data-image-path]').dataset.imagePath;
    const held = {originalFetch, originalTimeout, release: () => {}, signal: null};
    window.imageRecoveryFixture = held;
    window.setTimeout = (callback, delay, ...args) => originalTimeout(
      callback, shortenDeadline && delay === ompDeckNetwork.STARTUP_TIMEOUT_MS ? 120 : delay, ...args,
    );
    window.fetch = (input, options = {}) => {
      const pathname = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
      if (pathname !== target) return originalFetch(input, options);
      held.signal = options.signal;
      const reason = () => options.signal?.reason || new DOMException('Fixture released', 'AbortError');
      if (stage === 'headers') return new Promise((_, reject) => {
        held.release = () => reject(reason());
        options.signal?.addEventListener('abort', held.release, {once: true});
      });
      return Promise.resolve(new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(new Uint8Array([137, 80, 78, 71]));
          held.release = () => controller.error(reason());
          options.signal?.addEventListener('abort', held.release, {once: true});
        },
      }), {headers: {'Content-Type': 'image/png'}}));
    };
    ompDeckImages.clear();
    touchRender();
    renderDetail();
  }, {stage, shortenDeadline});
}

async function restoreImageFetch(page) {
  await page.evaluate(() => {
    const held = window.imageRecoveryFixture;
    if (!held) return;
    held.release();
    window.fetch = held.originalFetch;
    window.setTimeout = held.originalTimeout;
    delete window.imageRecoveryFixture;
    ompDeckImages.clear();
    touchRender();
    renderDetail();
  });
}

async function decodedImage(page, selector = imageButton) {
  await page.waitForFunction(selector => {
    const image = document.querySelector(`${selector} img`);
    return image?.complete && image.naturalWidth === 1;
  }, {timeout: 2000}, selector);
}

export async function verifyMobileImages(page, root) {
  await verifyOfflineGallery(page, root);
  for (const stage of ['headers', 'body']) {
    await decodedImage(page);
    await holdImage(page, stage);
    try {
      await page.waitForSelector(`${imageButton}.image-failed`, {timeout: 1500});
      assert.equal(await page.evaluate(() => imageRecoveryFixture.signal?.aborted), true,
        `Stalled image ${stage} must be cancelled rather than remain loading`);
      await page.evaluate(() => { window.fetch = imageRecoveryFixture.originalFetch; });
      await page.tap(`${imageButton}.image-failed`);
      await decodedImage(page);
      await page.tap(imageButton);
      await page.waitForFunction(() => document.getElementById('image-viewer').open
        && document.getElementById('image-viewer-image').naturalWidth === 1);
      await page.tap('.image-viewer-close');
    } finally {
      await restoreImageFetch(page);
    }
  }

  await decodedImage(page);
  await holdImage(page, 'body', false);
  try {
    await page.evaluate(() => {
      window.setTimeout = imageRecoveryFixture.originalTimeout;
      window.fetch = imageRecoveryFixture.originalFetch;
      recoverLiveConnections();
    });
    await decodedImage(page);
    assert.equal(await page.evaluate(() => imageRecoveryFixture.signal.aborted), true,
      'Foreground recovery must replace the pending image body');
  } finally {
    await restoreImageFetch(page);
  }
  await verifySupersededImage(page);
}

async function verifySupersededImage(page) {
  await decodedImage(page);
  await page.evaluate(() => {
    const originalFetch = window.fetch;
    const target = document.querySelector('#activity [data-image-path]').dataset.imagePath;
    const held = {originalFetch, originalTimeout: window.setTimeout, release: () => {}};
    window.imageRecoveryFixture = held;
    window.fetch = (input, options = {}) => {
      const pathname = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
      if (pathname !== target) return originalFetch(input, options);
      // Deliver a response already in flight even after its request is superseded.
      return new Promise(resolve => { held.release = resolve; });
    };
    ompDeckImages.clear();
    touchRender();
    renderDetail();
  });
  try {
    await page.evaluate(() => {
      window.fetch = imageRecoveryFixture.originalFetch;
      recoverLiveConnections();
    });
    await decodedImage(page);
    await page.tap(imageButton);
    const currentUrl = await page.$eval('#image-viewer-image', image => image.src);
    await page.evaluate(async png => {
      const bytes = Uint8Array.from(atob(png), character => character.charCodeAt(0));
      imageRecoveryFixture.release(new Response(bytes, {headers: {'Content-Type': 'image/png'}}));
      await new Promise(resolve => setTimeout(resolve, 30));
    }, PNG);
    assert.deepEqual(await page.$eval('#image-viewer-image', image => ({
      source: image.src, open: image.closest('dialog').open, width: image.naturalWidth,
    })), {source: currentUrl, open: true, width: 1},
    'A superseded response must not replace or close the current image');
    await page.tap('.image-viewer-close');
  } finally {
    await restoreImageFetch(page);
  }
}

async function verifyOfflineGallery(page, root) {
  const galleryFile = path.join(root, 'gallery.png');
  await writeFile(galleryFile, Buffer.from(PNG, 'base64'));
  try {
    await decodedImage(page);
    await page.setOfflineMode(true);
    await page.evaluate(() => refresh(null, {replace: true}));
    await page.tap(imageButton);
    assert.equal(await page.$eval('#image-viewer', dialog => dialog.open), true,
      'A downloaded image must still open while offline');
    await page.waitForFunction(() => document.getElementById('image-viewer-image').naturalWidth === 1);
    await page.tap('.image-viewer-close');
    const [chooser] = await Promise.all([page.waitForFileChooser({timeout: 2000}), page.tap('#attach')]);
    await chooser.accept([galleryFile]);
    await decodedImage(page, '#attachments');
    assert.equal(await page.$eval('#send', button => button.disabled), true,
      'Opening the local gallery must not enable offline delivery');
    await page.tap('[data-remove-attachment]');
  } finally {
    await page.setOfflineMode(false);
    await page.evaluate(() => recoverLiveConnections());
    await page.waitForFunction(() => ompDeckApi.machines().every(machine => machine.status === 'live'));
    await rm(galleryFile, {force: true});
  }
}
