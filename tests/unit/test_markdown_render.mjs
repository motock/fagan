// Tests for static/app/render/markdown.js — renderMarkdown(text) -> html.
//
// Run with:  node tests/unit/test_markdown_render.mjs
//
// Written test-first (TDD) for the Comms markdown-rendering story. Until
// static/app/render/markdown.js exists and exports renderMarkdown, every
// test below fails with the module-load error — that is the expected RED
// state; a later dispatch implements against these tests.
//
// Harness notes:
//   - markdown.js is a PURE, DOM-free module, so no jsdom / document stub /
//     tests/_app_js_loader.mjs bootstrap is needed. We import it directly
//     with the same cache-busting query strategy the loader uses.
//   - The module is a cumulative shared artifact (later sibling stories may
//     extend it in place), so we assert only the behaviour THIS story adds
//     and never a byte-for-byte hash of the file.
//
// Graded criteria (see the story brief):
//   POSITIVE
//   - plain text -> escaped text wrapped in <p>
//   - ATX headings # .. #### -> <h1>..<h4> (##### is not a heading)
//   - unordered (- and *) and ordered (1.) lists, one level of nesting
//   - fenced code blocks, with and without an info string -> <pre><code>
//   - GitHub-style pipe tables -> <table><thead>..</thead><tbody>..</tbody>
//   - blockquotes -> <blockquote>
//   - blank-line-separated paragraphs (a single newline stays one paragraph)
//   - inline **bold**, *italic*, _italic_, `code`, [text](url)
//   NEGATIVE / SECURITY
//   - <img src=x onerror=...> is escaped: &lt;img present, no <img tag,
//     no event-handler attribute in any emitted tag
//   - <script> is escaped, never emitted raw
//   - javascript: links get no href; https/http/mailto links get href +
//     rel="noopener noreferrer"
//   - HTML inside a fenced code block stays escaped
//   - output contains ONLY the allowed tag set, no class/style/on* attrs
//   - deterministic, no global state, no DOM/global access, no imports
//
// Deliberately NOT asserted (left open by the brief):
//   - the exact whitespace/newlines BETWEEN block elements
//   - whether a soft line break inside a paragraph becomes <br> or a newline
//   - tables written without outer pipes
//   - the exact escape spelling for apostrophes (&#39; / &#x27; / &apos;)
//   - the module's total export set (later siblings may add exports)

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const MODULE_PATH = path.join(
  HERE,
  "..",
  "..",
  "static",
  "app",
  "render",
  "markdown.js",
);

// ---------------------------------------------------------------------------
// Module load (cache-busted dynamic import, mirroring _app_js_loader.mjs).
// A load failure is captured and re-thrown from every test so the suite fails
// for the RIGHT reason (missing module / missing export), not a harness bug.
// ---------------------------------------------------------------------------
let renderMarkdown = null;
let loadError = null;
try {
  const mod = await import(`${pathToFileURL(MODULE_PATH).href}?t=${Date.now()}`);
  if (typeof mod.renderMarkdown !== "function") {
    loadError = new Error(
      "static/app/render/markdown.js must export a function named renderMarkdown",
    );
  } else {
    renderMarkdown = mod.renderMarkdown;
  }
} catch (err) {
  loadError = err;
}

function requireModule() {
  if (loadError) throw loadError;
  return renderMarkdown;
}

function render(text) {
  return requireModule()(text);
}

// ---------------------------------------------------------------------------
// Tiny assertion harness (no test framework in this repo).
// ---------------------------------------------------------------------------
let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`ok - ${name}`);
  } catch (err) {
    failures.push(name);
    console.error(`not ok - ${name}`);
    console.error(`    ${(err && err.message) || err}`);
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function assertEqual(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error(
      `${msg}\n    expected: ${JSON.stringify(expected)}\n    actual:   ${JSON.stringify(actual)}`,
    );
  }
}

function assertIncludes(hay, needle, msg) {
  if (typeof hay !== "string" || !hay.includes(needle)) {
    throw new Error(
      `${msg}\n    expected to include: ${JSON.stringify(needle)}\n    actual: ${JSON.stringify(hay)}`,
    );
  }
}

