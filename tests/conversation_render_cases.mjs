import assert from 'node:assert/strict';

export async function verifyConversationRendering(page) {
  const result = await page.evaluate(async () => {
    const savedState = Object.fromEntries(Object.keys(state).map(key => [key, state[key]]));
    const savedUrl = location.href;
    const savedHistory = history.state;
    const originalRequest = ompDeckApi.request;
    const ids = ['render-stability-source', 'render-stability-other'];
    const png = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM1sAAAAASUVORK5CYII='), value => value.charCodeAt(0));
    const waitFor = async predicate => {
      const deadline = performance.now() + 3000;
      while (!predicate()) {
        if (performance.now() >= deadline) throw new Error('Conversation rendering did not settle');
        await new Promise(resolve => setTimeout(resolve, 10));
      }
    };
    const message = (id, detail) => ({id, kind: 'assistant', title: 'OMP', detail, time: null});
    const makeAgent = (id, activity) => ({
      id, localId: id, machineId: 'local', machineName: 'Fixture', project: 'demo', label: id,
      lifecycle: 'ongoing', nodeAvailable: true, bridgeConnected: true, status: 'idle', executionState: 'idle',
      inboxState: 'read', capabilities: {sendMessage: true, attachments: true},
      attachmentLimits: {maxImages: 4, maxTotalBytes: 20 * 1024 * 1024},
      transport: {kind: 'extension', status: 'connected', label: 'Terminal'}, pendingUiRequest: null, subagents: [],
      session: {model: 'fixture/model', thinking: 'high', todos: [], activity, archiveActivity: [], updatedMs: Date.now()},
    });
    try {
      ompDeckApi.request = async path => {
        if (path.startsWith('/api/blob/')) return new Response(png, {headers: {'Content-Type': 'image/png'}});
        if (path.endsWith('/draft')) return new Response(JSON.stringify({
          enabled: false, revision: null, text: '', images: [], updatedAt: null, expiresAt: null,
        }));
        if (path.endsWith('/deliveries')) return new Response(JSON.stringify({version: 1, deliveries: []}));
        throw new Error(`Unexpected conversation fixture request: ${path}`);
      };
      const older = message('retained-message', '[Keep focus](https://example.com/)');
      older.attachments = [{url: `/api/blob/${'7'.repeat(64)}`, machineId: 'local'}];
      const latest = message('streaming-message', 'Before streaming');
      const source = makeAgent(ids[0], [older, latest]);
      const other = makeAgent(ids[1], [message('retained-message', 'Different session content')]);
      state.agents = [source, other];
      state.selectedId = null;
      state.showArchive = false;
      selectAgent(source.id, {pushHistory: false});
      await waitFor(() => el.activity.querySelector('img')?.naturalWidth === 1);
      const image = el.activity.querySelector('img');
      const link = el.activity.querySelector('[data-event-id="retained-message"] a');
      link.focus({preventScroll: true});
      for (let index = 0; index < 4; index += 1) {
        latest.detail = `Streaming revision ${index}`;
        source.session.updatedMs += 1;
        touchRender();
        renderDetail();
      }
      const retained = {
        focused: document.activeElement === link,
        decodedImage: image.isConnected && image.naturalWidth === 1,
        streamed: el.activity.querySelector('[data-event-id="streaming-message"]').textContent.includes('Streaming revision 3'),
      };
      source.executionState = 'working';
      source.activitySummary = 'First tool';
      touchRender();
      renderDetail();
      source.activitySummary = 'Next tool';
      touchRender();
      renderDetail();
      const statusUpdate = {
        focused: document.activeElement === link,
        working: Boolean(el.activity.querySelector('.typing-dots')),
      };
      source.executionState = 'idle';
      touchRender();
      renderDetail();
      statusUpdate.focusedAfterCompletion = document.activeElement === link;
      source.session.activity.push(message('new-message', 'Newest message'));
      touchRender();
      renderDetail();
      const newestFirst = [...el.activity.querySelectorAll('.event')].map(node => node.dataset.eventId);
      source.session.activity = source.session.activity.filter(event => event.id !== 'streaming-message');
      older.detail = 'Edited older content';
      touchRender();
      renderDetail();
      const edited = {
        removed: !el.activity.querySelector('[data-event-id="streaming-message"]'),
        changed: el.activity.querySelector('[data-event-id="retained-message"]').textContent.includes('Edited older content'),
      };
      state.showTodo = true;
      source.session.todos = [{text: 'Initial task', status: 'pending'}];
      touchRender();
      renderDetail();
      source.session.todos[0].text = 'Updated task';
      source.session.todos[0].status = 'completed';
      touchRender();
      renderDetail();
      const todo = {
        updated: el.activity.querySelector('.todo-panel').textContent.includes('Updated task'),
        progress: el.activity.querySelector('progress').value,
      };
      selectAgent(other.id, {pushHistory: false});
      const switched = {
        otherContent: el.activity.textContent.includes('Different session content'),
        oldContent: el.activity.textContent.includes('Edited older content'),
        images: el.activity.querySelectorAll('img').length,
      };
      return {retained, statusUpdate, newestFirst, edited, todo, switched};
    } finally {
      ompDeckApi.request = originalRequest;
      for (const id of ids) {
        composer.remove(id);
        ompDeckImages.releaseOwner(id);
      }
      Object.assign(state, savedState);
      history.replaceState(savedHistory, '', savedUrl);
      touchRender();
      renderSummary();
      renderList();
      renderDetail({selectionChanged: true});
    }
  });
  assert.deepEqual(result.retained, {focused: true, decodedImage: true, streamed: true},
    'Streaming updates must preserve focus and decoded images in unchanged messages');
  assert.deepEqual(result.statusUpdate, {focused: true, working: true, focusedAfterCompletion: true},
    'Tool-status changes and completion must not move unchanged conversation controls out of focus');
  assert.deepEqual(result.newestFirst, ['new-message', 'streaming-message', 'retained-message']);
  assert.deepEqual(result.edited, {removed: true, changed: true},
    'Reconciliation must apply removals and edits, including edits under an existing message identity');
  assert.deepEqual(result.todo, {updated: true, progress: 1},
    'Task edits must not be hidden by a reused list identity');
  assert.deepEqual(result.switched, {otherContent: true, oldContent: false, images: 0},
    'Switching sessions must not reuse private content merely because message identities match');
}
