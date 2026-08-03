// markdown.js — a small, dependency-free Markdown -> HTML renderer for the
// Guide screen (v2 UI handoff §4.11). Not a general-purpose parser: covers
// exactly what the repo's own docs (QUICKSTART.md/HOW_IT_WORKS.md) use —
// headings, paragraphs, lists, code fences, inline code/bold/italic/links,
// blockquotes, and hr. No CDN, no npm package.

function escapeHtml(str) {
  return str.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}

function inline(text) {
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');
  out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_m, text, href) => {
    // Guard the href scheme: only http(s)/mailto/anchor/relative links pass;
    // anything else (javascript:, data:, …) collapses to '#'. Quotes are
    // neutralized so a crafted URL can't break out of the attribute. `text`
    // is already escaped (escapeHtml ran over the whole line above).
    const h = href.trim();
    const safe = /^(https?:|mailto:|#|\/|\.)/i.test(h) ? h.replace(/"/g, '%22') : '#';
    return `<a href="${safe}" target="_blank" rel="noopener">${text}</a>`;
  });
  return out;
}

export function renderMarkdown(md) {
  const lines = (md || '').replace(/\r\n/g, '\n').split('\n');
  const html = [];
  let inCode = false, codeLines = [];
  let listItems = null; // 'ul' | 'ol' | null
  let paraLines = [];
  // Once true, a blank line has interrupted the open list and hasn't been
  // resolved yet -- a same-kind item right after still belongs to this list
  // (a "loose list", one blank line between items for source readability),
  // but anything else means the list actually ended at the blank line.
  let blankSinceList = false;

  function flushPara() {
    if (paraLines.length) { html.push(`<p>${inline(paraLines.join(' '))}</p>`); paraLines = []; }
  }
  function flushList() {
    if (listItems) { html.push(`</${listItems}>`); listItems = null; }
    blankSinceList = false;
  }

  for (const raw of lines) {
    const line = raw;

    if (line.trim().startsWith('```')) {
      flushPara(); flushList();
      if (inCode) { html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`); codeLines = []; inCode = false; }
      else inCode = true;
      continue;
    }
    if (inCode) { codeLines.push(line); continue; }

    if (!line.trim()) {
      flushPara();
      if (listItems) blankSinceList = true; else flushList();
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushPara(); flushList();
      const level = heading[1].length;
      html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }

    if (/^\s*---+\s*$/.test(line)) { flushPara(); flushList(); html.push('<hr>'); continue; }

    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    if (ol || ul) {
      flushPara();
      const kind = ol ? 'ol' : 'ul';
      if (listItems !== kind) { flushList(); html.push(`<${kind}>`); listItems = kind; }
      html.push(`<li>${inline((ol || ul)[1])}</li>`);
      blankSinceList = false;
      continue;
    }
    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) { flushPara(); flushList(); html.push(`<blockquote>${inline(quote[1])}</blockquote>`); continue; }

    if (listItems && !blankSinceList) {
      // Lazy continuation: a hard-wrapped line with no marker of its own,
      // directly following a list item (no blank line between), extends
      // that item instead of closing the list and spilling the rest of the
      // sentence into an orphan paragraph. A blank line first means this is
      // a new paragraph, not a continuation -- see the blank-line branch.
      const last = html[html.length - 1];
      if (last && last.startsWith('<li>') && last.endsWith('</li>')) {
        html[html.length - 1] = `${last.slice(0, -5)} ${inline(line.trim())}</li>`;
        continue;
      }
    }
    flushList();

    paraLines.push(line.trim());
  }
  flushPara(); flushList();
  if (inCode) html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);

  return html.join('\n');
}
