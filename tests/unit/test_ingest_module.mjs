// Tests for static/app/ingest.js — the plan-ingest module (WAP-14).
//
// Run with:  node tests/unit/test_ingest_module.mjs
//
// Written test-first (TDD). Until static/app/ingest.js exists and exports the
// three functions below, every test here fails with the module-load error —
// that is the expected RED state; a later dispatch implements against these
// tests.
//
// Harness notes:
//   - ingest.js is a string-template, DOM-free module (like
//     static/app/patch.js), so no jsdom bootstrap is needed. We import it
//     directly with the same cache-busting query strategy
//     tests/unit/test_patch_module.mjs uses.
//   - ingestPlan reads the shared secret the same way static/app/patch.js
//     does: `window.__PIPELINE_API_KEY__`, at CALL time. The test sets
//     globalThis.window before importing so a top-level read cannot throw.
//   - global fetch is stubbed per test; the stub records (url, init).
//
// CONTRACT (this file is the spec the implementer codes against):
//   normalizePlanName(raw) -> string
//     * String(raw == null ? "" : raw).trim()
//     * null / undefined / "" / "   " / a non-string value all coerce without
//       throwing; whitespace-only becomes ""
//   ingestPlan(planName) -> Promise<parsed JSON>
//     * const name = normalizePlanName(planName); if (!name) reject with
//       new Error("plan name is required") WITHOUT calling fetch — an empty
//       name must never reach the network (it would POST to
//       /api/plans//ingest)
//     * POST `/api/plans/${encodeURIComponent(name)}/ingest`
//     * headers: X-Pipeline-Api-Key (window.__PIPELINE_API_KEY__, read at
//       CALL time, omitted entirely when the global is absent — never the
//       literal string "undefined") PLUS X-Pipeline-Origin: ui PLUS
//       Content-Type: application/json
//     * body is EXACTLY JSON.stringify({}) — never paths, never a diff, never
//       overwrite: true (overwrite:false is the server default and this
//       control must never overwrite an already-ingested plan)
//     * non-2xx rejects (never resolves) with an Error carrying .status and a
//       message that includes the server's `detail` when the body parses as
//       JSON with a string detail; otherwise the message is
//       `<url> -> <status>`. A non-JSON response body must not surface as a
//       JSON parse error.
//   renderIngestStatusHtml(outcome) -> string
//     * outcome is {ok:true, planName, result} or {ok:false, status, detail}
//     * ok:true renders a confirmation naming the plan and the counts taken
//       from the server's ingest manifest:
//       Object.keys(result.epics || {}).length epics and
//       Object.keys(result.stories || {}).length stories
//     * ok:false renders a failure line carrying the status and the detail
//     * every interpolated value is escaped with an escapeHtml helper (& < >
//       " ') defined in THIS module (copied verbatim from
//       static/app/patch.js) — never imported
//     * the outcome object is the ONLY input: unknown extra fields
//       (result.diff_text, result.repo_root, a chat-markdown blob) are never
//       rendered
//     * tolerates null / undefined / malformed outcome (neither ok:true nor
//       ok:false), returning a string and never throwing

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const MODULE_PATH = path.join(HERE, "..", "..", "static", "app", "ingest.js");

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
const EXPORTS = ["normalizePlanName", "ingestPlan", "renderIngestStatusHtml"];

