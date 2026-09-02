function thinkingKey(sessionId) { return `omp:thinking:${sessionId}`; }

function thinkingLevel(agent) {
  return localStorage.getItem(thinkingKey(agent.id)) || agent.session?.thinking || 'auto';
}


function eventAttachments(event) {
  const images = Array.isArray(event.attachments)
    ? event.attachments.map(attachment => attachment.url)
    : (event.images || []).map(image => image.url);
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
