// Tests for static/app/comms.js — the Comms-panel "ingest a saved plan"
// control (#ingest-plan-name / #ingest-plan-submit / #ingest-plan-status).
//
// Run with:  node tests/unit/test_ingest_ui_wiring.mjs
//
// Written test-first (TDD). Until static/app/comms.js imports the three
// helpers from ./ingest.js, looks up the three new elements at module level,
// declares `async function submitIngestPlan()` and wires the click/keydown
// listeners, these tests fail — that is the expected RED state; a later
// dispatch implements against them.
//
// Harness notes (mirrors tests/unit/test_comms_sse_stream.mjs):
//   - jsdom is not installed in this repo, so the document is a hand-rolled
//     per-id element-stub singleton (`commsDoc`). comms.js reads
//     document.getElementById(...) at module load, so the singleton is reset
//     BEFORE each fresh import and the elements are re-read from it afterwards
//     (the module-level refs and the test's refs are the same objects).
//   - Unlike the sibling suite, addEventListener is NOT a noop: every element
//     records its listeners and `click()` / `dispatch(type, event)` actually
//     invokes them, so the wiring the module attaches is exercised for real.
//     A test file that only reads files as text would not grade the wiring.
//   - globalThis.window / document / fetch are installed BEFORE the import and
//     comms.js is imported with a cache-busting query, so each test gets a
//     fresh module body against a fresh document.
//   - fetch is a recorder: every call is captured (url + opts) and delegated to
//     the per-test `currentFetch` handler, which returns a controlled status +
//     body.
//
// Grading scope: THIS story only. comms.js is a shared artifact edited by
// several stories, so assertions are membership/behaviour based — never byte
// hashes, exact file contents, or exact function counts. The three pinned
// helpers (renderToolTraceHtml / appendCommsMessage / sendCommsMessage) are
// only checked for "still a function declaration", never for their bodies.
//
// Covered:
//   POSITIVE:
//     - a click on #ingest-plan-submit POSTs exactly once to
//       /api/plans/anagram/ingest with X-Pipeline-Origin: ui and body '{}'.
//     - a name needing encoding is encoded end to end ('my plan/2').
//     - surrounding whitespace is normalized away before the request.
//     - keydown 'Enter' on the name input submits; keydown 'a' does not.
//     - the success path renders the plan name and the response's story count.
//     - a failed request leaves the view usable (a later click still submits).
//   NEGATIVE / BOUNDARY:
//     - '' and whitespace-only input perform NO fetch and write the
//       "enter a plan name" message.
//     - a 400 with {"detail":"No plan named nope"} renders that detail + status.
//     - a rejected fetch is caught (no unhandled rejection) and renders a
//       failure message.
//     - a plan name / server detail containing <img src=x onerror=alert(1)>
//       reaches the status element escaped; the raw tag never lands in
//       innerHTML.
//   STRUCTURAL (membership only):
//     - comms.js imports ingestPlan / normalizePlanName / renderIngestStatusHtml
//       from ./ingest.js.
//     - comms.js looks up the three new element ids.
//     - submitIngestPlan is declared as `async function`.
//     - the three pinned helpers stay `function` declarations.
//     - comms.js does not import ./main.js (no callback into the dashboard).

import { readFileSync } from "node:fs";

// Repo-root static/app/comms.js (this file lives in tests/unit/).
const COMMS_JS_URL = new URL("../../static/app/comms.js", import.meta.url);
const COMMS_JS_PATH = new URL("../../static/app/comms.js", import.meta.url);

// ---------- tiny record/assert harness (same shape as test_comms_sse_stream.mjs) ----------

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok, detail });
  const tag = ok ? "PASS" : "FAIL";
  // eslint-disable-next-line no-console
  console.log(`${tag}  ${name}${detail ? ` — ${detail}` : ""}`);
}

function assertTrue(cond, label) {
  if (!cond) throw new Error(label || "assertion failed");
}

