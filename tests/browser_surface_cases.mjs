import assert from 'node:assert/strict';
import {verifyViewportImages} from './image_viewport_cases.mjs';

const viewports = [
  {width: 390, height: 844},
  {width: 1024, height: 768},
];

async function verifyStructuredRendering(page) {
  const originalViewport = page.viewport();
  try {
    for (const viewport of viewports) {
      await page.setViewport(viewport);
      const result = await page.evaluate(() => {
        const markdown = document.createElement('div');
        markdown.innerHTML = renderMarkdown('[`fn()`](https://example.test)');
        const hostile = document.createElement('div');
        hostile.innerHTML = renderMarkdown('[`<img src=x onerror=alert(1)>`](https://example.test)');
        const todo = document.createElement('div');
        todo.innerHTML = TodoUI.render([
          {phase: 'Plan: API', text: 'Keep: this\nlegitimate text', status: 'in_progress'},
          {phase: 'Plan: API', text: '<not markup>', status: 'completed'},
        ], escapeHtml);
        return {
          markdownText: markdown.querySelector('a')?.textContent,
          markdownCode: markdown.querySelector('a code')?.textContent,
          hostileImages: hostile.querySelectorAll('img').length,
          hostileCode: hostile.querySelector('code')?.textContent,
          phase: todo.querySelector('.todo-phase h3')?.textContent,
          tasks: [...todo.querySelectorAll('.todo-phase li > span:nth-child(2)')]
            .map(node => node.textContent),
          count: TodoUI.count([
            {phase: 'Plan: API', text: 'one'},
            {phase: 'Plan: API', text: 'two'},
          ]),
        };
      });
      assert.deepEqual(result, {
        markdownText: 'fn()',
        markdownCode: 'fn()',
        hostileImages: 0,
        hostileCode: '<img src=x onerror=alert(1)>',
        phase: 'Plan: API',
        tasks: ['Keep: this\nlegitimate text', '<not markup>'],
        count: 2,
      }, `structured rendering at ${viewport.width}px`);
    }
  } finally {
    if (originalViewport) await page.setViewport(originalViewport);
  }
}


