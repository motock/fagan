// Tests for workspace wiring in the chat request path.
//
// Run with:  node tests/test_app_workspace_wiring.mjs
//
// Covers two files, mirroring how selectedPlan already works:
//   - static/app/state.js: a `selectedWorkspace: null` field on the state
//     singleton, declared beside `selectedPlan: null`, and cleared by
//     resetState() alongside selectedPlan.
//   - static/app/comms.js: sendCommsMessage's POST body to /api/chat gains a
//     `workspace: state.selectedWorkspace` key while plan_name, message and
//     history are sent unchanged.
//
// Structure follows tests/test_app_workspace.mjs: a tiny record/assert
// harness, per-test setup, and a final summary + exit code. jsdom is not
// installed in this repo, so a plain window stub with a defensive Proxy
// document is used.
//
// tests/_app_js_loader.mjs hardcodes static/app.js as its import target, so
// it cannot load state.js / comms.js directly. We still bootstrap the browser
// globals THROUGH the shared loader exactly like the other frontend tests
// (it installs globalThis.window / document / localStorage / fetch before its
// own import and keeps them set for call-time reads — see its comments), then
// import the modules under test directly with the same cache-busting query
// the loader uses.
//
// Module-identity note: comms.js does `import { state } from "./state.js"`.
// Node caches ESM by resolved URL and a parent's ?t= query is NOT propagated
// to child specifiers, so comms.js (imported here with ?t=N) and our direct
// plain import of state.js share ONE state singleton — mutating
// state.selectedWorkspace in a test is visible inside sendCommsMessage.
//
// Written test-first: until `selectedWorkspace: null` exists in state.js and
// `workspace: state.selectedWorkspace` exists in comms.js's chat body, the
// assertions below fail — that is the expected RED state.
//
// Testable criteria covered here:
//   - state exposes selectedWorkspace, defaulting to null, as an own property
//     on both the module export and window.state.
//   - state.js still declares selectedPlan: null, and selectedWorkspace is
//     declared beside it (adjacent lines).
//   - resetState() clears selectedWorkspace as well as selectedPlan.
//   - comms.js exports sendCommsMessage and POSTs JSON to /api/chat with a
//     Content-Type header, unchanged.
//   - the POST body includes a `workspace` key — present even when unset.
//   - the sent workspace equals state.selectedWorkspace when set, including
//     the empty-string boundary (verbatim pass-through), and is null when no
//     workspace has been selected.
//   - regression guard: plan_name, message (trimmed) and history (the live
//     transcript array, grown after each reply) are still sent unchanged.
//   - blank input still short-circuits before any fetch, and fetch failures /
//     non-2xx replies still resolve without throwing, with workspace on the
//     wire even on the failure path.

import { readFileSync } from "node:fs";
import { loadAppInto } from "./_app_js_loader.mjs";

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok, detail: ok ? "" : detail || "" });
}

function normalize(value) {
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map(normalize);
  const out = {};
  for (const k of Object.keys(value).sort()) out[k] = normalize(value[k]);
  return out;
}

function assertEqual(actual, expected, label) {
  const a = JSON.stringify(normalize(actual));
  const e = JSON.stringify(normalize(expected));
  if (a !== e) throw new Error(`${label || "values differ"}: got ${a} want ${e}`);
}

function assertTrue(cond, label) {
  if (!cond) throw new Error(label || "assertion failed");
}

// ---------- environment ----------

// Swapped out per test; the window stub's fetch delegates to whatever is
// currently installed, so globalThis.fetch (wired by the loader) always
// reaches the active handler.
let currentFetch = null;
let lastRequest = null;

