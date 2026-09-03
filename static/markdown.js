const escapeHtml = value => {
  const node = document.createElement('div');
  node.textContent = value ?? '';
  return node.innerHTML;
};

function renderInlineMarkdown(value) {
  const code = [];
  const links = [];
  let html = escapeHtml(value).replace(/`([^`\n]+)`/g, (_, content) => {
    const token = `\u0000CODE${code.length}\u0000`;
    code.push(`<code>${content}</code>`);
    return token;
  });
  html = html
    .replace(/\[([^\]\n]*)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) => {
      const token = `\u0000LINK${links.length}\u0000`;
      links.push(`<a href="${url}" target="_blank" rel="noopener noreferrer">${label || url}</a>`);
      return token;
    })
    .replace(/(^|[\s(])(https?:\/\/[^\s<]+)/g, (_, prefix, rawUrl) => {
      const match = rawUrl.match(/[),.!?;:\]]+$/);
      const trailing = match?.[0] || '';
      const url = trailing ? rawUrl.slice(0, -trailing.length) : rawUrl;
      const token = `\u0000LINK${links.length}\u0000`;
      links.push(`<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
      return `${prefix}${token}${trailing}`;
    })
    .replace(/\[\]\(\)/g, '')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
    .replace(/(^|[^\w])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/(^|[^\w])_([^_\n]+)_/g, '$1<em>$2</em>')
    .replace(/\u0000CODE(\d+)\u0000/g, (_, index) => code[Number(index)]);
  return html.replace(/\u0000LINK(\d+)\u0000/g, (_, index) => links[Number(index)]);
}

function parseTableRow(line, requireOuterPipes = false) {
  const value = line.trim();
  const hasOuterPipes = value.startsWith('|') && value.endsWith('|');
  if (requireOuterPipes && !hasOuterPipes) return null;
  const content = value
    .replace(/^\|/, '')
    .replace(/\|$/, '');
  const cells = content.split('|').map(cell => cell.trim());
  return cells.length > 1 ? cells : null;
}

function parseMarkdownTable(lines, start) {
  const header = parseTableRow(lines[start]);
  const following = start + 1 < lines.length ? parseTableRow(lines[start + 1]) : null;
  if (!header || !following || following.length !== header.length) return null;

  const hasDelimiter = following.every(cell => /^:?-{3,}:?$/.test(cell));
  const implicitTable = !hasDelimiter
    && parseTableRow(lines[start], true)
    && parseTableRow(lines[start + 1], true)
    && header.every(Boolean)
    && following.every(Boolean);
  if (!hasDelimiter && !implicitTable) return null;

  const rows = [];
  let next = start + (hasDelimiter ? 2 : 1);
  while (next < lines.length) {
    const row = parseTableRow(lines[next], !hasDelimiter);
    if (!row || row.length !== header.length || (!hasDelimiter && !row.every(Boolean))) break;
    rows.push(row);
    next += 1;
  }
  return { header, rows, next };
}

function renderTableRow(cells, cellTag) {
  return `<tr>${cells.map(cell => `<${cellTag}>${renderInlineMarkdown(cell)}</${cellTag}>`).join('')}</tr>`;
}

function renderMarkdown(value) {
  const lines = String(value ?? '').replace(/\r\n?/g, '\n').split('\n');
  const output = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    if (/^```/.test(line)) {
      const language = line.slice(3).trim();
      const code = [];
      index += 1;
      while (index < lines.length && !/^```/.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      output.push(`<pre><code${language ? ` data-language="${escapeHtml(language)}"` : ''}>${escapeHtml(code.join('\n'))}</code></pre>`);
      continue;
    }
    const table = parseMarkdownTable(lines, index);
    if (table) {
      const header = renderTableRow(table.header, 'th');
      const body = table.rows.map(row => renderTableRow(row, 'td')).join('');
      output.push(`<table><thead>${header}</thead><tbody>${body}</tbody></table>`);
      index = table.next;
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      output.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }
    const list = line.match(/^\s*([-+*]|\d+\.)\s+(.+)$/);
    if (list) {
      const ordered = /\d+\./.test(list[1]);
      const tag = ordered ? 'ol' : 'ul';
      const start = ordered ? Number.parseInt(list[1], 10) : null;
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*([-+*]|\d+\.)\s+(.+)$/);
        if (!item || /\d+\./.test(item[1]) !== ordered) break;
        items.push(`<li>${renderInlineMarkdown(item[2])}</li>`);
        index += 1;
        let next = index;
        while (next < lines.length && !lines[next].trim()) next += 1;
        const nextItem = next < lines.length
          ? lines[next].match(/^\s*([-+*]|\d+\.)\s+(.+)$/)
          : null;
        if (!nextItem || /\d+\./.test(nextItem[1]) !== ordered) break;
        index = next;
      }
      const startAttribute = ordered && start !== 1 ? ` start="${start}"` : '';
      output.push(`<${tag}${startAttribute}>${items.join('')}</${tag}>`);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        quote.push(lines[index++].replace(/^>\s?/, ''));
      }
      output.push(`<blockquote>${renderMarkdown(quote.join('\n'))}</blockquote>`);
      continue;
    }
    const paragraph = [];
    while (index < lines.length && lines[index].trim()
      && !/^```/.test(lines[index])
      && !/^(#{1,6})\s+/.test(lines[index])
      && !/^\s*([-+*]|\d+\.)\s+/.test(lines[index])
      && !/^>\s?/.test(lines[index])) {
      if (parseMarkdownTable(lines, index)) break;
      paragraph.push(renderInlineMarkdown(lines[index++]));
    }
    output.push(`<p>${paragraph.join('<br>')}</p>`);
  }
  return output.join('');
}

function renderEventDetail(event) {
  if (!event.detail) return '';
  const content = ['user', 'assistant'].includes(event.kind)
    ? renderMarkdown(event.detail)
    : escapeHtml(event.detail);
  return `<div class="event-detail">${content}</div>`;
}
