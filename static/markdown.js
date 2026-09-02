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
    .replace(/\[\]\(\)/g, '')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
    .replace(/(^|[^\w])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/(^|[^\w])_([^_\n]+)_/g, '$1<em>$2</em>')
    .replace(/\u0000CODE(\d+)\u0000/g, (_, index) => code[Number(index)]);
  return html.replace(/\u0000LINK(\d+)\u0000/g, (_, index) => links[Number(index)]);
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
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*([-+*]|\d+\.)\s+(.+)$/);
        if (!item || /\d+\./.test(item[1]) !== ordered) break;
        items.push(`<li>${renderInlineMarkdown(item[2])}</li>`);
        index += 1;
      }
      output.push(`<${tag}>${items.join('')}</${tag}>`);
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