let mod = null;
let loadError = null;
try {
  mod = await import(`${pathToFileURL(MODULE_PATH).href}?t=${Date.now()}`);
  for (const name of EXPORTS) {
    if (typeof mod[name] !== "function") {
      loadError = new Error(
        `static/app/ingest.js must export a function named ${name}`,
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

function normalizePlanName(raw) {
  return requireModule().normalizePlanName(raw);
}

function ingestPlan(planName) {
  return requireModule().ingestPlan(planName);
}

function renderIngestStatusHtml(outcome) {
  return requireModule().renderIngestStatusHtml(outcome);
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

function assertNotEqual(actual, unexpected, msg) {
  if (actual === unexpected) {
    throw new Error(
      `${msg}\n    expected NOT to equal: ${JSON.stringify(unexpected)}`,
    );
  }
}

function assertDeepEqual(actual, expected, msg) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    throw new Error(`${msg}\n    expected: ${e}\n    actual:   ${a}`);
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

// Mirrors the POST /api/plans/{plan_name}/ingest response body: the route
// returns the merged manifest inline (see app/dashboard.py).
function makeManifest(overrides = {}) {
  return {
    ok: true,
    manifest_path: "plans/anagram/manifest.json",
    epics: { "epic-a": {}, "epic-b": {} },
    stories: { "story-1": {}, "story-2": {}, "story-3": {} },
    repo_root: "/repo",
    role_config: {},
    ...overrides,
  };
}

// The fetch stub records every (url, init) pair; the responder is installed
// per test so each test controls status + body.
let fetchCalls = [];
let fetchResponder = null;

globalThis.fetch = async (url, init) => {
  fetchCalls.push({ url, init });
  if (!fetchResponder) throw new Error("no fetch responder installed");
  return fetchResponder(url, init);
};

// A minimal Response stand-in. json() really parses, so a non-JSON body
// throws a SyntaxError exactly like the real Response.json() would — that is
// what makes the "no JSON parse error surfaces" test meaningful.
function makeResponse(status, body) {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return JSON.parse(text);
    },
    async text() {
      return text;
    },
  };
}

function stubFetch(status, body) {
  fetchCalls = [];
  fetchResponder = () => makeResponse(status, body);
}

// Header lookup that tolerates a plain object (what patch.js builds) and a
// Headers instance, case-insensitively.
function headerValue(init, name) {
  const headers = (init && init.headers) || {};
  if (typeof headers.get === "function") return headers.get(name);
  const lower = name.toLowerCase();
  for (const key of Object.keys(headers)) {
    if (key.toLowerCase() === lower) return headers[key];
  }
  return undefined;
}

function hasHeader(init, name) {
  return headerValue(init, name) !== undefined;
}

const XSS = "<img src=x onerror=alert(1)>";
const XSS_ESCAPED = "&lt;img src=x onerror=alert(1)&gt;";

// ---------------------------------------------------------------------------
// normalizePlanName
// ---------------------------------------------------------------------------

await test("normalizePlanName trims a plan name", () => {
  assertEqual(normalizePlanName("anagram"), "anagram", "plain name is unchanged");
  assertEqual(normalizePlanName("  anagram  "), "anagram", "surrounding space is trimmed");
  assertEqual(normalizePlanName("\t anagram \n"), "anagram", "all whitespace is trimmed");
});

await test("normalizePlanName coerces null/undefined/empty/whitespace to ''", () => {
  for (const raw of [null, undefined, "", "   ", "\t\n "]) {
    const out = normalizePlanName(raw);
    assertEqual(typeof out, "string", `normalizePlanName(${JSON.stringify(raw)}) must return a string`);
    assertEqual(out, "", `normalizePlanName(${JSON.stringify(raw)}) must be ""`);
  }
});

await test("normalizePlanName coerces a non-string value without throwing", () => {
  assertEqual(normalizePlanName(42), "42", "a number is stringified");
  assertEqual(normalizePlanName(true), "true", "a boolean is stringified");
  assertEqual(typeof normalizePlanName({}), "string", "an object still yields a string");
  assertEqual(typeof normalizePlanName([]), "string", "an array still yields a string");
});

// ---------------------------------------------------------------------------
// ingestPlan — positive
// ---------------------------------------------------------------------------

await test("ingestPlan POSTs to /api/plans/anagram/ingest", async () => {
  stubFetch(200, makeManifest());
  await ingestPlan("anagram");
  assertEqual(fetchCalls.length, 1, "exactly one fetch call");
  assertEqual(fetchCalls[0].url, "/api/plans/anagram/ingest", "request URL");
  assertEqual(fetchCalls[0].init.method, "POST", "request method");
});

await test("ingestPlan trims the name before building the URL", async () => {
  stubFetch(200, makeManifest());
  await ingestPlan("  anagram  ");
  assertEqual(fetchCalls[0].url, "/api/plans/anagram/ingest", "the trimmed name is used");
});

await test("ingestPlan encodes a name that needs encoding", async () => {
  stubFetch(200, makeManifest());
  await ingestPlan("my plan/2");
  assertEqual(
    fetchCalls[0].url,
    "/api/plans/my%20plan%2F2/ingest",
    "the plan name is encodeURIComponent'd",
  );
});

await test("ingestPlan sends a body of exactly {}", async () => {
  stubFetch(200, makeManifest());
  await ingestPlan("anagram");
  assertEqual(fetchCalls[0].init.body, "{}", "body must be exactly JSON.stringify({})");
  assertNotIncludes(fetchCalls[0].init.body, "overwrite", "never sends overwrite");
  assertNotIncludes(fetchCalls[0].init.body, "paths", "never sends paths");
  assertNotIncludes(fetchCalls[0].init.body, "diff", "never sends a diff");
});

await test("ingestPlan sends X-Pipeline-Origin: ui, the shared secret and JSON content type", async () => {
  stubFetch(200, makeManifest());
  await ingestPlan("anagram");
  const init = fetchCalls[0].init;
  assertEqual(headerValue(init, "X-Pipeline-Origin"), "ui", "origin header");
  assertEqual(headerValue(init, "X-Pipeline-Api-Key"), API_KEY, "shared secret header");
  assertEqual(headerValue(init, "Content-Type"), "application/json", "content type header");
});

await test("ingestPlan reads the shared secret at CALL time", async () => {
  stubFetch(200, makeManifest());
  const saved = globalThis.window.__PIPELINE_API_KEY__;
  try {
    globalThis.window.__PIPELINE_API_KEY__ = "key-one";
    await ingestPlan("anagram");
    globalThis.window.__PIPELINE_API_KEY__ = "key-two";
    await ingestPlan("anagram");
  } finally {
    globalThis.window.__PIPELINE_API_KEY__ = saved;
  }
  assertEqual(fetchCalls.length, 2, "two fetch calls");
  assertEqual(
    headerValue(fetchCalls[0].init, "X-Pipeline-Api-Key"),
    "key-one",
    "first call carries the first key",
  );
  assertEqual(
    headerValue(fetchCalls[1].init, "X-Pipeline-Api-Key"),
    "key-two",
    "second call carries the NEW key (the secret is not cached at module level)",
  );
});

await test("ingestPlan omits the key header when the global is absent", async () => {
  stubFetch(200, makeManifest());
  const saved = globalThis.window.__PIPELINE_API_KEY__;
  try {
    delete globalThis.window.__PIPELINE_API_KEY__;
    await ingestPlan("anagram");
  } finally {
    globalThis.window.__PIPELINE_API_KEY__ = saved;
  }
  const init = fetchCalls[0].init;
  assert(!hasHeader(init, "X-Pipeline-Api-Key"), "the key header must be omitted entirely");
  assertNotEqual(
    headerValue(init, "X-Pipeline-Api-Key"),
    "undefined",
    'the literal string "undefined" must never be sent',
  );
  assertEqual(headerValue(init, "X-Pipeline-Origin"), "ui", "origin header is still sent");
});

await test("ingestPlan resolves with the parsed JSON on 200", async () => {
  const manifest = makeManifest();
  stubFetch(200, manifest);
  const result = await ingestPlan("anagram");
  assertDeepEqual(result, manifest, "the parsed JSON body is returned");
});

// ---------------------------------------------------------------------------
// ingestPlan — negative
// ---------------------------------------------------------------------------

await test("ingestPlan rejects an empty plan name without calling fetch", async () => {
  for (const raw of ["", "   ", null, undefined]) {
    stubFetch(200, makeManifest());
    const err = await assertRejects(
      () => ingestPlan(raw),
      `ingestPlan(${JSON.stringify(raw)})`,
    );
    assertEqual(
      err.message,
      "plan name is required",
      `ingestPlan(${JSON.stringify(raw)}) message`,
    );
    assertEqual(
      fetchCalls.length,
      0,
      `ingestPlan(${JSON.stringify(raw)}) must never reach the network`,
    );
  }
});

await test("ingestPlan rejects a 400 with the server detail", async () => {
  stubFetch(400, { detail: "No plan named nope" });
  const err = await assertRejects(() => ingestPlan("nope"), "400 response");
  assertEqual(err.status, 400, "error carries .status");
  assertIncludes(err.message, "No plan named nope", "message carries the server detail");
});

await test("ingestPlan rejects a 403 with .status", async () => {
  stubFetch(403, { detail: "forbidden" });
  const err = await assertRejects(() => ingestPlan("anagram"), "403 response");
  assertEqual(err.status, 403, "error carries .status");
});

await test("ingestPlan rejects a non-JSON 500 body with an Error, not a SyntaxError", async () => {
  stubFetch(500, "<html>boom</html>");
  const err = await assertRejects(() => ingestPlan("anagram"), "500 response");
  assert(!(err instanceof SyntaxError), "a non-JSON body must not surface as a SyntaxError");
  assertEqual(err.status, 500, "error carries .status");
  assertEqual(
    err.message,
    "/api/plans/anagram/ingest -> 500",
    "without a JSON detail the message is exactly `<url> -> <status>`",
  );
});

await test("ingestPlan falls back to `<url> -> <status>` when the JSON detail is not a string", async () => {
  stubFetch(422, { detail: { nested: "not a string" } });
  const err = await assertRejects(() => ingestPlan("anagram"), "422 response");
  assertEqual(err.status, 422, "error carries .status");
  assertEqual(
    err.message,
    "/api/plans/anagram/ingest -> 422",
    "a non-string detail falls back to the url/status message",
  );
});

// ---------------------------------------------------------------------------
// renderIngestStatusHtml
// ---------------------------------------------------------------------------

await test("renderIngestStatusHtml names the plan and the manifest counts", () => {
  const html = renderIngestStatusHtml({
    ok: true,
    planName: "anagram",
    result: { epics: { a: 1, b: 2 }, stories: { s1: 1, s2: 1, s3: 1 } },
  });
  assertEqual(typeof html, "string", "returns a string");
  assertIncludes(html, "anagram", "names the plan");
  assertIncludes(html, "2 epics", "counts the epics from the manifest");
  assertIncludes(html, "3 stories", "counts the stories from the manifest");
});

await test("renderIngestStatusHtml counts empty collections as zero", () => {
  const html = renderIngestStatusHtml({
    ok: true,
    planName: "anagram",
    result: { epics: {}, stories: {} },
  });
  assertIncludes(html, "0 epics", "an empty epics map counts as 0");
  assertIncludes(html, "0 stories", "an empty stories map counts as 0");
});

await test("renderIngestStatusHtml tolerates a missing result", () => {
  const html = renderIngestStatusHtml({ ok: true, planName: "anagram" });
  assertEqual(typeof html, "string", "returns a string");
  assertIncludes(html, "anagram", "still names the plan");
  assertIncludes(html, "0 epics", "a missing epics map counts as 0");
  assertIncludes(html, "0 stories", "a missing stories map counts as 0");
});

await test("renderIngestStatusHtml renders the status and detail on failure", () => {
  const html = renderIngestStatusHtml({
    ok: false,
    status: 400,
    detail: "No plan named nope",
  });
  assertEqual(typeof html, "string", "returns a string");
  assertIncludes(html, "400", "carries the status");
  assertIncludes(html, "No plan named nope", "carries the detail");
});

await test("renderIngestStatusHtml escapes the plan name", () => {
  const html = renderIngestStatusHtml({
    ok: true,
    planName: XSS,
    result: { epics: { a: 1 }, stories: { s: 1 } },
  });
  assertIncludes(html, XSS_ESCAPED, "the plan name is HTML-escaped");
  assertNotIncludes(html, XSS, "the raw tag must never reach the DOM");
});

await test("renderIngestStatusHtml escapes the detail", () => {
  const html = renderIngestStatusHtml({ ok: false, status: 400, detail: XSS });
  assertIncludes(html, XSS_ESCAPED, "the detail is HTML-escaped");
  assertNotIncludes(html, XSS, "the raw tag must never reach the DOM");
});

await test("renderIngestStatusHtml escapes & < > \" '", () => {
  const html = renderIngestStatusHtml({
    ok: false,
    status: 400,
    detail: `&<>"'`,
  });
  assertIncludes(html, "&amp;", "& is escaped");
  assertIncludes(html, "&lt;", "< is escaped");
  assertIncludes(html, "&gt;", "> is escaped");
  assertIncludes(html, "&quot;", '" is escaped');
  assertIncludes(html, "&#39;", "' is escaped");
});

await test("renderIngestStatusHtml tolerates null/undefined/malformed outcomes", () => {
  for (const outcome of [null, undefined, {}, { ok: "maybe" }, { status: 400 }, "nope", 42]) {
    let html = null;
    try {
      html = renderIngestStatusHtml(outcome);
    } catch (err) {
      throw new Error(
        `renderIngestStatusHtml(${JSON.stringify(outcome)}) threw: ${(err && err.message) || err}`,
      );
    }
    assertEqual(
      typeof html,
      "string",
      `renderIngestStatusHtml(${JSON.stringify(outcome)}) must return a string`,
    );
  }
});

await test("an ok:false outcome never renders a confirmation", () => {
  const html = renderIngestStatusHtml({
    ok: false,
    status: 599,
    detail: "FAILUREDETAIL",
  });
  assertIncludes(html, "599", "the failure line carries the status");
  assertIncludes(html, "FAILUREDETAIL", "the failure line carries the detail");
  assertNotIncludes(html, "epics", "no confirmation counts on a failure");
  assertNotIncludes(html, "stories", "no confirmation counts on a failure");
});

await test("an ok:true outcome never renders a failure line", () => {
  const html = renderIngestStatusHtml({
    ok: true,
    planName: "anagram",
    result: { epics: { a: 1, b: 2 }, stories: { s1: 1, s2: 1, s3: 1 } },
  });
  assertIncludes(html, "anagram", "the confirmation names the plan");
  assertNotIncludes(html, "FAILUREDETAIL", "no failure detail on a success");
  assertNotIncludes(html, "599", "no failure status on a success");
  assertNotMatch(html, /\b(failed|failure)\b/i, "no failure line on a success");
});

await test("renderIngestStatusHtml never renders unknown fields", () => {
  const html = renderIngestStatusHtml({
    ok: true,
    planName: "anagram",
    result: {
      epics: { a: 1 },
      stories: { s: 1 },
      diff_text: "SECRETDIFF",
      repo_root: "SECRETROOT",
      manifest_path: "SECRETMANIFEST",
    },
    diff_text: "SECRETDIFF2",
    chat_markdown: "SECRETMD",
    markdown: "SECRETMD2",
  });
  for (const secret of [
    "SECRETDIFF",
    "SECRETDIFF2",
    "SECRETROOT",
    "SECRETMANIFEST",
    "SECRETMD",
    "SECRETMD2",
  ]) {
    assertNotIncludes(html, secret, `${secret} must never be rendered`);
  }
});

// ---------------------------------------------------------------------------
// Module shape
// ---------------------------------------------------------------------------

await test("static/app/ingest.js is an ES module that does not import main.js/app.js/comms.js", () => {
  const src = readFileSync(MODULE_PATH, "utf8");
  assertMatch(src, /\bexport\b/, "ingest.js must be an ES module (it has an export)");
  for (const banned of ["main.js", "app.js", "comms.js"]) {
    const re = new RegExp(`from\\s+["'][^"']*${banned.replace(".", "\\.")}["']`);
    assertNotMatch(src, re, `ingest.js must not import ${banned}`);
  }
});

await test("static/app/ingest.js defines escapeHtml locally instead of importing it", () => {
  const src = readFileSync(MODULE_PATH, "utf8");
  assertMatch(
    src,
    /(function\s+escapeHtml\s*\(|const\s+escapeHtml\s*=)/,
    "escapeHtml must be defined in this module (copied verbatim from patch.js)",
  );
  for (const banned of ["markdown.js", "patch.js"]) {
    const re = new RegExp(`from\\s+["'][^"']*${banned.replace(".", "\\.")}["']`);
    assertNotMatch(src, re, `escapeHtml must not be imported from ${banned}`);
  }
});

await test("static/app/ingest.js exports exactly the three contract functions", () => {
  const m = requireModule();
  for (const name of EXPORTS) {
    assertEqual(typeof m[name], "function", `${name} must be exported as a function`);
  }
  const extraFunctions = Object.keys(m).filter(
    (key) => typeof m[key] === "function" && !EXPORTS.includes(key),
  );
  assertEqual(
    extraFunctions.length,
    0,
    `no extra function exports expected, found: ${extraFunctions.join(", ")}`,
  );
});

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------
console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) {
  console.error(`failed: ${failures.join(", ")}`);
  process.exit(1);
}