function assertEqual(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${label || "values differ"}: got ${a} want ${e}`);
}

const noop = () => {};

// ---------- DOM stubs ----------

function makeClassList() {
  const set = new Set();
  return {
    add: (...cs) => {
      for (const c of cs) set.add(String(c));
    },
    remove: (...cs) => {
      for (const c of cs) set.delete(String(c));
    },
    toggle(c, force) {
      const has = set.has(c);
      const target = force === undefined ? !has : Boolean(force);
      if (target) set.add(c);
      else set.delete(c);
      return target;
    },
    contains: (c) => set.has(c),
    toString: () => [...set].join(" "),
  };
}

// Element stub. The important upgrade over the sibling suite: listeners are
// recorded and can actually be fired, so a listener comms.js attaches is
// invoked for real instead of being a noop.
function makeElement(id) {
  const listeners = new Map(); // type -> [fn]
  const el = {
    id: id || "",
    style: {},
    disabled: false,
    value: "",
    checked: false,
    _textContent: "",
    children: [],
    dataset: {},
    scrollTop: 0,
    scrollHeight: 100,
    classList: makeClassList(),
    addEventListener(type, fn) {
      if (typeof fn !== "function") return;
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const arr = listeners.get(type);
      if (!arr) return;
      const i = arr.indexOf(fn);
      if (i !== -1) arr.splice(i, 1);
    },
    listenersFor(type) {
      return (listeners.get(type) || []).slice();
    },
    dispatch(type, event) {
      const ev = event || {};
      if (!ev.type) ev.type = type;
      if (typeof ev.preventDefault !== "function") ev.preventDefault = noop;
      if (typeof ev.stopPropagation !== "function") ev.stopPropagation = noop;
      if (!ev.target) ev.target = el;
      for (const fn of (listeners.get(type) || []).slice()) fn(ev);
      return ev;
    },
    dispatchEvent(ev) {
      return el.dispatch(ev && ev.type, ev);
    },
    click() {
      return el.dispatch("click", { type: "click" });
    },
    setAttribute: noop,
    getAttribute: () => null,
    removeAttribute: noop,
    focus: noop,
    scrollTo: noop,
    querySelector: () => makeElement(),
    querySelectorAll: () => [],
    insertAdjacentHTML(_pos, html) {
      el._writes.push(String(html));
    },
  };
  el.appendChild = function (child) {
    el.children.push(child);
    el._writes.push(child && typeof child.outerHTML === "string" ? child.outerHTML : "");
    return child;
  };
  el._writes = [];
  el._htmlWrites = []; // innerHTML assignments only (markup-parsed writes)
  el._textWrites = []; // textContent assignments only (never markup-parsed)
  el._innerHTML = "";
  Object.defineProperty(el, "className", {
    get() {
      return el._className || "";
    },
    set(v) {
      el._className = String(v);
      el._writes.push(`class="${String(v)}"`);
    },
  });
  Object.defineProperty(el, "innerHTML", {
    get() {
      return el._innerHTML;
    },
    set(v) {
      el._innerHTML = String(v);
      el._htmlWrites.push(String(v));
      el._writes.push(String(v));
    },
  });
  Object.defineProperty(el, "textContent", {
    get() {
      return el._textContent;
    },
    set(v) {
      el._textContent = String(v);
      el._textWrites.push(String(v));
      el._writes.push(String(v));
    },
  });
  Object.defineProperty(el, "outerHTML", {
    get() {
      return `<div class="${el._className || ""}">${el._innerHTML}</div>`;
    },
  });
  return el;
}

// Every string that ever landed on the element (final innerHTML plus each
// write/append), so "contains" assertions work regardless of whether the
// implementation renders via innerHTML, textContent or insertAdjacentHTML.
function htmlOf(el) {
  return [el._innerHTML, ...el._writes, el.textContent].join("\n");
}

// The element's final rendered markup (the last innerHTML assignment).
function finalHtml(el) {
  return el._innerHTML;
}

// Process-wide singleton document with per-id cached elements.
const commsDoc = {
  _byId: new Map(),
  __reset() {
    this._byId = new Map();
  },
  getElementById(id) {
    if (!this._byId.has(id)) this._byId.set(id, makeElement(id));
    return this._byId.get(id);
  },
  querySelector: () => makeElement(),
  querySelectorAll: () => [],
  createElement: (tag) => makeElement(tag),
  createTextNode: (t) => ({ textContent: String(t) }),
  body: makeElement("body"),
  documentElement: makeElement("html"),
  head: makeElement("head"),
  title: "",
  addEventListener: noop,
  removeEventListener: noop,
};

function makeWindowStub() {
  const win = {
    document: commsDoc,
    localStorage: {
      getItem: () => null,
      setItem: noop,
      removeItem: noop,
      clear: noop,
    },
    location: { href: "http://localhost/", pathname: "/", search: "", hash: "" },
    matchMedia: () => ({ matches: false, addListener: noop, removeListener: noop }),
    addEventListener: noop,
    removeEventListener: noop,
    requestAnimationFrame: (cb) => setTimeout(() => cb(Date.now()), 0),
    scrollTo: noop,
    TextDecoder: globalThis.TextDecoder,
    TextEncoder: globalThis.TextEncoder,
    AbortController: globalThis.AbortController,
    // static/app/state.js reads `window.state` at module load.
    state: {
      filters: {},
      sort: "updated",
      view: "board",
      search: "",
      status: "",
      backend: "",
      escalated: "",
      plans: [],
    },
    __PIPELINE_API_KEY__: "test-key",
  };
  win.window = win;
  win.self = win;
  return win;
}

// ---------- fetch recorder ----------

// Swapped out per test; the recorder delegates to whatever is installed.
let currentFetch = null;
let fetchCalls = [];

function recorderFetch(url, opts) {
  fetchCalls.push({ url, opts });
  return currentFetch(url, opts);
}

function urlsOf(calls) {
  return calls.map((c) => c.url).join(", ");
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

// ---------- module loading ----------

async function loadComms() {
  commsDoc.__reset();
  fetchCalls = [];
  const win = makeWindowStub();
  globalThis.window = win;
  globalThis.document = commsDoc;
  globalThis.localStorage = win.localStorage;
  globalThis.fetch = recorderFetch;
  win.fetch = recorderFetch;
  // Cache-busting query, exactly like the sibling suites: every test gets a
  // fresh module body evaluated against the freshly reset document.
  const mod = await import(`${COMMS_JS_URL.href}?t=${Date.now()}-${Math.random()}`);
  // populateIngestPlanOptions() fires one /api/ingestable-plans fetch at module
  // load (9b4e26f3). Drain it and drop it from the recorder so the per-test
  // assertions below only see fetches the test itself triggers.
  await settle();
  const moduleLoadCalls = fetchCalls.filter((c) =>
    String(c.url).includes("/api/ingestable-plans"));
  fetchCalls = fetchCalls.filter((c) =>
    !String(c.url).includes("/api/ingestable-plans"));
  return { mod, moduleLoadCalls };
}

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

// Let the click handler's promise chain (ingestPlan -> res.json -> render)
// drain, plus the macrotask Node needs to emit unhandledRejection.
async function settle() {
  for (let i = 0; i < 6; i++) await flush();
}

// Unhandled rejections are recorded (comms.js installs its own swallow guard,
// but every listener still fires, so a leaked rejection is still visible here).
const unhandled = [];
process.on("unhandledRejection", (reason) => {
  unhandled.push(reason);
});

// ---------- tests ----------

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

const INGEST_URL = "/api/plans/anagram/ingest";
const XSS = "<img src=x onerror=alert(1)>";

// Default handler for any fetch issued during module load itself. Since the
// ingest plan picker (9b4e26f3) comms.js now calls fetchIngestablePlans() once
// at import time; without this default the module-load fetch would reject and
// pollute the per-test fetch-call counts recorded by the recorder.
currentFetch = async (url) => {
  if (String(url).includes("/api/ingestable-plans")) {
    return jsonResponse(200, { plans: ["anagram"] });
  }
  return jsonResponse(200, {});
};

test("a click on #ingest-plan-submit POSTs the plan name exactly once", async () => {
  await loadComms();
  currentFetch = async () =>
    jsonResponse(200, { plan: "anagram", epics: { e1: {} }, stories: { s1: {}, s2: {} } });
  const nameEl = document.getElementById("ingest-plan-name");
  const submitEl = document.getElementById("ingest-plan-submit");
  assertTrue(
    submitEl.listenersFor("click").length >= 1,
    "the submit control has a click listener attached by comms.js",
  );
  nameEl.value = "anagram";
  submitEl.click();
  await settle();
  assertEqual(fetchCalls.length, 1, `fetch call count (saw: ${urlsOf(fetchCalls)})`);
  const call = fetchCalls[0];
  assertEqual(call.url, INGEST_URL, "request url");
  assertEqual(call.opts && call.opts.method, "POST", "request method");
  assertEqual(
    call.opts && call.opts.headers && call.opts.headers["X-Pipeline-Origin"],
    "ui",
    "X-Pipeline-Origin header",
  );
  assertEqual(call.opts && call.opts.body, "{}", "request body");
});

test("a name needing encoding is encoded end to end", async () => {
  await loadComms();
  currentFetch = async () => jsonResponse(200, {});
  document.getElementById("ingest-plan-name").value = "my plan/2";
  document.getElementById("ingest-plan-submit").click();
  await settle();
  assertEqual(fetchCalls.length, 1, `fetch call count (saw: ${urlsOf(fetchCalls)})`);
  assertEqual(fetchCalls[0].url, "/api/plans/my%20plan%2F2/ingest", "encoded request url");
});

test("surrounding whitespace is normalized away before the request", async () => {
  await loadComms();
  currentFetch = async () => jsonResponse(200, {});
  document.getElementById("ingest-plan-name").value = "  anagram  ";
  document.getElementById("ingest-plan-submit").click();
  await settle();
  assertEqual(fetchCalls.length, 1, `fetch call count (saw: ${urlsOf(fetchCalls)})`);
  assertEqual(fetchCalls[0].url, INGEST_URL, "trimmed request url");
});

test("keydown Enter on the name input submits", async () => {
  await loadComms();
  currentFetch = async () => jsonResponse(200, {});
  const nameEl = document.getElementById("ingest-plan-name");
  assertTrue(
    nameEl.listenersFor("keydown").length >= 1,
    "the name input has a keydown listener attached by comms.js",
  );
  nameEl.value = "anagram";
  nameEl.dispatch("keydown", { key: "Enter" });
  await settle();
  assertEqual(fetchCalls.length, 1, `fetch call count (saw: ${urlsOf(fetchCalls)})`);
  assertEqual(fetchCalls[0].url, INGEST_URL, "request url");
});

test("keydown with a non-Enter key does not submit", async () => {
  await loadComms();
  currentFetch = async () => jsonResponse(200, {});
  const nameEl = document.getElementById("ingest-plan-name");
  nameEl.value = "anagram";
  nameEl.dispatch("keydown", { key: "a" });
  await settle();
  assertEqual(fetchCalls.length, 0, `no fetch for key 'a' (saw: ${urlsOf(fetchCalls)})`);
});

test("an empty input performs no fetch and writes a message", async () => {
  await loadComms();
  currentFetch = async () => jsonResponse(200, {});
  const statusEl = document.getElementById("ingest-plan-status");
  document.getElementById("ingest-plan-name").value = "";
  document.getElementById("ingest-plan-submit").click();
  await settle();
  assertEqual(fetchCalls.length, 0, `no fetch for empty input (saw: ${urlsOf(fetchCalls)})`);
  assertTrue(
    /enter a plan name/i.test(htmlOf(statusEl)),
    `status carries the 'enter a plan name' message (saw: ${JSON.stringify(htmlOf(statusEl))})`,
  );
});

test("a whitespace-only input performs no fetch and writes a message", async () => {
  await loadComms();
  currentFetch = async () => jsonResponse(200, {});
  const statusEl = document.getElementById("ingest-plan-status");
  document.getElementById("ingest-plan-name").value = "   \t\n ";
  document.getElementById("ingest-plan-submit").click();
  await settle();
  assertEqual(fetchCalls.length, 0, `no fetch for whitespace input (saw: ${urlsOf(fetchCalls)})`);
  assertTrue(
    /enter a plan name/i.test(htmlOf(statusEl)),
    `status carries the 'enter a plan name' message (saw: ${JSON.stringify(htmlOf(statusEl))})`,
  );
});

test("a 400 renders the server detail and the status", async () => {
  await loadComms();
  currentFetch = async () => jsonResponse(400, { detail: "No plan named nope" });
  const statusEl = document.getElementById("ingest-plan-status");
  document.getElementById("ingest-plan-name").value = "nope";
  document.getElementById("ingest-plan-submit").click();
  await settle();
  assertEqual(fetchCalls.length, 1, `fetch call count (saw: ${urlsOf(fetchCalls)})`);
  const html = htmlOf(statusEl);
  assertTrue(html.includes("No plan named nope"), `detail rendered (saw: ${JSON.stringify(html)})`);
  assertTrue(html.includes("400"), `status code rendered (saw: ${JSON.stringify(html)})`);
});

test("a rejected fetch is caught and leaves the view usable", async () => {
  await loadComms();
  currentFetch = async () => {
    throw new Error("network down");
  };
  const statusEl = document.getElementById("ingest-plan-status");
  const nameEl = document.getElementById("ingest-plan-name");
  const submitEl = document.getElementById("ingest-plan-submit");
  unhandled.length = 0;
  nameEl.value = "anagram";
  submitEl.click();
  await settle();
  assertEqual(unhandled.length, 0, "no unhandled rejection escaped the click handler");
  const html = htmlOf(statusEl);
  assertTrue(
    /fail|error|network|reach|unavailable|couldn'?t|could not|unable|try again|went wrong/i.test(html),
    `status carries a failure message (saw: ${JSON.stringify(html)})`,
  );
  // The view is still usable: a later click submits again.
  currentFetch = async () => jsonResponse(200, { plan: "anagram", stories: { s1: {} } });
  submitEl.click();
  await settle();
  assertEqual(fetchCalls.length, 2, `second click still submits (saw: ${urlsOf(fetchCalls)})`);
  assertTrue(
    htmlOf(statusEl).includes("anagram"),
    `status recovers after the failure (saw: ${JSON.stringify(htmlOf(statusEl))})`,
  );
});

test("the success path renders the plan name and the story count", async () => {
  await loadComms();
  currentFetch = async () =>
    jsonResponse(200, {
      plan: "anagram",
      epics: { e1: {}, e2: {} },
      stories: { s1: {}, s2: {}, s3: {}, s4: {}, s5: {}, s6: {}, s7: {} },
    });
  const statusEl = document.getElementById("ingest-plan-status");
  document.getElementById("ingest-plan-name").value = "anagram";
  document.getElementById("ingest-plan-submit").click();
  await settle();
  const html = htmlOf(statusEl);
  assertTrue(html.includes("anagram"), `plan name rendered (saw: ${JSON.stringify(html)})`);
  assertTrue(/\b7\b/.test(html), `story count rendered (saw: ${JSON.stringify(html)})`);
});

test("a plan name containing markup reaches the status element escaped", async () => {
  await loadComms();
  currentFetch = async () => jsonResponse(200, { plan: XSS, stories: { s1: {} } });
  const statusEl = document.getElementById("ingest-plan-status");
  document.getElementById("ingest-plan-name").value = XSS;
  document.getElementById("ingest-plan-submit").click();
  await settle();
  const html = finalHtml(statusEl);
  assertTrue(html.includes("&lt;img"), `escaped plan name rendered (saw: ${JSON.stringify(html)})`);
  assertTrue(!html.includes("<img"), `raw tag absent from the rendered html (saw: ${JSON.stringify(html)})`);
  for (const write of statusEl._htmlWrites) {
    assertTrue(
      !write.includes("<img"),
      `no innerHTML write carries the raw tag (saw: ${JSON.stringify(write)})`,
    );
  }
});

test("a server detail containing markup reaches the status element escaped", async () => {
  await loadComms();
  currentFetch = async () => jsonResponse(400, { detail: XSS });
  const statusEl = document.getElementById("ingest-plan-status");
  document.getElementById("ingest-plan-name").value = "anagram";
  document.getElementById("ingest-plan-submit").click();
  await settle();
  const html = finalHtml(statusEl);
  assertTrue(html.includes("&lt;img"), `escaped detail rendered (saw: ${JSON.stringify(html)})`);
  assertTrue(!html.includes("<img"), `raw tag absent from the rendered html (saw: ${JSON.stringify(html)})`);
  for (const write of statusEl._htmlWrites) {
    assertTrue(
      !write.includes("<img"),
      `no innerHTML write carries the raw tag (saw: ${JSON.stringify(write)})`,
    );
  }
});

test("a pending message is written before the request resolves", async () => {
  await loadComms();
  const statusEl = document.getElementById("ingest-plan-status");
  let writesAtFetch = -1;
  currentFetch = async () => {
    // Snapshot the status element at the moment the request is issued: the
    // pending message must already be on screen (the success/failure render
    // only happens after the await).
    writesAtFetch = statusEl._writes.length;
    return jsonResponse(200, { plan: "anagram", stories: { s1: {} } });
  };
  document.getElementById("ingest-plan-name").value = "anagram";
  document.getElementById("ingest-plan-submit").click();
  await settle();
  assertTrue(writesAtFetch >= 1, "a pending message is written before awaiting ingestPlan");
});

// ---------- structural (membership only) ----------

const commsSrc = readFileSync(COMMS_JS_PATH, "utf8");

test("comms.js imports the three ingest helpers from ./ingest.js", () => {
  const m = /import\s*\{([^}]*)\}\s*from\s*["']\.\/ingest\.js["']/.exec(commsSrc);
  assertTrue(!!m, "comms.js has a named import from ./ingest.js");
  const names = m[1]
    .split(",")
    .map((s) => s.trim().split(/\s+as\s+/)[0].trim())
    .filter(Boolean);
  for (const wanted of ["ingestPlan", "normalizePlanName", "renderIngestStatusHtml"]) {
    assertTrue(names.includes(wanted), `./ingest.js import includes ${wanted} (saw: ${names.join(", ")})`);
  }
});

test("comms.js looks up the three new element ids", () => {
  for (const id of ["ingest-plan-name", "ingest-plan-submit", "ingest-plan-status"]) {
    assertTrue(
      commsSrc.includes(`getElementById('${id}')`) || commsSrc.includes(`getElementById("${id}")`),
      `comms.js looks up #${id}`,
    );
  }
});

