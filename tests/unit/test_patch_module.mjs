// Tests for static/app/patch.js — the worktree-patch review/apply module.
//
// Run with:  node tests/unit/test_patch_module.mjs
//
// Written test-first (TDD). Until static/app/patch.js exists and exports the
// three functions below, every test here fails with the module-load error —
// that is the expected RED state; a later dispatch implements against these
// tests.
//
// Harness notes:
//   - patch.js is a string-template, DOM-free module (like
//     static/app/render/markdown.js), so no jsdom bootstrap is needed. We
//     import it directly with the same cache-busting query strategy the
//     existing markdown test / tests/_app_js_loader.mjs use.
//   - fetchPatchRecord / applyPatch read the shared secret the same way
//     static/app/api.js does: `window.__PIPELINE_API_KEY__`. The test sets
//     globalThis.window before importing so a top-level read cannot throw.
//   - global fetch is stubbed per test; the stub records (url, init).
//
// CONTRACT (this file is the spec the implementer codes against):
//   renderPatchRecord(record) -> string
//     * renders record.paths (every path), record.added_lines, record.status
//       and record.expires_at
//     * renders record.diff_text as unified-diff lines, each line carrying a
//       class token:
//         added line   -> class contains `diff-add`
//         removed line -> class contains `diff-del`
//         context line -> class contains `diff-ctx` (dimmed)
//         hunk header  -> class contains `diff-hunk`
//     * EVERY piece of diff content is HTML-escaped exactly like
//       static/app/render/markdown.js escapes (& < > " '), so a diff
//       containing `<img src=x onerror=alert(1)>` can never reach the DOM as
//       markup
//     * the record object is the ONLY input: extra fields (e.g. a chat-reply
//       markdown blob) are never rendered
//     * tolerates a record with missing/empty fields (returns a string, no
//       throw)
//   fetchPatchRecord(patchId) -> Promise<parsed JSON>
//     * GET /api/worktree/patch/{patchId}
//     * headers: X-Pipeline-Api-Key (window.__PIPELINE_API_KEY__) AND
//       X-Pipeline-Origin: ui
//     * non-2xx rejects with an Error whose message carries the status
//   applyPatch(patchId, confirmationToken) -> Promise<parsed JSON>
//     * POST /api/worktree/patch/{patchId}/apply
//     * same two headers, Content-Type: application/json
//     * body is EXACTLY {confirmation_token: confirmationToken} — never a
//       diff, never paths
//     * non-2xx rejects with an Error whose message carries the status

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const MODULE_PATH = path.join(HERE, "..", "..", "static", "app", "patch.js");

// The shared-secret global must exist before the module is evaluated: a
// top-level `window.__PIPELINE_API_KEY__` read would otherwise throw.
const API_KEY = "test-api-key-abc123";
globalThis.window = { __PIPELINE_API_KEY__: API_KEY };
globalThis.__PIPELINE_API_KEY__ = API_KEY;

// ---------------------------------------------------------------------------
// Module load (cache-busted dynamic import, mirroring _app_js_loader.mjs).
// A load failure is captured and re-thrown from every test so the suite fails
// for the RIGHT reason (missing module / missing export), not a harness bug.
// ---------------------------------------------------------------------------
let mod = null;
let loadError = null;
try {
  mod = await import(`${pathToFileURL(MODULE_PATH).href}?t=${Date.now()}`);
  for (const name of ["renderPatchRecord", "fetchPatchRecord", "applyPatch"]) {
    if (typeof mod[name] !== "function") {
      loadError = new Error(
        `static/app/patch.js must export a function named ${name}`,
      );
      break;
    }
  }
} catch (err) {
  loadError = err;
}

function requireModule() {
  if (loadError) throw loadError;
  return mod;
}

function renderPatchRecord(record) {
  return requireModule().renderPatchRecord(record);
}

