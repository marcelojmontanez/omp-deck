function thinkingLevel(agent) {
  const override = thinkingOverrides.get(agent.id);
  if (typeof override === 'string' && override) return override;
  const observed = agent.session?.thinking;
  return typeof observed === 'string' && observed ? observed : null;
}
function thinkingChoices(agent) {
  return Array.isArray(agent?.session?.thinkingLevels) ? agent.session.thinkingLevels : [];
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
  return typeof value === 'string' && value.startsWith('blob:') ? value : null;
}

function safeImagePath(value) {
  if (typeof value !== 'string') return null;
  let parsed;
  try {
    parsed = new URL(value, location.href);
  } catch {
    return null;
  }
  if (
    parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
    || !/^\/api\/blob\/[a-f0-9]{64}$/.test(parsed.pathname)
  ) return null;
  return parsed.pathname;
}

function eventAttachments(event) {
  const attachments = Array.isArray(event.attachments)
    ? event.attachments
    : Array.isArray(event.images) ? event.images : [];
  const images = attachments.map(attachment => {
    const value = typeof attachment === 'string' ? attachment : attachment?.url;
    const localUrl = safeAttachmentUrl(value);
    if (localUrl) {
      return `<button class="message-image" type="button" data-view-image="${escapeHtml(localUrl)}" aria-label="Enlarge attached image"><img src="${escapeHtml(localUrl)}" alt="Attached image" loading="lazy"></button>`;
    }
    const path = safeImagePath(value);
    const machineId = typeof attachment === 'object' ? attachment?.machineId : null;
    if (path && typeof machineId === 'string' && machineId) {
      return `<button class="message-image image-loading" type="button" data-image-path="${escapeHtml(path)}" data-image-machine="${escapeHtml(machineId)}" aria-label="Enlarge attached image"><span>Loading image…</span></button>`;
    }
    return '<div class="message-image image-unavailable" role="note">Image unavailable</div>';
  });
  if (images.length) return `<div class="message-attachments">${images.join('')}</div>`;
  if (event.attachments && !Array.isArray(event.attachments)) {
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