async function verifySelectionAndAccessibleName(page) {
  const result = await page.evaluate(async () => {
    const previousRequest = ompDeckApi.request;
    const saved = {
      agents: state.agents,
      selectedId: state.selectedId,
      pendingSelection: state.pendingSelection,
      showArchive: state.showArchive,
      showTodo: state.showTodo,
      showHistory: state.showHistory,
      sessionScope: state.sessionScope,
      filter: state.filter,
    };
    const machine = ompDeckApi.machine(state.selectedMachineId);
    if (!machine) throw new Error('Surface case requires a selected machine');
    const node = nodeState(machine.id);
    const savedNode = {
      sessions: node.sessions,
      snapshotGeneration: node.snapshotGeneration,
      hasSnapshot: node.hasSnapshot,
    };
    const raw = {
      id: 'surface-selection',
      lifecycle: 'ongoing',
      title: 'Selection fixture',
      project: {name: 'surface', path: '/surface'},
      state: 'idle',
      transport: {kind: 'rpc', status: 'connected', label: 'Managed RPC'},
      capabilities: {},
      messages: [{
        id: 'first', role: 'assistant', createdAt: '2026-09-18T12:00:00Z',
        content: [{type: 'text', text: 'Selected reply survives'}],
      }],
      deliveryError: {
        nativeRequestId: 'native-1',
        errorCode: '<img src=x onerror=alert(1)>',
        correlationAvailable: false,
        observedAt: '2026-09-18T12:00:01Z',
      },
      todos: [],
      subagents: [],
      unread: false,
      updatedAt: '2026-09-18T12:00:00Z',
    };
    try {
      ompDeckApi.request = async () => new Response('{}', {
        status: 200,
        headers: {'Content-Type': 'application/json'},
      });
      const initial = projectSession(raw, machine);
      node.sessions = new Map([[raw.id, raw]]);
      state.agents = [initial];
      state.selectedId = initial.id;
      state.pendingSelection = null;
      state.showArchive = false;
      state.showTodo = false;
      state.showHistory = false;
      state.sessionScope = 'ongoing';
      state.filter = 'all';
      touchRender();
      renderSummary();
      renderList();
      renderDetail({selectionChanged: true});
      const readLabel = document.querySelector(`[data-id="${CSS.escape(initial.id)}"]`).ariaLabel;
      const deliveryNotice = document.querySelector('.delivery-warning')?.textContent;
      const deliveryMarkup = document.querySelector('.delivery-warning img');
      initial.inboxState = 'unread';
      renderList();
      const unreadLabel = document.querySelector(`[data-id="${CSS.escape(initial.id)}"]`).ariaLabel;
      initial.inboxState = 'read';
      renderList();

      const paragraph = el.activity.querySelector('.event-detail p');
      const range = document.createRange();
      range.selectNodeContents(paragraph);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      const selectedText = selection.toString();
      const applied = applyFleetPatch(raw.id, {
        unread: true,
        updatedAt: '2026-09-18T12:01:00Z',
        messages: [...raw.messages, {
          id: 'second', role: 'assistant', createdAt: '2026-09-18T12:01:00Z',
          content: [{type: 'text', text: 'Authoritative update'}],
        }],
      }, node.snapshotGeneration + 1, machine.id);
      const preserved = selection.toString() === selectedText && paragraph.isConnected;

      selection.removeAllRanges();
      document.dispatchEvent(new Event('selectionchange'));
      await new Promise(resolve => requestAnimationFrame(resolve));
      return {
        preserved,
        applied,
        flushed: !paragraph.isConnected && el.activity.textContent.includes('Authoritative update'),
        readLabel,
        unreadLabel,
        deliveryNotice,
        deliveryMarkup: Boolean(deliveryMarkup),
      };
    } finally {
      ompDeckApi.request = previousRequest;
      node.sessions = savedNode.sessions;
      node.snapshotGeneration = savedNode.snapshotGeneration;
      node.hasSnapshot = savedNode.hasSnapshot;
      Object.assign(state, saved);
      touchRender();
      renderSummary();
      renderList();
      renderDetail({selectionChanged: true});
    }
  });
  assert.equal(result.preserved, true);
  assert.equal(result.applied, true);
  assert.equal(result.flushed, true);
  assert.equal(result.readLabel.includes('Unread response'), false);
  assert.equal(result.unreadLabel.includes('Unread response'), true);
  assert.match(result.deliveryNotice, /Exact delivery cannot be identified by the native API/);
  assert.equal(result.deliveryMarkup, false);
}

async function verifyUsageLifecycle(page) {
  const result = await page.evaluate(async () => {
    const previousRequest = ompDeckApi.request;
    const previousNow = Date.now;
    let usedFraction = 0.12;
    let calls = 0;
    try {
      ompDeckUsage.stop();
      ompDeckApi.request = async (path, options) => {
        if (path !== '/api/usage') return previousRequest(path, options);
        calls += 1;
        return new Response(JSON.stringify({reports: [{
          provider: 'test',
          limits: [{label: 'Surface', amount: {usedFraction}, window: {resetsAt: Date.now() + 3_600_000}}],
        }]}), {headers: {'Content-Type': 'application/json'}});
      };
      await ompDeckUsage.start();
      const before = document.getElementById('usage-list').textContent;
      usedFraction = 0.73;
      Date.now = () => previousNow() + 61_000;
      await ompDeckUsage.foreground();
      const after = document.getElementById('usage-list').textContent;
      return {before, after, calls};
    } finally {
      Date.now = previousNow;
      ompDeckUsage.stop();
      ompDeckApi.request = previousRequest;
      void ompDeckUsage.start();
    }
  });
  assert.match(result.before, /12%/);
  assert.match(result.after, /73%/);
  assert.equal(result.calls, 2);
}

