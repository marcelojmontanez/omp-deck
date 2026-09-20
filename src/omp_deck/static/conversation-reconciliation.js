'use strict';

window.ompDeckConversation = (() => {
  // Signatures follow live DOM nodes, so switching sessions drops both markup and cached content.
  const rendered = new WeakMap();

  function attachmentSnapshot(event) {
    const attachments = Array.isArray(event.attachments)
      ? event.attachments
      : Array.isArray(event.images) ? event.images : [];
    return {
      count: event.attachments && !Array.isArray(event.attachments) ? event.attachments : null,
      items: attachments.map(attachment => typeof attachment === 'string'
        ? [attachment, null]
        : [attachment?.url, attachment?.machineId]),
    };
  }

  function sameAttachments(left, right) {
    if (!left || !right || left.count !== right.count || left.items.length !== right.items.length) {
      return false;
    }
    return left.items.every((part, index) => (
      part[0] === right.items[index][0] && part[1] === right.items[index][1]
    ));
  }

  function eventSnapshot(event) {
    return {
      id: event.id,
      kind: event.kind,
      title: event.title,
      detail: event.detail,
      time: event.time,
      deliveryId: event.deliveryId,
      deliveryState: event.deliveryState,
      attachments: attachmentSnapshot(event),
    };
  }

  function sameEvent(left, right) {
    return Boolean(left && right
      && left.id === right.id
      && left.kind === right.kind
      && left.title === right.title
      && left.detail === right.detail
      && left.time === right.time
      && left.deliveryId === right.deliveryId
      && left.deliveryState === right.deliveryState
      && sameAttachments(left.attachments, right.attachments));
  }

  function eventHtml(event, archived) {
    return `<article class="event ${archived ? 'archived ' : ''}${escapeHtml(event.kind)}" ${event.id ? `data-event-id="${escapeHtml(event.id)}"` : ''}>
      <div class="event-head">
        <span class="event-title">${escapeHtml(event.title)}</span>
        <time class="event-time">${escapeHtml(clockTime(event.time))}</time>
      </div>
      ${event.kind === 'thinking'
        ? '<div class="typing-dots" aria-label="OMP is working"><i></i><i></i><i></i></div>'
        : `${eventAttachments(event)}${renderEventDetail(event)}${deliveryControls(event)}`}
    </article>`;
  }

  function descriptor(key, value, render, same = Object.is, update = null) {
    return {key, value, render, same, update};
  }

  function archiveToggleDescriptor(key, expanded) {
    return descriptor(
      key,
      expanded,
      () => `<div class="archive-separator"><button type="button" data-toggle-archive aria-expanded="${expanded}">${expanded ? 'Hide earlier history' : 'Show earlier history'}</button></div>`,
      Object.is,
      node => {
        const button = node.querySelector('[data-toggle-archive]');
        button.setAttribute('aria-expanded', String(expanded));
        button.textContent = expanded ? 'Hide earlier history' : 'Show earlier history';
      },
    );
  }

  function eventDescriptors(events, prefix, {archived = false} = {}) {
    const occurrences = new Map();
    return events.map(event => {
      const identity = event.id
        ? `event:${event.id}`
        : event.deliveryId
          ? `delivery:${event.deliveryId}`
          : `anonymous:${event.kind || ''}:${event.time || ''}:${event.title || ''}`;
      const occurrence = occurrences.get(identity) || 0;
      occurrences.set(identity, occurrence + 1);
      return descriptor(
        `${prefix}:${identity}:${occurrence}`,
        eventSnapshot(event),
        () => eventHtml(event, archived),
        sameEvent,
      );
    });
  }

  function elementFromHtml(html) {
    const template = document.createElement('template');
    template.innerHTML = html.trim();
    return template.content.firstElementChild;
  }

  function reconcile(container, descriptors) {
    const existing = new Map();
    const wanted = new Set(descriptors.map(item => item.key));
    for (const child of [...container.children]) {
      const record = rendered.get(child);
      if (!record || !wanted.has(record.key) || existing.has(record.key)) {
        child.remove();
      } else {
        existing.set(record.key, child);
      }
    }

    descriptors.forEach((item, index) => {
      let node = existing.get(item.key);
      const previous = node ? rendered.get(node) : null;
      if (!node || !item.same(previous.value, item.value)) {
        if (node && item.update) {
          item.update(node, item.value);
        } else {
          const replacement = elementFromHtml(item.render());
          if (node) node.replaceWith(replacement);
          node = replacement;
        }
        rendered.set(node, {key: item.key, value: item.value});
      }
      const position = container.children[index];
      if (position !== node) container.insertBefore(node, position || null);
    });

    while (container.children.length > descriptors.length) {
      container.lastElementChild.remove();
    }
  }

  return Object.freeze({archiveToggleDescriptor, descriptor, eventDescriptors, reconcile});
})();