function assertNotIncludes(hay, needle, msg) {
  if (typeof hay === "string" && hay.includes(needle)) {
    throw new Error(
      `${msg}\n    expected NOT to include: ${JSON.stringify(needle)}\n    actual: ${JSON.stringify(hay)}`,
    );
  }
}

function assertMatch(hay, re, msg) {
  if (typeof hay !== "string" || !re.test(hay)) {
    throw new Error(
      `${msg}\n    expected to match: ${re}\n    actual: ${JSON.stringify(hay)}`,
    );
  }
}

function assertNotMatch(hay, re, msg) {
  if (typeof hay === "string" && re.test(hay)) {
    throw new Error(
      `${msg}\n    expected NOT to match: ${re}\n    actual: ${JSON.stringify(hay)}`,
    );
  }
}

// ---------------------------------------------------------------------------
// Output-contract helpers.
// ---------------------------------------------------------------------------
const ALLOWED_TAGS = new Set([
  "p",
  "h1",
  "h2",
  "h3",
  "h4",
  "ul",
  "ol",
  "li",
  "pre",
  "code",
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
  "blockquote",
  "strong",
  "em",
  "a",
  "br",
]);

function tagsOf(html) {
  return html.match(/<\/?[a-zA-Z][^>]*>/g) || [];
}

function assertOnlyAllowedTags(html, label) {
  for (const tag of tagsOf(html)) {
    const m = tag.match(/^<\/?([a-zA-Z0-9]+)/);
    const name = m ? m[1].toLowerCase() : "";
    assert(
      ALLOWED_TAGS.has(name),
      `${label}: disallowed tag <${name}> in output: ${JSON.stringify(html)}`,
    );
  }
}

// Event-handler attributes only matter in TAG context. Escaped text such as
// "&lt;img src=x onerror=alert(1)&gt;" is inert (it is text, not a tag), so we
// scan each emitted tag rather than the raw string.
function assertNoEventHandlerAttributes(html, label) {
  for (const tag of tagsOf(html)) {
    assertNotMatch(
      tag,
      /\son[a-z]+\s*=/i,
      `${label}: event-handler attribute inside tag ${JSON.stringify(tag)}`,
    );
  }
}

// Like the event-handler check, this scans TAG context only: escaped text such
// as "&lt;div class=&quot;x&quot;&gt;" is inert and must not trip the check.
function assertNoClassesOrStyles(html, label) {
  for (const tag of tagsOf(html)) {
    assertNotMatch(tag, /\bclass\s*=/i, `${label}: class attribute inside tag ${JSON.stringify(tag)}`);
    assertNotMatch(tag, /\bstyle\s*=/i, `${label}: style attribute inside tag ${JSON.stringify(tag)}`);
  }
}

function assertSafeOutput(html, label) {
  assertOnlyAllowedTags(html, label);
  assertNoEventHandlerAttributes(html, label);
  assertNoClassesOrStyles(html, label);
  assertNotMatch(html, /<script/i, `${label}: output must not contain a <script> tag`);
  assertNotMatch(html, /<img/i, `${label}: output must not contain an <img> tag`);
}

// A representative document exercising every block + inline element this
// story adds, plus hostile text.
const COMPREHENSIVE = [
  "# Heading one",
  "",
  "Some **bold** and *italic* and `code` and a [link](https://example.com).",
  "",
  "- one",
  "- two",
  "",
  "1. first",
  "2. second",
  "",
  "> quoted",
  "",
  "```python",
  "print('<hi>')",
  "```",
  "",
  "| A | B |",
  "| --- | --- |",
  "| 1 | 2 |",
  "",
  'plain <script>alert(1)</script> & "quotes" \'apostrophes\'',
].join("\n");

// ---------------------------------------------------------------------------
// Module shape / purity.
// ---------------------------------------------------------------------------
test("module exports renderMarkdown as a function", () => {
  assertEqual(typeof requireModule(), "function", "renderMarkdown must be a function");
});

test("module is importable by plain Node with no DOM globals", () => {
  // The dynamic import at the top of this file already proves this: there is
  // no document/window stub installed anywhere in this test file.
  assertEqual(typeof renderMarkdown, "function", "module must load in plain Node");
});