async function verifyWideTables(page) {
  const originalViewport = page.viewport();
  try {
    for (const viewport of viewports) {
      await page.setViewport(viewport);
      await page.evaluate(() => {
        const parents = [el.app, document.body, el.workspace];
        const classes = parents.map(parent => parent.className);
        parents.forEach(parent => parent.classList.add('chat-open'));
        const article = document.createElement('article');
        article.className = 'event assistant';
        article.dataset.wideTableFixture = '';
        const detail = document.createElement('div');
        detail.className = 'event-detail';
        const columns = Array.from({length: 30}, (_, index) => index + 1);
        detail.innerHTML = renderMarkdown(
          `| ${columns.map(number => `Column ${number}`).join(' | ')} |\n`
          + `| ${columns.map(() => '---').join(' | ')} |\n`
          + `| ${columns.map(number => `Value ${number}`).join(' | ')} |`
        );
        article.append(detail);
        const scrollTop = el.activity.scrollTop;
        el.activity.append(article);
        article.scrollIntoView();
        window.wideTableFixture = {article, parents, classes, scrollTop};
      });
      try {
        const selector = '[data-wide-table-fixture] [aria-label="Scrollable table"]';
        await page.focus(selector);
        await page.evaluate(selector => {
          window.wideTableFixture.keyboardSettled = false;
          document.querySelector(selector).addEventListener('scrollend', () => {
            window.wideTableFixture.keyboardSettled = true;
          }, {once: true});
        }, selector);
        await page.keyboard.press('ArrowRight');
        await page.waitForFunction(selector =>
          document.querySelector(selector)?.scrollLeft > 0, {}, selector);
        await page.waitForFunction(() => window.wideTableFixture.keyboardSettled, {timeout: 3000});
        await page.hover(selector);
        await page.mouse.wheel({deltaX: 10000, deltaY: 0});
        await page.waitForFunction(selector => {
          const scroller = document.querySelector(selector);
          const last = scroller?.querySelector('td:last-child');
          return last && last.getBoundingClientRect().right <= scroller.getBoundingClientRect().right + 1;
        }, {timeout: 3000}, selector).catch(async error => {
          const details = await page.evaluate(selector => {
            const element = document.querySelector(selector);
            if (!element) return {removed: true};
            const box = element.getBoundingClientRect();
            return {
              scrollLeft: element.scrollLeft,
              scrollWidth: element.scrollWidth,
              clientWidth: element.clientWidth,
              region: box.toJSON(),
              lastCell: element.querySelector('td:last-child').getBoundingClientRect().toJSON(),
              pointerTarget: document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)?.outerHTML.slice(0, 200),
            };
          }, selector);
          throw new Error(`Wide table wheel scroll at ${viewport.width}px: ${JSON.stringify(details)}`, {cause: error});
        });
        const visible = await page.evaluate(selector => {
          const scroller = document.querySelector(selector);
          const last = scroller.querySelector('td:last-child');
          return {
            lastCell: last.textContent,
            lastCellVisible: last.getBoundingClientRect().left >= scroller.getBoundingClientRect().left,
            pageContained: document.documentElement.scrollWidth <= window.innerWidth,
          };
        }, selector);
        assert.deepEqual(visible, {lastCell: 'Value 30', lastCellVisible: true, pageContained: true});
      } finally {
        await page.evaluate(() => {
          const fixture = window.wideTableFixture;
          fixture.article.remove();
          fixture.parents.forEach((parent, index) => { parent.className = fixture.classes[index]; });
          el.activity.scrollTop = fixture.scrollTop;
          delete window.wideTableFixture;
        });
      }
    }
  } finally {
    if (originalViewport) await page.setViewport(originalViewport);
  }
}

export async function verifyBrowserSurface(page) {
  await verifyStructuredRendering(page);
  await verifyWideTables(page);
  await verifyViewportImages(page);
  await verifySelectionAndAccessibleName(page);
  await verifyUsageLifecycle(page);
}