test("comms.js calls all three ingest helpers", () => {
  for (const fn of ["normalizePlanName", "ingestPlan", "renderIngestStatusHtml"]) {
    assertTrue(
      new RegExp(`${fn}\\s*\\(`).test(commsSrc),
      `comms.js calls ${fn}(...)`,
    );
  }
});

test("submitIngestPlan is declared as an async function", () => {
  assertTrue(
    /async\s+function\s+submitIngestPlan\s*\(/.test(commsSrc),
    "comms.js declares `async function submitIngestPlan()`",
  );
});

test("the pinned helpers stay function declarations", () => {
  for (const fn of ["renderToolTraceHtml", "appendCommsMessage", "sendCommsMessage"]) {
    assertTrue(
      new RegExp(`function\\s+${fn}\\s*\\(`).test(commsSrc),
      `${fn} is still a function declaration`,
    );
    assertTrue(
      !new RegExp(`(const|let|var)\\s+${fn}\\s*=`).test(commsSrc),
      `${fn} was not rewritten as an arrow/expression`,
    );
  }
});

test("comms.js does not import ./main.js", () => {
  assertTrue(
    !/from\s*["'][^"']*main\.js["']/.test(commsSrc),
    "comms.js must not import main.js (no callback into the dashboard)",
  );
});

// ---------- runner ----------

async function main() {
  for (const t of tests) {
    try {
      await t.fn();
      record(t.name, true);
    } catch (err) {
      record(t.name, false, err && err.message ? err.message : String(err));
    }
  }
  const failed = results.filter((r) => !r.ok);
  // eslint-disable-next-line no-console
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  if (failed.length) {
    // eslint-disable-next-line no-console
    console.log("FAILED:");
    for (const f of failed) {
      // eslint-disable-next-line no-console
      console.log(`  - ${f.name}: ${f.detail}`);
    }
    process.exitCode = 1;
  }
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error(err);
  process.exitCode = 1;
});