test("module source has no imports, no require, no DOM/global access", () => {
  requireModule();
  const raw = readFileSync(MODULE_PATH, "utf8");
  assert(raw.trim().length > 0, "markdown.js must not be empty");
  // Strip comments so prose mentioning "window." etc. cannot false-positive.
  const src = raw
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1");
  assertNotMatch(src, /^\s*import\s/m, "must not import other frontend modules");
  assertNotMatch(src, /\brequire\s*\(/, "must not use require()");
  assertNotMatch(src, /\bdocument\s*\./, "must not access document");
  assertNotMatch(src, /\bwindow\s*\./, "must not access window");
  assertNotMatch(src, /\bfetch\s*\(/, "must not call fetch");
  assertNotMatch(src, /\bglobalThis\b/, "must not touch global state");
});

// ---------------------------------------------------------------------------
// Plain text / paragraphs.
// ---------------------------------------------------------------------------
test("plain text with no markdown tokens is escaped text wrapped in <p>", () => {
  assertEqual(render("hello world").trim(), "<p>hello world</p>", "plain text paragraph");
});

test("plain text escapes <, > and &", () => {
  const html = render("1 < 2 & 3 > 2");
  assertIncludes(html, "&lt;", "literal < must be escaped");
  assertIncludes(html, "&gt;", "literal > must be escaped");
  assertIncludes(html, "&amp;", "literal & must be escaped");
  assertNotIncludes(html, "1 < 2", "raw < must not survive");
  assertNotIncludes(html, "3 > 2", "raw > must not survive");
});

test("plain text escapes double quotes and apostrophes", () => {
  const html = render('He said "hi" and \'bye\'');
  assertIncludes(html, "&quot;hi&quot;", "double quotes must be escaped");
  assertMatch(
    html,
    /&#39;bye&#39;|&#x27;bye&#x27;|&apos;bye&apos;/,
    "apostrophes must be escaped",
  );
});

test("blank-line-separated input becomes two paragraphs", () => {
  const html = render("first paragraph\n\nsecond paragraph");
  assertEqual((html.match(/<p\b/g) || []).length, 2, "expected exactly two <p> blocks");
  assertIncludes(html, "first paragraph", "first paragraph text");
  assertIncludes(html, "second paragraph", "second paragraph text");
});

test("a single newline inside a paragraph does not split it", () => {
  const html = render("line one\nline two");
  assertEqual((html.match(/<p\b/g) || []).length, 1, "soft break must stay in one <p>");
  assertIncludes(html, "line one", "first line text");
  assertIncludes(html, "line two", "second line text");
});

test("empty and whitespace-only input return a string without throwing", () => {
  assertEqual(typeof render(""), "string", "empty input returns a string");
  assertEqual(typeof render("   \n  "), "string", "whitespace input returns a string");
  assertSafeOutput(render(""), "empty input");
});

test("null and undefined input do not throw and return a string", () => {
  assertEqual(typeof render(null), "string", "null input returns a string");
  assertEqual(typeof render(undefined), "string", "undefined input returns a string");
});

// ---------------------------------------------------------------------------
// Headings.
// ---------------------------------------------------------------------------
test("ATX headings # .. #### map to h1 .. h4", () => {
  assertEqual(render("# Title").trim(), "<h1>Title</h1>", "# -> h1");
  assertEqual(render("## Title").trim(), "<h2>Title</h2>", "## -> h2");
  assertEqual(render("### Title").trim(), "<h3>Title</h3>", "### -> h3");
  assertEqual(render("#### Title").trim(), "<h4>Title</h4>", "#### -> h4");
});

test("##### is not a heading (no h5/h6 emitted)", () => {
  const html = render("##### Title");
  assertNotMatch(html, /<h5/i, "h5 must not be emitted");
  assertNotMatch(html, /<h6/i, "h6 must not be emitted");
  assertIncludes(html, "Title", "text is still rendered");
});

test("inline formatting works inside a heading", () => {
  const html = render("# **Bold** title");
  assertIncludes(html, "<h1>", "heading element");
  assertIncludes(html, "<strong>Bold</strong>", "bold inside heading");
});

// ---------------------------------------------------------------------------
// Lists.
// ---------------------------------------------------------------------------
test("unordered list with '-' renders ul/li", () => {
  const html = render("- alpha\n- beta");
  assertMatch(html, /<ul>/, "expected a <ul>");
  assertIncludes(html, "<li>alpha</li>", "first item");
  assertIncludes(html, "<li>beta</li>", "second item");
  assertIncludes(html, "</ul>", "closing </ul>");
});

test("unordered list with '*' renders ul/li", () => {
  const html = render("* alpha\n* beta");
  assertMatch(html, /<ul>/, "expected a <ul>");
  assertIncludes(html, "<li>alpha</li>", "first item");
  assertIncludes(html, "<li>beta</li>", "second item");
});

test("ordered list renders ol/li", () => {
  const html = render("1. alpha\n2. beta");
  assertMatch(html, /<ol>/, "expected an <ol>");
  assertIncludes(html, "<li>alpha</li>", "first item");
  assertIncludes(html, "<li>beta</li>", "second item");
  assertIncludes(html, "</ol>", "closing </ol>");
});

test("unordered list supports one level of nesting", () => {
  const html = render("- parent\n  - child");
  assertEqual((html.match(/<ul\b/g) || []).length, 2, "expected a nested <ul>");
  assertIncludes(html, "parent", "parent text");
  assertIncludes(html, "child", "child text");
  assertSafeOutput(html, "nested unordered list");
});

test("ordered list supports one level of nesting", () => {
  const html = render("1. parent\n  1. child");
  assertEqual((html.match(/<ol\b/g) || []).length, 2, "expected a nested <ol>");
  assertIncludes(html, "parent", "parent text");
  assertIncludes(html, "child", "child text");
  assertSafeOutput(html, "nested ordered list");
});

test("deeper-than-one-level nesting still produces safe output", () => {
  const html = render("- a\n  - b\n    - c");
  assertIncludes(html, "a", "level 1 text");
  assertIncludes(html, "b", "level 2 text");
  assertIncludes(html, "c", "level 3 text");
  assertSafeOutput(html, "deeply nested list");
});

test("inline formatting works inside a list item", () => {
  const html = render("- **bold** item");
  assertIncludes(html, "<li><strong>bold</strong> item</li>", "bold inside li");
});

// ---------------------------------------------------------------------------
// Fenced code blocks.
// ---------------------------------------------------------------------------
test("fenced code block renders <pre><code>", () => {
  const html = render("```\nconst x = 1;\n```");
  assertIncludes(html, "<pre><code>", "opening pre/code");
  assertIncludes(html, "const x = 1;", "code content");
  assertIncludes(html, "</code></pre>", "closing code/pre");
  assertNoClassesOrStyles(html, "fenced code");
});

test("fenced code block with an info string renders <pre><code> and drops the info string", () => {
  const html = render("```python\nprint('hi')\n```");
  assertIncludes(html, "<pre><code>", "opening pre/code");
  assertIncludes(html, "print('hi')", "code content");
  assertIncludes(html, "</code></pre>", "closing code/pre");
  assertNotIncludes(html, "python", "info string must not be emitted");
  assertNoClassesOrStyles(html, "fenced code with info string");
});

test("HTML inside a fenced code block stays escaped", () => {
  const html = render('```\n<div class="x">hi</div>\n```');
  assertIncludes(html, "&lt;div", "opening tag escaped");
  assertIncludes(html, "&lt;/div&gt;", "closing tag escaped");
  assertNotMatch(html, /<div/i, "no literal <div> tag");
  assertSafeOutput(html, "code block with HTML");
});

// ---------------------------------------------------------------------------
// Tables.
// ---------------------------------------------------------------------------
test("pipe table renders table/thead/tbody with th and td cells", () => {
  const html = render("| Name | Age |\n| --- | --- |\n| Alice | 30 |");
  assertIncludes(html, "<table>", "table element");
  assertIncludes(html, "<thead>", "thead element");
  assertIncludes(html, "<tr>", "row element");
  assertIncludes(html, "<th>Name</th>", "first header cell");
  assertIncludes(html, "<th>Age</th>", "second header cell");
  assertIncludes(html, "</thead>", "closing thead");
  assertIncludes(html, "<tbody>", "tbody element");
  assertIncludes(html, "<td>Alice</td>", "first body cell");
  assertIncludes(html, "<td>30</td>", "second body cell");
  assertIncludes(html, "</tbody>", "closing tbody");
  assertIncludes(html, "</table>", "closing table");
  assert(html.indexOf("<thead") < html.indexOf("<tbody"), "thead must precede tbody");
  assertNoClassesOrStyles(html, "pipe table");
});

test("table cell text is escaped", () => {
  const html = render("| A | B |\n| --- | --- |\n| <b>x</b> | & |");
  assertIncludes(html, "&lt;b&gt;x&lt;/b&gt;", "cell HTML escaped");
  assertIncludes(html, "&amp;", "cell ampersand escaped");
  assertNotMatch(html, /<b>/i, "no literal <b> tag");
  assertSafeOutput(html, "table with hostile cells");
});

// ---------------------------------------------------------------------------
// Blockquotes.
// ---------------------------------------------------------------------------
test("blockquote renders <blockquote>", () => {
  const html = render("> quoted text");
  assertIncludes(html, "<blockquote>", "opening blockquote");
  assertIncludes(html, "quoted text", "quote text");
  assertIncludes(html, "</blockquote>", "closing blockquote");
});

// ---------------------------------------------------------------------------
// Inline formatting.
// ---------------------------------------------------------------------------
test("**bold** renders <strong>", () => {
  assertEqual(render("**bold**").trim(), "<p><strong>bold</strong></p>", "bold");
});

test("*italic* renders <em>", () => {
  assertEqual(render("*italic*").trim(), "<p><em>italic</em></p>", "italic with *");
});

test("_italic_ renders <em>", () => {
  assertEqual(render("_italic_").trim(), "<p><em>italic</em></p>", "italic with _");
});

test("`inline code` renders <code>", () => {
  assertEqual(render("`x = 1`").trim(), "<p><code>x = 1</code></p>", "inline code");
});

test("inline code content is escaped", () => {
  const html = render("`<div>`");
  assertIncludes(html, "<code>&lt;div&gt;</code>", "inline code escaped");
  assertNotMatch(html, /<div/i, "no literal <div> tag");
});

// ---------------------------------------------------------------------------
// Links.
// ---------------------------------------------------------------------------
test("https link renders an anchor with href and rel=noopener noreferrer", () => {
  const html = render("[link](https://example.com)");
  const anchor = html.match(/<a\b[^>]*>[\s\S]*?<\/a>/);
  assert(anchor, `expected an <a> element in ${JSON.stringify(html)}`);
  assertMatch(anchor[0], /href="https:\/\/example\.com\/?"/, "href points at the URL");
  assertIncludes(anchor[0], 'rel="noopener noreferrer"', "rel attribute present");
  assertIncludes(anchor[0], ">link</a>", "link label rendered as anchor text");
});

test("http link renders an href", () => {
  const html = render("[x](http://example.com)");
  assertMatch(html, /href="http:\/\/example\.com\/?"/, "http href");
  assertIncludes(html, 'rel="noopener noreferrer"', "rel attribute present");
});

test("mailto link renders an href", () => {
  const html = render("[mail](mailto:me@example.com)");
  assertMatch(html, /href="mailto:me@example\.com"/, "mailto href");
  assertIncludes(html, 'rel="noopener noreferrer"', "rel attribute present");
});

test("link label text is escaped", () => {
  const html = render("[<b>hi</b>](https://example.com)");
  assertIncludes(html, "&lt;b&gt;hi&lt;/b&gt;", "label escaped");
  assertNotMatch(html, /<b>/i, "no literal <b> tag");
});

// ---------------------------------------------------------------------------
// SECURITY / negative cases.
// ---------------------------------------------------------------------------
test("raw <img onerror> input is escaped and inert", () => {
  const html = render("<img src=x onerror=alert(1)>");
  assertIncludes(html, "&lt;img", "the < must be escaped");
  assertNotMatch(html, /<img\b/i, "no literal <img tag");
  assertNoEventHandlerAttributes(html, "img injection");
  assertOnlyAllowedTags(html, "img injection");
});

test("raw <script> input is escaped and inert", () => {
  const html = render("<script>alert(1)</script>");
  assertNotMatch(html, /<script/i, "no literal <script tag");
  assertIncludes(html, "&lt;script&gt;", "escaped script text");
  assertSafeOutput(html, "script injection");
});

test("javascript: link gets no href and stays inert text", () => {
  const html = render("[click](javascript:alert(1))");
  assertNotMatch(html, /href\s*=\s*["']?\s*javascript:/i, "no javascript: href");
  assertNotMatch(html, /href=/i, "no href attribute at all for a javascript: URL");
  assertIncludes(html, "click", "label rendered as text");
  assertNotMatch(html, /<script/i, "no script tag");
});

test("data: and vbscript: links get no href", () => {
  const dataHtml = render("[x](data:text/html,<script>alert(1)</script>)");
  assertNotMatch(dataHtml, /href\s*=\s*["']?\s*data:/i, "no data: href");
  const vbHtml = render("[x](vbscript:msgbox(1))");
  assertNotMatch(vbHtml, /href\s*=\s*["']?\s*vbscript:/i, "no vbscript: href");
});

test("comprehensive document emits only allowed tags and no classes/styles", () => {
  const html = render(COMPREHENSIVE);
  assertSafeOutput(html, "comprehensive document");
  assertIncludes(html, "<h1>", "heading rendered");
  assertIncludes(html, "<strong>", "bold rendered");
  assertIncludes(html, "<em>", "italic rendered");
  assertIncludes(html, "<code>", "code rendered");
  assertIncludes(html, "<ul>", "unordered list rendered");
  assertIncludes(html, "<ol>", "ordered list rendered");
  assertIncludes(html, "<blockquote>", "blockquote rendered");
  assertIncludes(html, "<table>", "table rendered");
  assertIncludes(html, "<pre><code>", "fenced code rendered");
  assertIncludes(html, "<a ", "link rendered");
});

test("output is deterministic and carries no global state between calls", () => {
  const first = render(COMPREHENSIVE);
  const second = render(COMPREHENSIVE);
  assertEqual(second, first, "same input must produce identical output");
  render("some other input");
  const third = render(COMPREHENSIVE);
  assertEqual(third, first, "an intervening call must not change the result");
});

// ---------------------------------------------------------------------------
// REGRESSION: inline code / links nested inside **bold** or *italic*.
//
// renderInline() stashes each structural fragment behind a U+E000<index>U+E001
// placeholder. The bold/italic passes stash a fragment that ALREADY contains an
// inner placeholder (the code span or link), so a single-pass restore leaves the
// inner placeholder in the output verbatim: the nested link/code is silently
// dropped and raw private-use characters leak into the emitted HTML. The restore
// must re-scan until no placeholder remains.
//
// The href assertions tolerate an optional trailing slash because `new URL`
// normalizes "https://x.com" to "https://x.com/" — the same tolerance the
// pre-existing link tests use.
// ---------------------------------------------------------------------------

const PLACEHOLDER_LEAK = /[\uE000\uE001]/;

function assertNoPlaceholderLeak(html, label) {
  assertNotMatch(
    html,
    PLACEHOLDER_LEAK,
    `${label}: output must not contain raw U+E000/U+E001 placeholder characters`,
  );
}

test("link nested inside **bold** is rendered, not dropped", () => {
  const html = render("**bold with [a link](https://x.com) inside**");
  assertMatch(
    html,
    /^<p><strong>bold with <a href="https:\/\/x\.com\/?" rel="noopener noreferrer">a link<\/a> inside<\/strong><\/p>$/,
    "nested link inside bold",
  );
  assertNoPlaceholderLeak(html, "nested link inside bold");
});

test("code span nested inside **bold** is rendered, not dropped", () => {
  const html = render("**bold with `code` inside**");
  assertEqual(
    html,
    "<p><strong>bold with <code>code</code> inside</strong></p>",
    "nested code inside bold",
  );
  assertNoPlaceholderLeak(html, "nested code inside bold");
});

test("link nested inside *italic* is rendered, not dropped", () => {
  const html = render("*italic with [a link](https://x.com) inside*");
  assertMatch(
    html,
    /^<p><em>italic with <a href="https:\/\/x\.com\/?" rel="noopener noreferrer">a link<\/a> inside<\/em><\/p>$/,
    "nested link inside italic",
  );
  assertNoPlaceholderLeak(html, "nested link inside italic");
});

test("code span nested inside _italic_ is rendered, not dropped", () => {
  const html = render("_italic with `code` inside_");
  assertEqual(
    html,
    "<p><em>italic with <code>code</code> inside</em></p>",
    "nested code inside underscore italic",
  );
  assertNoPlaceholderLeak(html, "nested code inside underscore italic");
});

test("realistic chat message with a link nested inside bold renders fully", () => {
  const html = render("Great **work on [the PR](https://github.com/x/y/pull/1) today**");
  assertMatch(
    html,
    /^<p>Great <strong>work on <a href="https:\/\/github\.com\/x\/y\/pull\/1" rel="noopener noreferrer">the PR<\/a> today<\/strong><\/p>$/,
    "nested link inside bold in a sentence",
  );
  assertNoPlaceholderLeak(html, "nested link inside bold in a sentence");
});

test("mixed nested inline content leaks no placeholder characters", () => {
  const html = render(
    "**bold with [a link](https://x.com) and `code` inside** and *italic with `code` inside*",
  );
  assertNoPlaceholderLeak(html, "mixed nested inline");
  assertIncludes(html, "<strong>", "bold rendered");
  assertIncludes(html, "<em>", "italic rendered");
  assertIncludes(html, "<code>code</code>", "nested code rendered");
  assertIncludes(html, 'rel="noopener noreferrer"', "nested link rendered");
  assertSafeOutput(html, "mixed nested inline");
});

// The stash must stay a per-call local: a later call must not see an earlier
// call's tokens (or a hoisted counter). Exercised through the public entry
// point, which is the module's only export.
test("consecutive calls with nested inline content do not share stash state", () => {
  const a = render("**bold with [a link](https://x.com) inside**");
  assertMatch(
    a,
    /^<p><strong>bold with <a href="https:\/\/x\.com\/?" rel="noopener noreferrer">a link<\/a> inside<\/strong><\/p>$/,
    "call A (nested link in bold)",
  );
  const b = render("**bold with `code` inside**");
  assertEqual(
    b,
    "<p><strong>bold with <code>code</code> inside</strong></p>",
    "call B (nested code in bold) immediately after call A",
  );
  const c = render("plain **bold** text");
  assertEqual(c, "<p>plain <strong>bold</strong> text</p>", "call C (plain bold) after A and B");
  assertNoPlaceholderLeak(a + b + c, "consecutive nested calls");
});

// Literal U+E000/U+E001 in the INPUT must never be mistaken for a placeholder
// (which silently swallows it). Whatever the chosen spelling (strip, or escape
// to &#xE000;/&#xE001;), no raw private-use character may reach the output and
// the surrounding text must survive.
test("literal U+E000/U+E001 in input never leaks and never swallows text", () => {
  const bare = render("\uE0000\uE001");
  assertNoPlaceholderLeak(bare, "literal placeholder characters");
  assertIncludes(bare, "0", "text between the private-use characters survives");

  const wrapped = render("before \uE0000\uE001 after");
  assertNoPlaceholderLeak(wrapped, "literal placeholder characters in a sentence");
  assertIncludes(wrapped, "before", "text before survives");
  assertIncludes(wrapped, "after", "text after survives");
  assertIncludes(wrapped, "0", "text between the private-use characters survives");
});

// ---------------------------------------------------------------------------
// Summary.
// ---------------------------------------------------------------------------
console.log("");
console.log(`${passed} passed, ${failures.length} failed`);
if (failures.length) {
  console.error("failing tests:");
  for (const name of failures) console.error(`  - ${name}`);
  process.exitCode = 1;
}