function fetchPatchRecord(patchId) {
  return requireModule().fetchPatchRecord(patchId);
}

function applyPatch(patchId, token) {
  return requireModule().applyPatch(patchId, token);
}

// ---------------------------------------------------------------------------
// Tiny assertion harness (no test framework in this repo).
// ---------------------------------------------------------------------------
let passed = 0;
const failures = [];

async function test(name, fn) {
  try {
    await fn();
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

async function assertRejects(fn, msg) {
  let threw = null;
  try {
    await fn();
  } catch (err) {
    threw = err;
  }
  if (!threw) throw new Error(`${msg}: expected the promise to reject`);
  if (!(threw instanceof Error)) {
    throw new Error(`${msg}: expected an Error, got ${typeof threw}`);
  }
  return threw;
}

// ---------------------------------------------------------------------------
// Fixtures / helpers.
// ---------------------------------------------------------------------------

// Mirrors the GET /api/worktree/patch/{patch_id} response body (see
// app/dashboard.py get_worktree_patch_route).
function makeRecord(overrides = {}) {
  return {
    ok: true,
    patch_id: "wp-abc123",
    plan_name: "demo-plan",
    story_key: "story-1",
    paths: ["static/app/patch.js", "tests/unit/test_patch_module.mjs"],
    added_lines: 7,
    diff_text: [
      "--- a/static/app/patch.js",
      "+++ b/static/app/patch.js",
      "@@ -1,3 +1,4 @@",
      " context line",
      "-removed line",
      "+added line",
    ].join("\n"),
    status: "pending",
    created_at: "2024-01-01T00:00:00Z",
    expires_at: "2024-01-01T00:15:00Z",
    confirmation_token: "tok-secret",
    ...overrides,
  };
}

// Return the full element whose class attribute carries `token` AND whose
// inner text includes `needle`, or null. Needed because a unified diff has
// several lines starting with the same marker (e.g. the `+++` file header and
// a `+` added line), so "the first element with class diff-add" is not
// necessarily the line we mean.
function elementWithClassContaining(html, token, needle) {
  const re = new RegExp(
    `<[a-z][a-z0-9]*\\b[^>]*class=["'][^"']*\\b${token}\\b[^"']*["'][^>]*>([\\s\\S]*?)</[a-z][a-z0-9]*>`,
    "gi",
  );
  for (const m of String(html).matchAll(re)) {
    if (m[0].includes(needle)) return m[0];
  }
  return null;
}

// Read a header from a fetch init regardless of how the implementation built
// it (plain object, Headers instance, or array of pairs).
function headerValue(init, name) {
  const h = init && init.headers;
  if (!h) return undefined;
  if (typeof h.get === "function") return h.get(name);
  if (Array.isArray(h)) {
    const hit = h.find(
      (pair) => String(pair[0]).toLowerCase() === name.toLowerCase(),
    );
    return hit ? hit[1] : undefined;
  }
  for (const k of Object.keys(h)) {
    if (k.toLowerCase() === name.toLowerCase()) return h[k];
  }
  return undefined;
}

let captured = null;

function stubFetch(response) {
  captured = null;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return response;
  };
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

// ---------------------------------------------------------------------------
// Module shape.
// ---------------------------------------------------------------------------
await test("patch.js exports exactly the three required functions", () => {
  const m = requireModule();
  const names = Object.keys(m).sort();
  assertEqual(
    names.join(","),
    "applyPatch,fetchPatchRecord,renderPatchRecord",
    "patch.js must export exactly renderPatchRecord, fetchPatchRecord, applyPatch",
  );
  for (const name of names) {
    assertEqual(typeof m[name], "function", `${name} must be a function`);
  }
});

await test("function arities match the specified signatures", () => {
  const m = requireModule();
  assertEqual(m.renderPatchRecord.length, 1, "renderPatchRecord(record)");
  assertEqual(m.fetchPatchRecord.length, 1, "fetchPatchRecord(patchId)");
  assertEqual(m.applyPatch.length, 2, "applyPatch(patchId, confirmationToken)");
});

await test("module source is DOM-free (no document / innerHTML)", () => {
  requireModule();
  const raw = readFileSync(MODULE_PATH, "utf8");
  assert(raw.trim().length > 0, "patch.js must not be empty");
  // Strip comments so prose mentioning "document." cannot false-positive.
  const src = raw
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1");
  assertNotMatch(src, /\bdocument\s*\./, "must not access document");
  assertNotMatch(src, /\binnerHTML\b/, "must not assign innerHTML");
  assertNotMatch(
    src,
    /\binsertAdjacentHTML\b/,
    "must not use insertAdjacentHTML",
  );
});

await test("patch.js is an ES module that does not import the UI wiring files", () => {
  requireModule();
  const raw = readFileSync(MODULE_PATH, "utf8");
  assertMatch(raw, /^\s*export\s/m, "patch.js must be an ES module with exports");
  // This story must not wire itself into the UI: a later story does that.
  assertNotMatch(
    raw,
    /from\s+["'][^"']*\/?(?:main|app)\.js["']/,
    "patch.js must not import main.js / app.js",
  );
});

// ---------------------------------------------------------------------------
// renderPatchRecord — happy path.
// ---------------------------------------------------------------------------
await test("renderPatchRecord returns a non-empty HTML string", () => {
  const html = renderPatchRecord(makeRecord());
  assertEqual(typeof html, "string", "renderPatchRecord must return a string");
  assert(html.trim().length > 0, "renderPatchRecord must not return empty HTML");
});

await test("renderPatchRecord renders every path in the record", () => {
  const html = renderPatchRecord(makeRecord());
  for (const p of ["static/app/patch.js", "tests/unit/test_patch_module.mjs"]) {
    assertIncludes(html, p, `path list must include ${p}`);
  }
});

await test("renderPatchRecord renders the added-lines count", () => {
  const html = renderPatchRecord(makeRecord({ added_lines: 7 }));
  assertIncludes(html, "7", "added-lines count must be rendered");
  assertMatch(html, /added/i, "the added-lines count must be labelled");
});

await test("renderPatchRecord renders the status", () => {
  const html = renderPatchRecord(makeRecord({ status: "pending" }));
  assertIncludes(html, "pending", "status must be rendered");
});

await test("renderPatchRecord renders the expiry", () => {
  const html = renderPatchRecord(makeRecord());
  assertIncludes(html, "2024-01-01T00:15:00Z", "expiry must be rendered");
});

// ---------------------------------------------------------------------------
// renderPatchRecord — unified diff line classes.
// ---------------------------------------------------------------------------
await test("added diff lines carry an add class and the + content", () => {
  const html = renderPatchRecord(makeRecord());
  const el = elementWithClassContaining(html, "(?:diff-)?add", "+added line");
  assert(el, "an added line must carry a class token like diff-add");
  assertIncludes(el, "+added line", "the added line content must be rendered");
});

await test("removed diff lines carry a del class and the - content", () => {
  const html = renderPatchRecord(makeRecord());
  const el = elementWithClassContaining(html, "(?:diff-)?del", "-removed line");
  assert(el, "a removed line must carry a class token like diff-del");
  assertIncludes(el, "-removed line", "the removed line content must be rendered");
});

await test("context diff lines carry a dimmed ctx class", () => {
  const html = renderPatchRecord(makeRecord());
  const el = elementWithClassContaining(html, "(?:diff-)?(?:ctx|context|dim)", "context line");
  assert(el, "a context line must carry a class token like diff-ctx");
  assertIncludes(el, "context line", "the context line content must be rendered");
});

await test("hunk headers carry a hunk class", () => {
  const html = renderPatchRecord(makeRecord());
  const el = elementWithClassContaining(html, "(?:diff-)?hunk", "@@ -1,3 +1,4 @@");
  assert(el, "a hunk header must carry a class token like diff-hunk");
  assertIncludes(el, "@@ -1,3 +1,4 @@", "the hunk header content must be rendered");
});

// ---------------------------------------------------------------------------
// renderPatchRecord — escaping / security.
// ---------------------------------------------------------------------------
await test("diff content is HTML-escaped: <img onerror> never reaches the DOM", () => {
  const html = renderPatchRecord(
    makeRecord({
      diff_text: [
        "@@ -1,1 +1,2 @@",
        "+<img src=x onerror=alert(1)>",
      ].join("\n"),
    }),
  );
  assertIncludes(html, "&lt;img", "the <img tag must be escaped");
  assertNotMatch(html, /<img\b/i, "no raw <img tag may be emitted");
  assertNotMatch(
    html,
    /<[^>]*\sonerror\s*=/i,
    "no onerror event-handler attribute may be emitted",
  );
});

await test("diff content is HTML-escaped: <script> never reaches the DOM", () => {
  const html = renderPatchRecord(
    makeRecord({
      diff_text: ["@@ -1,1 +1,2 @@", "+<script>alert(1)</script>"].join("\n"),
    }),
  );
  assertIncludes(html, "&lt;script&gt;", "the <script> tag must be escaped");
  assertNotMatch(html, /<script\b/i, "no raw <script tag may be emitted");
});

await test("diff content escapes &, <, >, quotes and apostrophes", () => {
  const html = renderPatchRecord(
    makeRecord({
      diff_text: ['@@ -1,1 +1,2 @@', '+a < b & c > d "e" \'f\''].join("\n"),
    }),
  );
  assertIncludes(html, "&lt;", "literal < must be escaped");
  assertIncludes(html, "&gt;", "literal > must be escaped");
  assertIncludes(html, "&amp;", "literal & must be escaped");
  assertIncludes(html, "&quot;", "double quotes must be escaped");
  assertMatch(
    html,
    /&#39;|&#x27;|&apos;/,
    "apostrophes must be escaped",
  );
  assertNotIncludes(html, "a < b", "raw < must not survive");
  assertNotIncludes(html, "c > d", "raw > must not survive");
});

await test("paths and status are escaped too", () => {
  const html = renderPatchRecord(
    makeRecord({
      paths: ["<img src=x onerror=alert(1)>"],
      status: "<script>alert(1)</script>",
    }),
  );
  assertNotMatch(html, /<img\b/i, "no raw <img tag may be emitted from paths");
  assertNotMatch(html, /<script\b/i, "no raw <script tag may be emitted from status");
  assertIncludes(html, "&lt;img", "the path must be escaped");
});

await test("no event-handler attribute is emitted anywhere", () => {
  const html = renderPatchRecord(
    makeRecord({
      diff_text: [
        "@@ -1,1 +1,2 @@",
        '+<button onclick="applyPatch()">Apply</button>',
      ].join("\n"),
    }),
  );
  assertNotMatch(
    html,
    /<[^>]*\son[a-z]+\s*=/i,
    "no on* event-handler attribute may be emitted",
  );
  assertNotMatch(html, /<button\b/i, "no raw <button tag may be emitted");
});

// ---------------------------------------------------------------------------
// renderPatchRecord — the record is the ONLY input.
// ---------------------------------------------------------------------------
await test("chat-reply markdown fields on the record are never rendered", () => {
  const html = renderPatchRecord(
    makeRecord({
      chat_reply: "<button onclick=\"applyPatch('wp-abc123')\">Apply</button>",
      markdown: "[Apply patch](javascript:applyPatch())",
      reply: "```diff\n+<button>Apply</button>\n```",
    }),
  );
  assertNotMatch(html, /<button\b/i, "chat-reply markup must never be rendered");
  assertNotMatch(
    html,
    /<[^>]*\son[a-z]+\s*=/i,
    "chat-reply event handlers must never be rendered",
  );
  assertNotIncludes(
    html,
    "javascript:applyPatch()",
    "chat-reply markdown must never be rendered",
  );
});

// ---------------------------------------------------------------------------
// renderPatchRecord — boundaries / malformed input.
// ---------------------------------------------------------------------------
await test("empty paths, zero added lines and empty diff render without throwing", () => {
  const html = renderPatchRecord(
    makeRecord({ paths: [], added_lines: 0, diff_text: "", status: "pending" }),
  );
  assertEqual(typeof html, "string", "must still return a string");
  assertIncludes(html, "0", "a zero added-lines count must be rendered");
});

await test("a record with missing fields renders without throwing", () => {
  const html = renderPatchRecord({});
  assertEqual(typeof html, "string", "must return a string for a bare record");
});

await test("a single-line diff with no trailing newline renders", () => {
  const html = renderPatchRecord(
    makeRecord({ diff_text: "+only line" }),
  );
  assertEqual(typeof html, "string", "must return a string");
  assertIncludes(html, "+only line", "the single diff line must be rendered");
});

// ---------------------------------------------------------------------------
// fetchPatchRecord.
// ---------------------------------------------------------------------------
await test("fetchPatchRecord GETs /api/worktree/patch/{id} and returns parsed JSON", async () => {
  const payload = { ok: true, patch_id: "wp-abc123", status: "pending" };
  stubFetch(jsonResponse(200, payload));
  const body = await fetchPatchRecord("wp-abc123");
  assertEqual(
    captured.url,
    "/api/worktree/patch/wp-abc123",
    "fetchPatchRecord must GET the record endpoint",
  );
  assert(
    !captured.init || !captured.init.method || String(captured.init.method).toUpperCase() === "GET",
    `fetchPatchRecord must use GET, got ${captured.init && captured.init.method}`,
  );
  assertEqual(body.patch_id, "wp-abc123", "must return the parsed JSON body");
  assertEqual(body.status, "pending", "must return the parsed JSON body");
});

await test("fetchPatchRecord attaches BOTH required headers", async () => {
  stubFetch(jsonResponse(200, { ok: true }));
  await fetchPatchRecord("wp-abc123");
  assertEqual(
    headerValue(captured.init, "X-Pipeline-Api-Key"),
    API_KEY,
    "X-Pipeline-Api-Key must carry window.__PIPELINE_API_KEY__",
  );
  assertEqual(
    headerValue(captured.init, "X-Pipeline-Origin"),
    "ui",
    "X-Pipeline-Origin must be exactly 'ui'",
  );
});

await test("fetchPatchRecord rejects on a non-2xx response with the status", async () => {
  stubFetch(jsonResponse(404, { detail: "no such patch" }));
  const err = await assertRejects(
    () => fetchPatchRecord("wp-missing"),
    "fetchPatchRecord must reject on 404",
  );
  assertMatch(err.message, /404/, "the rejection must carry the status code");
});

await test("fetchPatchRecord rejects on a 403 response with the status", async () => {
  stubFetch(jsonResponse(403, { detail: "forbidden" }));
  const err = await assertRejects(
    () => fetchPatchRecord("wp-abc123"),
    "fetchPatchRecord must reject on 403",
  );
  assertMatch(err.message, /403/, "the rejection must carry the status code");
});

await test("fetchPatchRecord never sends the literal string 'undefined' as the key", async () => {
  const saved = globalThis.window;
  globalThis.window = {};
  try {
    stubFetch(jsonResponse(200, { ok: true }));
    await fetchPatchRecord("wp-abc123");
    const key = headerValue(captured.init, "X-Pipeline-Api-Key");
    // Mirroring api.js: an absent key global means the header is OMITTED,
    // never sent as the literal string "undefined".
    assert(
      key === undefined || key === null || key === "",
      `an absent key global must omit the header, got ${JSON.stringify(key)}`,
    );
    assertEqual(
      headerValue(captured.init, "X-Pipeline-Origin"),
      "ui",
      "the origin header must still be sent when the key is absent",
    );
  } finally {
    globalThis.window = saved;
  }
});

// ---------------------------------------------------------------------------
// applyPatch.
// ---------------------------------------------------------------------------
await test("applyPatch POSTs /api/worktree/patch/{id}/apply and returns parsed JSON", async () => {
  const payload = { ok: true, status: "applied" };
  stubFetch(jsonResponse(200, payload));
  const body = await applyPatch("wp-abc123", "tok-secret");
  assertEqual(
    captured.url,
    "/api/worktree/patch/wp-abc123/apply",
    "applyPatch must POST the apply endpoint",
  );
  assertEqual(
    String(captured.init.method).toUpperCase(),
    "POST",
    "applyPatch must use POST",
  );
  assertEqual(body.status, "applied", "must return the parsed JSON body");
});

await test("applyPatch attaches BOTH required headers and a JSON content type", async () => {
  stubFetch(jsonResponse(200, { ok: true }));
  await applyPatch("wp-abc123", "tok-secret");
  assertEqual(
    headerValue(captured.init, "X-Pipeline-Api-Key"),
    API_KEY,
    "X-Pipeline-Api-Key must carry window.__PIPELINE_API_KEY__",
  );
  assertEqual(
    headerValue(captured.init, "X-Pipeline-Origin"),
    "ui",
    "X-Pipeline-Origin must be exactly 'ui'",
  );
  assertMatch(
    String(headerValue(captured.init, "Content-Type") || ""),
    /application\/json/i,
    "the JSON body needs Content-Type: application/json",
  );
});

await test("applyPatch sends EXACTLY {confirmation_token} — never a diff or paths", async () => {
  stubFetch(jsonResponse(200, { ok: true }));
  await applyPatch("wp-abc123", "tok-secret");
  const raw = captured.init.body;
  assertEqual(typeof raw, "string", "the body must be a JSON string");
  assertNotMatch(raw, /diff/i, "the body must never carry a diff");
  assertNotMatch(raw, /paths/i, "the body must never carry paths");
  const parsed = JSON.parse(raw);
  assertEqual(
    Object.keys(parsed).sort().join(","),
    "confirmation_token",
    "the body must have exactly one field: confirmation_token",
  );
  assertEqual(
    parsed.confirmation_token,
    "tok-secret",
    "the confirmation token must be sent verbatim",
  );
});

await test("applyPatch JSON-encodes a token containing quotes and markup", async () => {
  stubFetch(jsonResponse(200, { ok: true }));
  const token = 'a"b<c>&d';
  await applyPatch("wp-abc123", token);
  const parsed = JSON.parse(captured.init.body);
  assertEqual(parsed.confirmation_token, token, "the token must round-trip");
  assertEqual(
    Object.keys(parsed).length,
    1,
    "the body must still have exactly one field",
  );
});

await test("applyPatch rejects on a non-2xx response with the status", async () => {
  stubFetch(jsonResponse(403, { detail: "bad token" }));
  const err = await assertRejects(
    () => applyPatch("wp-abc123", "wrong"),
    "applyPatch must reject on 403",
  );
  assertMatch(err.message, /403/, "the rejection must carry the status code");
});

await test("applyPatch rejects on a 409 response with the status", async () => {
  stubFetch(jsonResponse(409, { detail: "already applied" }));
  const err = await assertRejects(
    () => applyPatch("wp-abc123", "tok-secret"),
    "applyPatch must reject on 409",
  );
  assertMatch(err.message, /409/, "the rejection must carry the status code");
});

// ---------------------------------------------------------------------------
// Summary.
// ---------------------------------------------------------------------------
console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) {
  console.error(`failed: ${failures.join(", ")}`);
  process.exit(1);
}
