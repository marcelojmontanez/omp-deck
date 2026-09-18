function relativeTime(value) {
  if (!value) return 'now';
  const seconds = Math.max(0, Math.round((Date.now() - value) / 1000));
  if (seconds < 8) return 'now';
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h`;
}

function clockTime(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}

function thinkingLevel(agent) {
  const override = thinkingOverrides.get(agent.id);
  if (typeof override === 'string' && override) return override;
  const observed = agent.session?.thinking;
  return typeof observed === 'string' && observed ? observed : null;
}
function thinkingChoices(agent) {
  return Array.isArray(agent?.session?.thinkingLevels) ? agent.session.thinkingLevels : [];
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

function deliveryEvent(item) {
  if (item.state === 'accepted') return null;
  const labels = {
    submitting: 'Submitting', uncertain: 'Delivery uncertain',
    failed: 'Not sent', cancelled: 'Cancelled', stale: 'Stale',
  };
  return {
    kind: 'user pending', title: `You · ${labels[item.state] || 'Delivery uncertain'}`,
    detail: item.text, time: new Date(item.createdAt).toISOString(),
    attachments: item.attachments, deliveryId: item.id, deliveryState: item.state,
  };
}

function deliveryControls(event) {
  if (!event.deliveryId || ['submitting', 'accepted'].includes(event.deliveryState)) return '';
  const id = escapeHtml(event.deliveryId);
  return `<div class="delivery-controls"><span>Receipt only; check the conversation before sending again.</span><button type="button" data-delivery-action="restore" data-delivery-id="${id}">Restore draft</button><button type="button" data-delivery-action="dismiss" data-delivery-id="${id}">Dismiss receipt</button></div>`;
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
function returnToSessions() {
  const previousId = state.selectedId;
  closeSessionMenu();
  const url = new URL(location.href);
  url.searchParams.delete('session');
  history.replaceState({view: 'menu'}, '', url);
  showMenu();
  const card = el['agent-list'].querySelector(`[data-id="${CSS.escape(previousId || '')}"]`);
  const fallback = el['history-toggle'].hidden
    ? el['agent-list'].querySelector('[data-id]') || document.getElementById('dashboard-title')
    : el['history-toggle'];
  (card || fallback).focus();
}

function formatImageLimit(bytes) {
  return bytes >= 1024 * 1024 ? `${bytes / (1024 * 1024)} MiB` : `${Math.ceil(bytes / 1024)} KiB`;
}
