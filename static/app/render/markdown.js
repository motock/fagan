// static/app/render/markdown.js
//
// Pure, DOM-free markdown -> HTML renderer for the Comms chat panel.
//
// Contract (see tests/unit/test_markdown_render.mjs):
//   - renderMarkdown(text) -> html string. Pure: no browser globals, no
//     imports, no module-level mutable state, deterministic across calls.
//   - Every literal piece of input text is HTML-escaped before emission.
//   - Emitted tags are limited to:
//     p h1-h4 ul ol li pre code table thead tbody tr th td blockquote
//     strong em a
//   - Bare semantic elements only: NO class/style/event-handler attributes.
//   - Links: only http:, https: and mailto: URLs get an href, always with
//     rel="noopener noreferrer". Anything else renders as inert escaped text.
//   - Literal U+E000/U+E001 characters in inline text are stripped before
//     rendering: they are this module's internal placeholder alphabet and
//     must never reach the output or be mistaken for a placeholder.

// Full escape for text that lands in element content or an attribute value.
// Order matters: & first so the entities themselves are not double-escaped.
function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Code bodies (fenced blocks and inline code) are element TEXT, never an
// attribute value, so escaping & < > is sufficient to be inert while keeping
// code samples such as print('hi') byte-identical for readability.
function escapeCode(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// Build the anchor for one [label](url) match, or inert escaped text when the
// URL is not an allowlisted scheme. A relative URL throws in `new URL` here
// (no base), which is caught and treated as inert as well.
function buildAnchor(label, url, rawMatch) {
  let parsed = null;
  try {
    parsed = new URL(url);
  } catch (err) {
    parsed = null;
  }
  const protocol = parsed ? parsed.protocol : "";
  if (protocol === "http:" || protocol === "https:" || protocol === "mailto:") {
    const href = escapeHtml(parsed.href);
    const text = escapeHtml(label);
    return `<a href="${href}" rel="noopener noreferrer">${text}</a>`;
  }
  // javascript:, data:, vbscript:, unparsable -> no <a> at all, no href.
  return escapeHtml(rawMatch);
}

// Inline pass: code spans first (protected), then links, then bold/italic.
// Everything structural is stashed as pre-escaped HTML behind a placeholder;
// the residual plain text is escaped exactly once at the end.
//
// Placeholders NEST: the bold/italic passes stash their whole <strong>/<em>
// fragment, which can already contain the placeholder a code span or link
// stashed earlier, so the restore at the bottom must re-scan until no
// placeholder remains. A single replace() pass never re-reads the text it
// just inserted and would leak the inner placeholder verbatim.
function renderInline(raw) {
  const tokens = []; // per-call stash: index == placeholder id. NEVER module-level.
  const stash = (html) => {
    tokens.push(html);
    return `\uE000${tokens.length - 1}\uE001`;
  };

  // 0. Strip literal U+E000/U+E001 from the input. They are this function's
  //    private placeholder alphabet; a literal copy in the source text would
  //    otherwise be parsed as a placeholder and silently swallowed.
  let text = String(raw).replace(/[\uE000\uE001]/g, "");

  // 1. Inline code spans first so their content is never formatted.
  text = text.replace(/`([^`]+)`/g, (_, code) =>
    stash(`<code>${escapeCode(code)}</code>`),
  );

  // 2. Links. The URL may contain one level of balanced parentheses.
  text = text.replace(
    /\[([^\]]*)\]\(([^)\s]*(?:\([^()\s]*\)[^)\s]*)*)\)/g,
    (match, label, url) => stash(buildAnchor(label, url, match)),
  );

  // 3. Bold, then single-star italic, then underscore italic. The underscore
  //    form requires a non-word character (or an edge) on both sides so that
  //    snake_case_identifiers stay literal.
  text = text.replace(/\*\*([^*]+)\*\*/g, (_, body) =>
    stash(`<strong>${escapeHtml(body)}</strong>`),
  );
  text = text.replace(/\*([^*\s][^*]*)\*/g, (_, body) =>
    stash(`<em>${escapeHtml(body)}</em>`),
  );
  text = text.replace(/(?<!\w)_([^_]+)_(?!\w)/g, (_, body) =>
    stash(`<em>${escapeHtml(body)}</em>`),
  );

  // 4. Whatever remains is plain text: escape it, then restore the stashed
  //    HTML. The restore is ITERATIVE and bounded: each pass resolves one
  //    level of placeholder nesting, `pass <= tokens.length` covers the
  //    deepest possible nesting, and the no-progress guard guarantees
  //    termination even if a token re-contained its own placeholder.
  const PLACEHOLDER = /\uE000(\d+)\uE001/; // non-global: safe with .test()
  let out = escapeHtml(text);
  for (let pass = 0; pass <= tokens.length; pass++) {
    if (!PLACEHOLDER.test(out)) break;
    const next = out.replace(/\uE000(\d+)\uE001/g, (_, index) => {
      const token = tokens[Number(index)];
      return token === undefined ? "" : token;
    });
    if (next === out) break; // no progress -> stop
    out = next;
  }
  return out;
}

// A GitHub-style separator row: dashes with optional colons and pipes.
function isSeparatorRow(line) {
  const row = line.trim();
  if (!row.includes("-")) return false;
  return /^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$/.test(row);
}

// Split one table row into trimmed cell strings, dropping the outer pipes.
function splitRow(line) {
  let row = line.trim();
  if (row.startsWith("|")) row = row.slice(1);
  if (row.endsWith("|")) row = row.slice(0, -1);
  return row.split("|").map((cell) => cell.trim());
}

function renderTable(headerCells, bodyRows) {
  const head = `<thead><tr>${headerCells
    .map((cell) => `<th>${renderInline(cell)}</th>`)
    .join("")}</tr></thead>`;
  const body = `<tbody>${bodyRows
    .map(
      (cells) =>
        `<tr>${cells.map((cell) => `<td>${renderInline(cell)}</td>`).join("")}</tr>`,
    )
    .join("")}</tbody>`;
  return `<table>${head}${body}</table>`;
}

// Render list items with at most one level of nesting: an indented item
// becomes a nested list inside the previous top-level <li>.
function buildList(items) {
  let out = "";
  const stack = []; // open list tags, outermost first, max depth 2
  let liOpen = false;

  const closeLi = () => {
    if (liOpen) {
      out += "</li>";
      liOpen = false;
    }
  };
  const closeListsTo = (depth) => {
    while (stack.length > depth) {
      closeLi();
      out += `</${stack.pop()}>`;
    }
  };

  for (const item of items) {
    const type = item.ordered ? "ol" : "ul";
    const depth = item.indent > 0 ? 1 : 0;
    if (stack.length < depth + 1) {
      if (stack.length === 1 && !liOpen) out += "<li>";
      out += `<${type}>`;
      stack.push(type);
    } else if (stack.length > depth + 1) {
      closeListsTo(depth + 1);
    } else if (stack[depth] !== type) {
      closeListsTo(depth);
      out += `<${type}>`;
      stack.push(type);
    } else {
      closeLi();
    }
    out += `<li>${renderInline(item.content)}`;
    liOpen = true;
  }
  closeListsTo(0);
  return out;
}

// Render the stripped inner lines of a blockquote as paragraphs.
function renderBlockquote(quoteLines) {
  const paragraphs = [];
  let current = [];
  for (const line of quoteLines) {
    if (line.trim()) {
      current.push(line.trim());
    } else if (current.length) {
      paragraphs.push(current.join(" "));
      current = [];
    }
  }
  if (current.length) paragraphs.push(current.join(" "));
  const inner = paragraphs
    .map((para) => `<p>${renderInline(para)}</p>`)
    .join("");
  return `<blockquote>${inner}</blockquote>`;
}

// True when the line starts a block that must interrupt a paragraph.
function isBlockStart(line) {
  if (/^\s*```/.test(line)) return true;
  if (/^#{1,4}\s+/.test(line)) return true;
  if (/^\s*>/.test(line)) return true;
  if (/^\s*(?:[-*]|\d+\.)\s+/.test(line)) return true;
  return false;
}

