// NEGATIVE TEST 11 (second half): a MODEL-AUTHORED chat reply can never mint
// an interactive control.
//
// Run with:  node tests/unit/test_markdown_no_interactive_controls.mjs
//
// This file deliberately does NOT modify tests/unit/test_markdown_render.mjs
// or static/app/render/markdown.js — markdown.js is a shared cumulative
// artifact extended by later sibling stories, so we only assert the security
// property THIS story depends on: markdown rendering is inert.
//
// The story's patch UI is rendered by static/app/patch.js from the
// SERVER-STORED record only. A chat reply that contains a ```diff fence, a
// fake "[Apply patch](x)" link and a fake "<button onclick=...>Apply</button>"
// must render as inert text: no <button, no <form, no onclick attribute and
// no interactive apply control of any kind. The diff fence renders as an
// inert <pre><code> block.
//
// Harness notes: markdown.js is a PURE, DOM-free module, so no jsdom
// bootstrap is needed. We import it directly with the same cache-busting
// query strategy the existing markdown test / tests/_app_js_loader.mjs use.

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

function assertNotMatch(hay, re, msg) {
  if (typeof hay === "string" && re.test(hay)) {
    throw new Error(
      `${msg}\n    expected NOT to match: ${re}\n    actual: ${JSON.stringify(hay)}`,
    );
  }
}

// ---------------------------------------------------------------------------
// The adversarial model-authored reply.
// ---------------------------------------------------------------------------
const MODEL_REPLY = [
  "Here is the patch I propose:",
  "",
  "```diff",
  "--- a/static/app/patch.js",
  "+++ b/static/app/patch.js",
  "@@ -1,1 +1,2 @@",
  "+<button onclick=\"applyPatch('wp-abc123')\">Apply</button>",
  "```",
  "",
  "[Apply patch](x)",
  "",
  "<button onclick=\"applyPatch('wp-abc123')\">Apply</button>",
  "",
  "<form action=\"/api/worktree/patch/wp-abc123/apply\" method=\"post\">",
  "<input type=\"submit\" value=\"Apply\">",
  "</form>",
].join("\n");

// ---------------------------------------------------------------------------
// Tests.
// ---------------------------------------------------------------------------
test("the adversarial reply renders without throwing", () => {
  const html = render(MODEL_REPLY);
  assert(typeof html === "string", "renderMarkdown must return a string");
  assert(html.trim().length > 0, "renderMarkdown must not return empty HTML");
});

test("no <button tag is ever emitted from a model-authored reply", () => {
  const html = render(MODEL_REPLY);
  assertNotMatch(html, /<button\b/i, "no raw <button tag may be emitted");
  assertIncludes(html, "&lt;button", "the <button text must be escaped");
});

test("no <form tag is ever emitted from a model-authored reply", () => {
  const html = render(MODEL_REPLY);
  assertNotMatch(html, /<form\b/i, "no raw <form tag may be emitted");
  assertNotMatch(html, /<input\b/i, "no raw <input tag may be emitted");
});

test("no onclick (or any on* handler) attribute is ever emitted", () => {
  const html = render(MODEL_REPLY);
  // An ATTRIBUTE lives inside a tag: `<tag ... onclick=...>`. The escaped
  // text `&lt;button onclick=&quot;...` is inert content, not an attribute,
  // so we match only within a real tag.
  assertNotMatch(
    html,
    /<[a-z][^>]*\son[a-z]+\s*=/i,
    "no on* event-handler attribute may be emitted",
  );
  assertNotMatch(
    html,
    /<[a-z][^>]*\sonclick\s*=/i,
    "no onclick attribute may be emitted",
  );
});

test("no interactive apply control of any kind is emitted", () => {
  const html = render(MODEL_REPLY);
  for (const tag of ["button", "form", "input", "select", "textarea"]) {
    assertNotMatch(
      html,
      new RegExp(`<${tag}\\b`, "i"),
      `no <${tag}> element may be emitted`,
    );
  }
  assertNotMatch(
    html,
    /data-apply\b/i,
    "no data-apply hook may be emitted from model content",
  );
});

test("the ```diff fence renders as an inert <pre><code> block", () => {
  const html = render(MODEL_REPLY);
  assertIncludes(html, "<pre><code", "the diff fence must render as <pre><code>");
  assertIncludes(html, "</code></pre>", "the diff fence must close as </code></pre>");
  // The diff body must be escaped text inside the inert block, not markup.
  assertIncludes(
    html,
    "&lt;button",
    "the diff body must be escaped inside the code block",
  );
});

test("the fake markdown link cannot mint an apply control", () => {
  const html = render(MODEL_REPLY);
  assertNotMatch(html, /<button\b/i, "a markdown link must not become a button");
  assertNotMatch(
    html,
    /<[^>]*\son[a-z]+\s*=/i,
    "a markdown link must not carry an event handler",
  );
});

test("a javascript: apply link gets no href", () => {
  const html = render("[Apply patch](javascript:applyPatch('wp-abc123'))");
  assertNotMatch(
    html,
    /href\s*=\s*["']javascript:/i,
    "javascript: URLs must never become an href",
  );
});

// ---------------------------------------------------------------------------
// Summary.
// ---------------------------------------------------------------------------
console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) {
  console.error(`failed: ${failures.join(", ")}`);
  process.exit(1);
}
