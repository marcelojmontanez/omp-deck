'use strict';

function createComposerDraftProtocol(snapshot) {
  const REQUEST_TIMEOUT_MS = 120_000;

  function validate(value, {imagesRequired = true, fallbackImages = []} = {}) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new Error('Invalid saved draft response');
    }
    if (
      typeof value.enabled !== 'boolean'
      || !(value.revision === null || (typeof value.revision === 'string' && value.revision))
      || typeof value.text !== 'string'
      || (imagesRequired && !Array.isArray(value.images))
      || (!imagesRequired && value.images !== undefined && !Array.isArray(value.images))
      || !(value.updatedAt === null || typeof value.updatedAt === 'string')
      || !(value.expiresAt === null || typeof value.expiresAt === 'string')
    ) throw new Error('Invalid saved draft response');
    const sourceImages = value.images === undefined ? fallbackImages : value.images;
    const images = sourceImages.map(image => {
      if (
        !image || typeof image !== 'object' || Array.isArray(image)
        || typeof image.data !== 'string' || !image.data
        || typeof image.mimeType !== 'string' || !image.mimeType.startsWith('image/')
        || !(/^[A-Za-z0-9+/]+={0,2}$/.test(image.data) && image.data.length % 4 === 0)
        || !([undefined, 'string'].includes(typeof image.name))
      ) throw new Error('Invalid saved draft image');
      return {data: image.data, mimeType: image.mimeType, ...(image.name ? {name: image.name} : {})};
    });
    if (!value.enabled && (value.text || images.length)) {
      throw new Error('Invalid disabled saved draft');
    }
    return {...value, images};
  }

  function wireImage(attachment) {
    const match = /^data:([^;,]+);base64,([A-Za-z0-9+/]+={0,2})$/.exec(attachment?.data || '');
    if (!match || !match[1].startsWith('image/') || match[1] !== attachment.type) {
      throw new Error('An image is not ready to save');
    }
    return {
      data: match[2],
      mimeType: match[1],
      ...(attachment.name ? {name: attachment.name} : {}),
    };
  }

  function wireSnapshot(id, enabled = true, includeImages = true) {
    const current = snapshot(id);
    const result = {enabled, text: enabled ? current.text : ''};
    if (includeImages) result.images = enabled ? current.attachments.map(wireImage) : [];
    return result;
  }

  function sameImages(left, right) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
    return left.every((image, index) => {
      const other = right[index];
      return image.data === other?.data && image.mimeType === other?.mimeType
        && (image.name || '') === (other.name || '');
    });
  }

  function sameWire(left, right) {
    if (!left || !right || left.enabled !== right.enabled || left.text !== right.text) return false;
    if (!Object.hasOwn(left, 'images')) return true;
    return sameImages(left.images, right.images);
  }

  function localMatches(id, remote) {
    try {
      const current = wireSnapshot(id);
      current.enabled = remote.enabled;
      return sameWire(current, remote);
    } catch {
      return false;
    }
  }

  function imagesMatch(id, remote) {
    try {
      return sameImages(wireSnapshot(id).images, remote.images);
    } catch {
      return false;
    }
  }

  function attemptedMatches(id, attempted, remote) {
    return sameWire(attempted, remote)
      && (Object.hasOwn(attempted, 'images') || imagesMatch(id, remote));
  }

  async function request(id, options = {}, validation = {}) {
    const {response, payload} = await ompDeckNetwork.fetchJsonBounded(
      `/api/sessions/${encodeURIComponent(id)}/draft`,
      {cache: 'no-store', ...options},
      REQUEST_TIMEOUT_MS,
      ompDeckApi.request,
    );
    if (!response.ok) {
      const error = new Error(payload?.error || `Saved draft request failed (${response.status})`);
      error.response = response;
      error.payload = payload;
      throw error;
    }
    return validate(payload, validation);
  }

  return Object.freeze({attemptedMatches, localMatches, request, sameWire, validate, wireSnapshot});
}