// Render one markdown payload. Block order: fenced code, heading, table,
// blockquote, list, paragraph — blank lines separate blocks.
export function renderMarkdown(text) {
  if (text === null || text === undefined) return "";
  const source = String(text).replace(/\r\n?/g, "\n");
  const lines = source.split("\n");
  const blocks = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    // Fenced code block (with or without an info string, which is dropped).
    if (/^\s*```/.test(line)) {
      i += 1;
      const codeLines = [];
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        codeLines.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1; // consume the closing fence
      blocks.push(`<pre><code>${escapeCode(codeLines.join("\n"))}</code></pre>`);
      continue;
    }

    // ATX heading, 1-4 hashes (##### and deeper are plain text).
    const heading = line.match(/^#{1,4}\s+(.+)$/);
    if (heading) {
      const level = line.match(/^#+/)[0].length;
      blocks.push(`<h${level}>${renderInline(heading[1].trim())}</h${level}>`);
      i += 1;
      continue;
    }

    // Pipe table: header row, then a separator row of dashes.
    if (line.includes("|") && i + 1 < lines.length && isSeparatorRow(lines[i + 1])) {
      const headerCells = splitRow(line);
      i += 2;
      const bodyRows = [];
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
        bodyRows.push(splitRow(lines[i]));
        i += 1;
      }
      blocks.push(renderTable(headerCells, bodyRows));
      continue;
    }

    // Blockquote.
    if (/^\s*>/.test(line)) {
      const quoteLines = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        quoteLines.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      blocks.push(renderBlockquote(quoteLines));
      continue;
    }

    // List (unordered -, * or ordered 1.).
    if (/^\s*(?:[-*]|\d+\.)\s+/.test(line)) {
      const items = [];
      while (i < lines.length) {
        const item = lines[i].match(/^(\s*)([-*]|\d+\.)\s+(.*)$/);
        if (!item) break;
        items.push({
          indent: item[1].length,
          ordered: /\d/.test(item[2]),
          content: item[3].trim(),
        });
        i += 1;
      }
      blocks.push(buildList(items));
      continue;
    }

    // Paragraph: consecutive plain lines, soft breaks joined with a space.
    const paraLines = [];
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) {
      paraLines.push(lines[i].trim());
      i += 1;
    }
    if (paraLines.length) {
      blocks.push(`<p>${renderInline(paraLines.join(" "))}</p>`);
    } else {
      i += 1; // safety: never stall on an unconsumed line
    }
  }

  return blocks.join("\n");
}
