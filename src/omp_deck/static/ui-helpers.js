function thinkingLevel(agent) {
  return thinkingOverrides.get(agent.id) || agent.session?.thinking || 'auto';
}
function composerDeliveryRoute(agent) {
  const status = agent?.executionState || agent?.status || 'idle';
  return status === 'working' && agent.capabilities?.followUp ? 'follow-ups' : 'messages';
}


async function drainComposerQueue(queue, deliver, restore) {
  while (queue.length) {
    try {
      await deliver(queue[0]);
      queue.shift();
    } catch (error) {
      restore(queue.splice(0), error);
      return;
    }
  }
}




function safeAttachmentUrl(value) {
  if (typeof value !== 'string') return null;
  if (value.startsWith('blob:')) return value;
  let parsed;
  try {
    parsed = new URL(value, location.href);
  } catch {
    return null;
  }
  if (
    parsed.origin !== location.origin
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
    || !/^\/api\/blob\/[a-f0-9]{64}$/.test(parsed.pathname)
  ) return null;
  return parsed.pathname;
}

function eventAttachments(event) {
  const images = (Array.isArray(event.attachments)
    ? event.attachments.map(attachment => attachment.url)
    : (event.images || []).map(image => image.url))
    .map(safeAttachmentUrl)
    .filter(Boolean);
  if (images.length) {
    return `<div class="message-attachments">${images.map(url =>
      `<a href="${escapeHtml(url)}" data-view-image="${escapeHtml(url)}" aria-label="Enlarge attached image"><img src="${escapeHtml(url)}" alt="Attached image" loading="lazy"></a>`
    ).join('')}</div>`;
  }
  if (event.attachments) {
    return `<div class="attachment-count">▧ ${event.attachments} file${event.attachments === 1 ? '' : 's'}</div>`;
  }
  return '';
}

function syncVisualViewport() {
  const viewport = window.visualViewport;
  if (!viewport) return;
  const keyboardOpen = viewport.height < window.innerHeight - 80;
  document.documentElement.style.setProperty('--app-height', `${viewport.height}px`);
  document.documentElement.style.setProperty('--viewport-top', `${viewport.offsetTop}px`);
  document.body.classList.toggle('keyboard-open', keyboardOpen);
}

let viewportSyncTimers = [];
function scheduleVisualViewportSync() {
  viewportSyncTimers.forEach(clearTimeout);
  viewportSyncTimers = [];
  syncVisualViewport();
  requestAnimationFrame(syncVisualViewport);
  for (const delay of [50, 150, 350, 700]) {
    viewportSyncTimers.push(setTimeout(syncVisualViewport, delay));
  }
}

if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', scheduleVisualViewportSync);
  window.visualViewport.addEventListener('scroll', scheduleVisualViewportSync);
  window.addEventListener('focus', scheduleVisualViewportSync);
  window.addEventListener('pageshow', scheduleVisualViewportSync);
  document.addEventListener('focusin', scheduleVisualViewportSync);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') scheduleVisualViewportSync();
  });
  scheduleVisualViewportSync();
}