function makeWindowStub() {
  const noop = () => {};
  const makeElement = () => ({
    style: {},
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    addEventListener: noop,
    removeEventListener: noop,
    appendChild: noop,
    setAttribute: noop,
    querySelector: () => makeElement(),
    querySelectorAll: () => [],
    children: [],
    innerHTML: "",
    textContent: "",
    scrollTop: 0,
    scrollHeight: 0,
    scrollTo: noop,
  });
  // Defensive document stub: any property read resolves to a no-op (or a
  // stub element for the common lookups), so unknown top-level DOM wiring
  // in the app graph cannot crash the bootstrap.
  const documentStub = new Proxy(
    {},
    {
      get(_target, prop) {
        if (
          prop === "getElementById" ||
          prop === "querySelector" ||
          prop === "createElement"
        ) {
          return () => makeElement();
        }
        if (
          prop === "querySelectorAll" ||
          prop === "getElementsByClassName" ||
          prop === "getElementsByTagName"
        ) {
          return () => [];
        }
        if (prop === "body" || prop === "documentElement" || prop === "head") {
          return makeElement();
        }
        if (prop === "title") return "";
        return noop;
      },
    },
  );
  const win = {
    fetch: (url, opts) => currentFetch(url, opts),
    document: documentStub,
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
    requestAnimationFrame: () => 0,
  };
  win.window = win;
  win.self = win;
  return win;
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

// Install a fetch handler that records the request in lastRequest, then
// either fails like a network error or replies with a canned response.
function captureFetch(status, body, { fail } = {}) {
  lastRequest = null;
  currentFetch = async (url, opts) => {
    lastRequest = { url, opts, body: JSON.parse(opts.body) };
    if (fail === "network") throw new Error("network down");
    return jsonResponse(status, body);
  };
}

// Bootstrap the browser globals (window/document/localStorage/fetch) through
// the shared loader, exactly as the other frontend tests do.
let bootstrapped = false;
async function bootstrapGlobals() {
  if (bootstrapped) return;
  bootstrapped = true;
  currentFetch = async (url, opts) => {
    if (url.includes("/api/plans")) return jsonResponse(200, { plans: [] });
    if (url.includes("/api/usage")) return jsonResponse(200, { available: false });
    return jsonResponse(200, {});
  };
  try {
    await loadAppInto(makeWindowStub());
  } catch {
    // Globals are already wired at this point; the app graph's DOM wiring
    // is out of scope for these wiring tests.
  }
}

// state.js is imported WITHOUT a cache-busting query on purpose: comms.js's
// own `import { state } from "./state.js"` resolves to the same plain URL, so
// both share one state singleton (see the module-identity note above).
async function loadStateModule() {
  await bootstrapGlobals();
  return import("../static/app/state.js");
}

// Fresh comms.js per test so the module-level commsHistory transcript starts
// empty, making the history assertions deterministic.
let commsSeq = 0;
async function loadCommsModule() {
  await bootstrapGlobals();
  return import(`../static/app/comms.js?t=${++commsSeq}`);
}

async function run(name, fn) {
  try {
    await fn();
    record(name, true);
  } catch (err) {
    record(name, false, err && err.message ? err.message : String(err));
  }
}

// ---------- state.js: selectedWorkspace field ----------

await run("state exposes selectedWorkspace defaulting to null", async () => {
  const mod = await loadStateModule();
  assertTrue(mod && typeof mod.state === "object", "state export missing");
  assertTrue(
    Object.prototype.hasOwnProperty.call(mod.state, "selectedWorkspace"),
    "state should have an own selectedWorkspace property",
  );
  assertEqual(mod.state.selectedWorkspace, null, "state.selectedWorkspace default");
  assertTrue(
    typeof globalThis.window === "object" && globalThis.window.state,
    "window.state should be set by state.js",
  );
  assertTrue(
    Object.prototype.hasOwnProperty.call(globalThis.window.state, "selectedWorkspace"),
    "window.state should expose selectedWorkspace",
  );
  assertEqual(
    globalThis.window.state.selectedWorkspace,
    null,
    "window.state.selectedWorkspace default",
  );
});

await run(
  "state.js declares selectedWorkspace: null beside selectedPlan: null",
  async () => {
    const src = readFileSync(new URL("../static/app/state.js", import.meta.url), "utf8");
    assertTrue(
      /selectedPlan:\s*null/.test(src),
      "pre-existing selectedPlan: null declaration must remain",
    );
    assertTrue(
      /selectedWorkspace:\s*null/.test(src),
      "selectedWorkspace: null declaration missing from state.js",
    );
    const lines = src.split("\n");
    const planLine = lines.findIndex((l) => l.includes("selectedPlan:"));
    const wsLine = lines.findIndex((l) => l.includes("selectedWorkspace:"));
    assertTrue(planLine !== -1, "selectedPlan declaration line not found");
    assertTrue(wsLine !== -1, "selectedWorkspace declaration line not found");
    assertTrue(
      Math.abs(planLine - wsLine) <= 4,
      `selectedWorkspace should be declared beside selectedPlan (lines ${planLine + 1} and ${wsLine + 1})`,
    );
  },
);

await run("resetState clears selectedWorkspace alongside selectedPlan", async () => {
  const mod = await loadStateModule();
  assertTrue(typeof mod.resetState === "function", "resetState export missing");
  mod.state.selectedPlan = "plan-reset";
  mod.state.selectedWorkspace = "/tmp/reset-ws";
  mod.resetState();
  assertEqual(mod.state.selectedPlan, null, "resetState should clear selectedPlan");
  assertEqual(
    mod.state.selectedWorkspace,
    null,
    "resetState should clear selectedWorkspace",
  );
});

// ---------- comms.js: workspace in the chat POST body ----------

await run("comms.js exports sendCommsMessage", async () => {
  const mod = await loadCommsModule();
  assertTrue(
    typeof mod.sendCommsMessage === "function",
    "sendCommsMessage should be an exported function",
  );
});

await run("sendCommsMessage POSTs JSON to /api/chat (request shape unchanged)", async () => {
  const mod = await loadCommsModule();
  captureFetch(200, { reply: "roger", tool_calls: [] });
  await mod.sendCommsMessage("hello");
  assertTrue(lastRequest !== null, "fetch was not called");
  assertEqual(lastRequest.url, "/api/chat", "chat endpoint");
  assertEqual(lastRequest.opts.method, "POST", "HTTP method");
  assertEqual(
    lastRequest.opts.headers["Content-Type"],
    "application/json",
    "Content-Type header",
  );
});

await run("POST body includes a workspace key even when unset", async () => {
  const mod = await loadCommsModule();
  captureFetch(200, { reply: "roger", tool_calls: [] });
  await mod.sendCommsMessage("hello");
  assertTrue(lastRequest !== null, "fetch was not called");
  assertTrue(
    Object.prototype.hasOwnProperty.call(lastRequest.body, "workspace"),
    "POST body should include a workspace key",
  );
  assertEqual(lastRequest.body.workspace, null, "workspace should be null when unset");
});

await run("sent workspace equals state.selectedWorkspace when it is set", async () => {
  const mod = await loadCommsModule();
  const stateMod = await loadStateModule();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  captureFetch(200, { reply: "roger", tool_calls: [] });
  await mod.sendCommsMessage("hello");
  assertEqual(
    lastRequest.body.workspace,
    "/tmp/alpha",
    "workspace should mirror state.selectedWorkspace",
  );
});

await run("sent workspace is null when no workspace has been selected", async () => {
  const mod = await loadCommsModule();
  const stateMod = await loadStateModule();
  stateMod.state.selectedWorkspace = null;
  captureFetch(200, { reply: "roger", tool_calls: [] });
  await mod.sendCommsMessage("hello");
  assertEqual(
    lastRequest.body.workspace,
    null,
    "workspace should be null when not selected",
  );
});

await run("workspace passes through verbatim (empty-string boundary)", async () => {
  const mod = await loadCommsModule();
  const stateMod = await loadStateModule();
  stateMod.state.selectedWorkspace = "";
  captureFetch(200, { reply: "roger", tool_calls: [] });
  await mod.sendCommsMessage("hello");
  assertEqual(
    lastRequest.body.workspace,
    "",
    "empty-string workspace should be sent verbatim, not coerced",
  );
});

await run("plan_name, message and history are still sent unchanged (regression)", async () => {
  const mod = await loadCommsModule();
  const stateMod = await loadStateModule();
  stateMod.state.selectedPlan = "plan-x";
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  captureFetch(200, { reply: "roger", tool_calls: [] });
  await mod.sendCommsMessage("  hello tower  ");
  assertEqual(lastRequest.body.plan_name, "plan-x", "plan_name");
  assertEqual(lastRequest.body.message, "hello tower", "message should be trimmed");
  assertEqual(lastRequest.body.history, [], "first send carries the empty transcript");
  await mod.sendCommsMessage("second");
  assertEqual(
    lastRequest.body.history,
    [
      { role: "user", content: "hello tower" },
      { role: "assistant", content: "roger" },
    ],
    "history should still be the live transcript",
  );
  assertEqual(lastRequest.body.message, "second", "message on the second send");
});

await run("blank input still short-circuits before any fetch", async () => {
  const mod = await loadCommsModule();
  let called = 0;
  currentFetch = async () => {
    called += 1;
    return jsonResponse(200, { reply: "x" });
  };
  await mod.sendCommsMessage("   ");
  await mod.sendCommsMessage("");
  assertEqual(called, 0, "no fetch should be made for blank input");
});

await run("network failure resolves without throwing, workspace still on the wire", async () => {
  const mod = await loadCommsModule();
  const stateMod = await loadStateModule();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  captureFetch(200, { reply: "roger" }, { fail: "network" });
  await mod.sendCommsMessage("hello"); // must not reject
  assertTrue(lastRequest !== null, "fetch was not called");
  assertEqual(
    lastRequest.body.workspace,
    "/tmp/alpha",
    "workspace should be sent even on the failure path",
  );
});

await run("non-2xx reply resolves without throwing", async () => {
  const mod = await loadCommsModule();
  captureFetch(500, { detail: "boom" });
  await mod.sendCommsMessage("hello"); // must not reject
  assertTrue(lastRequest !== null, "fetch was not called");
});

await run("comms.js chat body sends workspace: state.selectedWorkspace", async () => {
  const mod = await loadCommsModule();
  const stateMod = await loadStateModule();
  stateMod.state.selectedPlan = "plan-ws-body";
  stateMod.state.selectedWorkspace = "/tmp/ws-body";
  captureFetch(200, { reply: "roger", tool_calls: [] });
  await mod.sendCommsMessage("workspace body probe");
  assertTrue(lastRequest !== null, "fetch was not called");
  const body = lastRequest.body;
  assertEqual(body.plan_name, stateMod.state.selectedPlan, "plan_name in chat body");
  assertEqual(body.message, "workspace body probe", "message in chat body");
  assertEqual(body.workspace, stateMod.state.selectedWorkspace, "workspace in chat body");
});

// ---------- summary ----------

let failed = 0;
for (const r of results) {
  if (r.ok) {
    console.log(`  ok  - ${r.name}`);
  } else {
    failed += 1;
    console.log(`FAIL  - ${r.name}`);
    console.log(`        ${r.detail}`);
  }
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exitCode = failed ? 1 : 0;