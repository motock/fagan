// Tests for static/app/comms.js — incremental SSE streaming in the Comms panel.
//
// Run with:  node tests/unit/test_comms_sse_stream.mjs
//
// Written test-first (TDD) for the SSE-streaming story. Until
// static/app/comms.js exports parseSseFrames / streamCommsMessage and wires
// sendCommsMessage through them, these tests fail — that is the expected RED
// state; a later dispatch implements against them.
//
// Harness notes (mirrors tests/test_app_workspace.mjs):
//   - jsdom is not installed in this repo. comms.js renders HTML strings into
//     the existing #comms-thread / #comms-body / #comms-send / #on-air
//     elements, so a hand-rolled per-id element-stub document is enough.
//   - tests/_app_js_loader.mjs hardcodes static/app.js as its import target
//     and cannot load comms.js directly, so we bootstrap the browser globals
//     THROUGH the shared loader (it installs globalThis.window / document /
//     localStorage / fetch and keeps them set for call-time reads) and then
//     import the module under test directly with the same cache-busting
//     query the loader uses.
//   - The document stub is a process-wide singleton with per-id cached
//     elements: comms.js reads document.getElementById(...) at call time
//     against whichever window the loader last installed, so every UI test
//     resets the singleton BEFORE loading the module and then re-reads the
//     elements from it (this also works if comms.js caches element refs at
//     module top level, because the reset happens before the fresh import).
//   - fetch is a module-level `currentFetch` swapped per test; the window
//     stub's fetch delegates to it at call time, so globalThis.fetch (wired
//     by the loader) always reaches the active handler.
//
// Grading scope: THIS story only. comms.js is a shared artifact edited by
// several stories, so assertions are membership/behaviour based — never byte
// hashes, exact file contents, or exact function counts.
//
// Covered:
//   POSITIVE:
//     - parseSseFrames parses one complete frame -> {events:[{type,data}], rest:''}.
//     - Two complete frames in one buffer parse to two events, in order.
//     - A split frame is buffered (zero events, non-empty rest) and completes
//       once the remainder is concatenated and re-parsed.
//     - streamCommsMessage POSTs to /api/chat/stream (POST + X-Pipeline-Api-Key
//       + JSON body carrying the message), calls onEvent once per frame in
//       order, and resolves to the `result` frame's data.
//     - A frame split across two stream reads is buffered by streamCommsMessage.
//     - sendCommsMessage renders incrementally: the tool name is visible
//       mid-stream (live status line) while the send button is disabled, and
//       the final reply + trace chip land once `result` arrives.
//   NEGATIVE / BOUNDARY:
//     - parseSseFrames('') -> zero events, rest ''.
//     - A frame whose data: line is invalid JSON is skipped, never thrown.
//     - A <img src=x onerror=alert(1)> payload is escaped when rendered:
//       thread HTML contains &lt;img and no literal <img tag.
//     - ok:false on the stream response falls back to blocking /api/chat and
//       still appends the tower reply.
//     - A stream response without res.body (no streaming support) falls back.
//     - A stream that throws mid-read BEFORE the result event falls back.
//     - A rejected fetch still appends the catch message and re-enables the
//       button (the panel is never left permanently pending).
//     - A tool result carrying an `error` key renders the tower-denied role.
//     - #on-air loses the `live` class in the finally path.

import { readFileSync } from "node:fs";
import { loadAppInto } from "../_app_js_loader.mjs";

// Repo-root static/app/comms.js (this file lives in tests/unit/).
const COMMS_JS_URL = new URL("../../static/app/comms.js", import.meta.url);

// ---------- tiny record/assert harness (same shape as test_app_workspace.mjs) ----------

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok, detail });
  const tag = ok ? "PASS" : "FAIL";
  // eslint-disable-next-line no-console
  console.log(`${tag}  ${name}${detail ? ` — ${detail}` : ""}`);
}

// Key-order-insensitive deep equality (JSON round-trip over sorted keys).
function normalize(value) {
  if (Array.isArray(value)) return value.map(normalize);
  if (value && typeof value === "object") {
    const out = {};
    for (const k of Object.keys(value).sort()) out[k] = normalize(value[k]);
    return out;
  }
  return value;
}

function assertEqual(actual, expected, label) {
  const a = JSON.stringify(normalize(actual));
  const e = JSON.stringify(normalize(expected));
  if (a !== e) throw new Error(`${label || "values differ"}: got ${a} want ${e}`);
}

function assertTrue(cond, label) {
  if (!cond) throw new Error(label || "assertion failed");
}

// ---------- DOM stubs ----------

const noop = () => {};

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

function makeElement(id) {
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
    addEventListener: noop,
    removeEventListener: noop,
    setAttribute: noop,
    getAttribute: () => null,
    removeAttribute: noop,
    focus: noop,
    click: noop,
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
  el._innerHTML = "";
  Object.defineProperty(el, "className", {
    get() {
      return el._className || "";
    },
    set(v) {
      el._className = String(v);
      // Record as an attribute so role strings like "msg tower denied" are
      // visible to htmlOf().
      el._writes.push(`class="${String(v)}"`);
    },
  });
  Object.defineProperty(el, "innerHTML", {
    get() {
      return el._innerHTML;
    },
    set(v) {
      el._innerHTML = String(v);
      el._writes.push(String(v));
    },
  });
  Object.defineProperty(el, "textContent", {
    get() {
      return el._textContent;
    },
    set(v) {
      el._textContent = String(v);
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

// Every HTML string that ever landed on the element (final innerHTML plus
// each write/append), so "contains" assertions work regardless of whether the
// implementation renders via innerHTML +=, insertAdjacentHTML or appendChild.
function htmlOf(el) {
  return [el._innerHTML, ...el._writes, el.textContent].join("\n");
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
    fetch: (url, opts) => currentFetch(url, opts),
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
  };
  win.window = win;
  win.self = win;
  return win;
}

// ---------- fetch stubs ----------

// Swapped out per test; the window stub's fetch delegates to whatever is
// currently installed.
let currentFetch = null;

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

// A streaming Response stub whose body.getReader() yields the given SSE
// frames as separate chunks. `failAfter` makes read #failAfter+1 throw
// (simulating a mid-stream network failure); `withBody: false` simulates an
// environment without streaming support.
function sseResponse(frames, { ok = true, status = 200, withBody = true, failAfter = Infinity } = {}) {
  const encoder = new TextEncoder();
  const chunks = frames.map((f) => encoder.encode(f));
  let i = 0;
  const reader = {
    read: async () => {
      if (i >= failAfter) throw new Error("simulated mid-stream failure");
      if (i >= chunks.length) return { done: true, value: undefined };
      return { done: false, value: chunks[i++] };
    },
    cancel: async () => {},
  };
  // Best-effort json(): the last `result` frame's data. The streamed path
  // never calls json(), but preservation tests also run against the current
  // blocking implementation, which does.
  let resultData = {};
  for (const f of frames) {
    const m = /event: result\ndata: (.*)\n/.exec(f);
    if (m) {
      try {
        resultData = JSON.parse(m[1]);
      } catch {
        // leave the previous value
      }
    }
  }
  return {
    ok,
    status,
    body: withBody ? { getReader: () => reader } : undefined,
    json: async () => resultData,
    text: async () => frames.join(""),
  };
}

// ---------- module loading ----------

let bootstrapped = false;
async function bootstrapGlobals() {
  if (bootstrapped) return;
  bootstrapped = true;
  // Defensive globals some call paths may read bare (the loader only
  // propagates window/document/localStorage/fetch).
  globalThis.requestAnimationFrame = globalThis.requestAnimationFrame || (() => 0);
  currentFetch = async () => jsonResponse(200, {});
  try {
    await loadAppInto(makeWindowStub());
  } catch {
    // The full app graph's DOM wiring is out of scope here; the globals are
    // already installed and persist for call-time reads.
  }
}

// Cache-busted re-import per test, mirroring the loader's own dynamic
// import() strategy so a stateful implementation cannot leak across tests.
let importSeq = 0;
async function loadCommsModule() {
  await bootstrapGlobals();
  return import(COMMS_JS_URL.href + "?t=" + ++importSeq);
}

async function run(name, fn) {
  try {
    await fn();
    record(name, true);
  } catch (err) {
    record(name, false, err && err.message ? err.message : String(err));
  }
}

// ---------- SSE fixtures ----------
//
// Shapes mirror what the existing blocking path consumes today:
// renderToolTraceHtml expects {name, args, result} entries and the
// 'tower denied' rule keys off c.result.error, so the stream events carry
// the same shapes.

const TOOL_CALL_DATA = { name: "deploy_check", args: { path: "/tmp" } };
const TOOL_RESULT_DATA = {
  name: "deploy_check",
  args: { path: "/tmp" },
  result: { output: "ok" },
};
const FRAME_TOOL_CALL = `event: tool_call\ndata: ${JSON.stringify(TOOL_CALL_DATA)}\n\n`;
const FRAME_TOOL_RESULT = `event: tool_result\ndata: ${JSON.stringify(TOOL_RESULT_DATA)}\n\n`;
const RESULT_DATA = { reply: "Tower online.", tool_calls: [TOOL_RESULT_DATA] };
const FRAME_RESULT = `event: result\ndata: ${JSON.stringify(RESULT_DATA)}\n\n`;

// The export names this story must ADD, and the pre-existing exports that
// must survive (membership anchors — comms.js is a shared artifact, so the
// full list is deliberately not asserted beyond these names).
const NEW_EXPORTS = ["parseSseFrames", "streamCommsMessage"];
const ANCHOR_EXPORTS = [
  "sendCommsMessage",
  "resetCommsThread",
  "renderToolTraceHtml",
  "appendCommsMessage",
  "updateCommsSubtitle",
];

// ---------- exports ----------

await run("exports parseSseFrames and streamCommsMessage as functions", async () => {
  const mod = await loadCommsModule();
  for (const name of NEW_EXPORTS) {
    assertTrue(
      typeof mod[name] === "function",
      `${name} should be an exported function (got ${typeof mod[name]})`,
    );
  }
});

await run("export list keeps every pre-existing export and adds the two new names", async () => {
  const src = readFileSync(COMMS_JS_URL, "utf8");
  const names = new Set();
  // Collect names from every `export { ... }` block…
  for (const m of src.matchAll(/export\s*\{([^}]*)\}/g)) {
    for (const part of m[1].split(",")) {
      const name = part.trim().split(/\s+as\s+/).pop().trim();
      if (name) names.add(name);
    }
  }
  // …and from inline `export function` / `export const` declarations.
  for (const m of src.matchAll(/export\s+(?:async\s+)?function\s+([A-Za-z0-9_$]+)/g)) {
    names.add(m[1]);
  }
  for (const m of src.matchAll(/export\s+const\s+([A-Za-z0-9_$]+)/g)) {
    names.add(m[1]);
  }
  for (const name of NEW_EXPORTS) {
    assertTrue(names.has(name), `export list should contain ${name}`);
  }
  for (const name of ANCHOR_EXPORTS) {
    assertTrue(names.has(name), `pre-existing export ${name} must not be removed`);
  }
});

// ---------- parseSseFrames (pure) ----------

await run("parseSseFrames parses one complete frame and leaves an empty rest", async () => {
  const mod = await loadCommsModule();
  const { events, rest } = mod.parseSseFrames('event: turn\ndata: {"n":1}\n\n');
  assertEqual(events, [{ type: "turn", data: { n: 1 } }], "one turn event expected");
  assertEqual(rest, "", "a complete frame must leave no trailing text");
});

await run("parseSseFrames parses two complete frames in one buffer, in order", async () => {
  const mod = await loadCommsModule();
  const { events, rest } = mod.parseSseFrames(FRAME_TOOL_CALL + FRAME_RESULT);
  assertEqual(events.length, 2, "two events expected");
  assertEqual(events[0].type, "tool_call", "first event type");
  assertEqual(events[0].data, { tool: "deploy_check", args: { path: "/tmp" } }, "first event data");
  assertEqual(events[1].type, "result", "second event type");
  assertEqual(events[1].data, RESULT_DATA, "second event data");
  assertEqual(rest, "", "complete frames must leave no rest");
});

await run("parseSseFrames buffers a split frame and completes it on the next call", async () => {
  const mod = await loadCommsModule();
  const first = mod.parseSseFrames('event: turn\ndata: {"n"');
  assertEqual(first.events, [], "an incomplete frame must yield zero events");
  assertTrue(typeof first.rest === "string" && first.rest.length > 0, "rest must be non-empty");
  const second = mod.parseSseFrames(first.rest + ':1}\n\n');
  assertEqual(second.events, [{ type: "turn", data: { n: 1 } }], "recomposed frame must parse");
  assertEqual(second.rest, "", "recomposed frame must leave no rest");
});

await run("parseSseFrames('') returns zero events and an empty rest", async () => {
  const mod = await loadCommsModule();
  const out = mod.parseSseFrames("");
  assertEqual(out.events, [], "empty buffer must yield zero events");
  assertEqual(out.rest, "", "empty buffer must yield an empty rest");
});

await run("parseSseFrames skips a frame whose data line is invalid JSON, without throwing", async () => {
  const mod = await loadCommsModule();
  const buffer =
    "event: tool_call\ndata: {oops not json\n\n" +
    'event: result\ndata: {"ok":true}\n\n';
  let out;
  try {
    out = mod.parseSseFrames(buffer);
  } catch (err) {
    throw new Error(`parseSseFrames must never throw on bad JSON: ${err.message}`);
  }
  assertEqual(out.events.length, 1, "only the valid frame should be emitted");
  assertEqual(out.events[0].type, "result", "the valid frame's type");
  assertEqual(out.events[0].data, { ok: true }, "the valid frame's data");
});

// ---------- streamCommsMessage ----------

await run("streamCommsMessage POSTs to /api/chat/stream with the pipeline key header and JSON body", async () => {
  const mod = await loadCommsModule();
  const calls = [];
  currentFetch = async (url, opts) => {
    calls.push({ url, opts });
    return sseResponse([FRAME_RESULT]);
  };
  // The header is conditional on window.__PIPELINE_API_KEY__ today; set it so
  // the "same headers as sendCommsMessage builds" requirement is observable.
  globalThis.window.__PIPELINE_API_KEY__ = "test-key-123";
  const out = await mod.streamCommsMessage("hello tower", () => {});
  assertEqual(out, RESULT_DATA, "streamCommsMessage must resolve to the result frame's data");
  assertEqual(calls.length, 1, "exactly one fetch expected");
  const { url, opts } = calls[0];
  assertTrue(String(url).includes("/api/chat/stream"), `stream endpoint expected, got ${url}`);
  assertTrue(opts && opts.method === "POST", "the request must be a POST (EventSource cannot POST)");
  const headers = (opts && opts.headers) || {};
  const keys = Object.keys(headers);
  const keyName = keys.find((k) => k.toLowerCase() === "x-pipeline-api-key");
  assertTrue(Boolean(keyName), "X-Pipeline-Api-Key header must be sent");
  assertTrue(
    keyName && headers[keyName] === "test-key-123",
    "X-Pipeline-Api-Key must carry window.__PIPELINE_API_KEY__",
  );
  const ctName = keys.find((k) => k.toLowerCase() === "content-type");
  assertTrue(
    Boolean(ctName) && String(headers[ctName]).includes("application/json"),
    "content-type must be application/json",
  );
  const body = JSON.parse(opts.body);
  assertTrue(body && typeof body === "object", "body must be a JSON object");
  assertEqual(body.message, "hello tower", "body.message must carry the trimmed message");
  // Same body sendCommsMessage builds today: membership, not exact values.
  for (const k of ["plan_name", "workspace", "history"]) {
    assertTrue(k in body, `body.${k} must be present (same body as the blocking path)`);
  }
  assertTrue(Array.isArray(body.history), "body.history must be the commsHistory array");
});

await run("streamCommsMessage calls onEvent once per frame, in order, and returns the result data", async () => {
  const mod = await loadCommsModule();
  currentFetch = async () => sseResponse([FRAME_TOOL_CALL, FRAME_TOOL_RESULT, FRAME_RESULT]);
  const seen = [];
  const out = await mod.streamCommsMessage("hello", (e) => seen.push(e));
  assertEqual(
    seen.map((e) => e.type),
    ["tool_call", "tool_result", "result"],
    "one onEvent call per frame, in order",
  );
  assertEqual(seen[0].data, TOOL_CALL_DATA, "first event data");
  assertEqual(out, RESULT_DATA, "must resolve to the result frame's data");
});

await run("streamCommsMessage buffers a frame split across two reads", async () => {
  const mod = await loadCommsModule();
  const encoder = new TextEncoder();
  const chunks = [
    encoder.encode('event: tool_call\ndata: {"name":"deploy_check'),
    encoder.encode('"}\n\nevent: result\ndata: {"reply":"done"}\n\n'),
  ];
  let i = 0;
  currentFetch = async () => ({
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () =>
          i < chunks.length
            ? { done: false, value: chunks[i++] }
            : { done: true, value: undefined },
      }),
    },
  });
  const seen = [];
  const out = await mod.streamCommsMessage("hi", (e) => seen.push(e));
  assertEqual(
    seen.map((e) => e.type),
    ["tool_call", "result"],
    "a frame split across reads must still parse once complete",
  );
  assertEqual(out, { reply: "done" }, "the result frame's data must be returned");
});

// ---------- sendCommsMessage: incremental rendering ----------

// Flush helper: gives the implementation's pending UI work (microtasks and
// timers) a moment to settle before we snapshot the thread HTML mid-stream.
const flush = (ms = 10) => new Promise((r) => setTimeout(r, ms));

await run("sendCommsMessage renders incrementally: live tool status mid-stream, reply + trace at the end", async () => {
  const mod = await loadCommsModule();
  commsDoc.__reset();
  const thread = commsDoc.getElementById("comms-thread");
  const sendBtn = commsDoc.getElementById("comms-send");
  const onAir = commsDoc.getElementById("on-air");
  let midHtml = null;
  let midDisabled = null;
  let midLive = null;
  const encoder = new TextEncoder();
  const chunks = [FRAME_TOOL_CALL, FRAME_TOOL_RESULT, FRAME_RESULT].map((f) => encoder.encode(f));
  let i = 0;
  currentFetch = async () => ({
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () => {
          if (i === 1) {
            // Chunk 0 (tool_call) has been delivered; let pending UI work
            // settle, then snapshot the live status line.
            await flush();
            midHtml = htmlOf(thread);
            midDisabled = sendBtn.disabled;
            midLive = onAir.classList.contains("live");
          }
          return i < chunks.length
            ? { done: false, value: chunks[i++] }
            : { done: true, value: undefined };
        },
      }),
    },
  });
  await mod.sendCommsMessage("status check");
  assertTrue(typeof midHtml === "string", "the stream should have delivered more than one chunk");
  assertTrue(
    midHtml.includes("deploy_check"),
    "the tool name must appear in the thread while the call is in flight (live status line)",
  );
  assertTrue(midDisabled === true, "the send button must be disabled while the stream is in flight");
  assertEqual(midLive, true, "#on-air must carry the live class while the stream is in flight");
  const finalHtml = htmlOf(thread);
  assertTrue(finalHtml.includes("Tower online."), "the final reply text must be rendered");
  assertTrue(finalHtml.includes("trace-chip"), "the tool trace chip markup must be rendered");
  assertEqual(sendBtn.disabled, false, "the send button must be re-enabled at the end");
  assertEqual(onAir.classList.contains("live"), false, "#on-air must lose the live class at the end");
});

await run("tool_result chips are rendered through the existing renderToolTraceHtml helper", async () => {
  const mod = await loadCommsModule();
  commsDoc.__reset();
  const thread = commsDoc.getElementById("comms-thread");
  let chipHtml = null;
  const encoder = new TextEncoder();
  const chunks = [FRAME_TOOL_CALL, FRAME_TOOL_RESULT, FRAME_RESULT].map((f) => encoder.encode(f));
  let i = 0;
  currentFetch = async () => ({
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () => {
          if (i === 2) {
            // Chunks 0-1 (tool_call + tool_result) have been delivered.
            await flush();
            chipHtml = htmlOf(thread);
          }
          return i < chunks.length
            ? { done: false, value: chunks[i++] }
            : { done: true, value: undefined };
        },
      }),
    },
  });
  await mod.sendCommsMessage("chip probe");
  assertTrue(
    typeof mod.renderToolTraceHtml === "function",
    "renderToolTraceHtml must remain available on the module",
  );
  const chip = mod.renderToolTraceHtml([TOOL_RESULT_DATA]);
  assertTrue(
    typeof chip === "string" && chip.length > 0,
    "renderToolTraceHtml should return markup for the one-element call array",
  );
  assertTrue(Boolean(chipHtml), "the stream should have delivered the tool_result chunk");
  assertTrue(
    chipHtml.includes(chip),
    "on tool_result the thread must contain exactly the markup renderToolTraceHtml produces for that one call",
  );
});

await run("model-derived text is HTML-escaped when rendered (no raw <img> tag)", async () => {
  const mod = await loadCommsModule();
  commsDoc.__reset();
  const thread = commsDoc.getElementById("comms-thread");
  const payload = '<img src=x onerror=alert(1)>';
  currentFetch = async () =>
    sseResponse([`event: result\ndata: ${JSON.stringify({ reply: payload, tool_calls: [] })}\n\n`]);
  await mod.sendCommsMessage("xss probe");
  const html = htmlOf(thread);
  assertTrue(html.includes("&lt;img"), "the payload must be escaped (&lt;img expected in the HTML)");
  assertTrue(!html.includes("<img"), "no literal <img tag may be interpolated into the thread");
});

await run("commsHistory push semantics are preserved across a streamed turn", async () => {
  const mod = await loadCommsModule();
  commsDoc.__reset();
  const reply2 = { reply: "Copy that.", tool_calls: [] };
  let body2 = null;
  currentFetch = async (url, opts) => {
    body2 = JSON.parse(opts.body);
    return sseResponse([`event: result\ndata: ${JSON.stringify(reply2)}\n\n`]);
  };
  await mod.sendCommsMessage("status check");
  await mod.sendCommsMessage("second question");
  const hist = body2 && body2.history;
  assertTrue(Array.isArray(hist), "the request body must carry the commsHistory array");
  assertTrue(hist.length >= 2, "the previous turn must have been pushed to history");
  assertEqual(
    hist.slice(-2),
    [
      { role: "user", content: "status check" },
      { role: "assistant", content: "Copy that." },
    ],
    "history must end with the previous user turn then assistant turn, same shape as today",
  );
});

// ---------- sendCommsMessage: fallbacks ----------

await run("ok:false on the stream falls back to blocking /api/chat and still appends the tower reply", async () => {
  const mod = await loadCommsModule();
  commsDoc.__reset();
  const thread = commsDoc.getElementById("comms-thread");
  const calls = [];
  currentFetch = async (url, opts) => {
    calls.push({ url, opts });
    if (calls.length === 1) return { ok: false, status: 500, body: undefined };
    return jsonResponse(200, { reply: "fallback reply" });
  };
  await mod.sendCommsMessage("fallback probe");
  assertEqual(calls.length, 2, "expected one stream attempt then one blocking call");
  assertTrue(
    String(calls[0].url).includes("/api/chat/stream"),
    "the first attempt must target the stream endpoint",
  );
  assertTrue(
    String(calls[1].url).includes("/api/chat") && !String(calls[1].url).includes("/stream"),
    "the fallback must hit the blocking /api/chat endpoint",
  );
  assertTrue(
    htmlOf(thread).includes("fallback reply"),
    "the fallback reply must still be appended to the thread",
  );
  assertEqual(commsDoc.getElementById("comms-send").disabled, false, "send button must be re-enabled");
});

await run("a stream response without res.body falls back to blocking /api/chat", async () => {
  const mod = await loadCommsModule();
  commsDoc.__reset();
  const thread = commsDoc.getElementById("comms-thread");
  const calls = [];
  currentFetch = async (url, opts) => {
    calls.push({ url, opts });
    if (calls.length === 1) return { ok: true, status: 200, body: undefined };
    return jsonResponse(200, { reply: "no-body fallback reply" });
  };
  await mod.sendCommsMessage("no-body probe");
  assertEqual(calls.length, 2, "expected the stream attempt then the blocking fallback");
  assertTrue(
    String(calls[1].url).includes("/api/chat") && !String(calls[1].url).includes("/stream"),
    "the fallback must hit the blocking /api/chat endpoint",
  );
  assertTrue(
    htmlOf(thread).includes("no-body fallback reply"),
    "the fallback reply must be rendered when streaming is unsupported",
  );
});

await run("a stream that throws mid-read before the result event falls back to /api/chat", async () => {
  const mod = await loadCommsModule();
  commsDoc.__reset();
  const thread = commsDoc.getElementById("comms-thread");
  const calls = [];
  currentFetch = async (url, opts) => {
    calls.push({ url, opts });
    if (calls.length === 1) return sseResponse([FRAME_TOOL_CALL], { failAfter: 1 });
    return jsonResponse(200, { reply: "mid-stream fallback reply" });
  };
  await mod.sendCommsMessage("mid-stream probe");
  assertEqual(calls.length, 2, "expected the failed stream attempt then the blocking call");
  assertTrue(
    String(calls[1].url).includes("/api/chat") && !String(calls[1].url).includes("/stream"),
    "the fallback must hit the blocking /api/chat endpoint",
  );
  assertTrue(
    htmlOf(thread).includes("mid-stream fallback reply"),
    "the panel must never be left with a permanently pending bubble",
  );
  assertEqual(commsDoc.getElementById("comms-send").disabled, false, "send button must be re-enabled");
});

await run("a rejected fetch still appends the catch message and re-enables the button", async () => {
  const mod = await loadCommsModule();
  commsDoc.__reset();
  const thread = commsDoc.getElementById("comms-thread");
  currentFetch = async () => {
    throw new TypeError("Failed to fetch");
  };
  await mod.sendCommsMessage("network probe");
  // escapeHtml turns the apostrophe into &#39;; assert the escaped rendering.
  assertTrue(
    htmlOf(thread).includes("Couldn&#39;t reach the tower - try again."),
    "the catch message must be appended to the thread",
  );
  assertEqual(commsDoc.getElementById("comms-send").disabled, false, "send button must be re-enabled");
  assertEqual(
    commsDoc.getElementById("on-air").classList.contains("live"),
    false,
    "#on-air must lose the live class in the finally path",
  );
});

await run("a tool result carrying an error key renders the tower-denied role", async () => {
  const mod = await loadCommsModule();
  commsDoc.__reset();
  const thread = commsDoc.getElementById("comms-thread");
  const deniedCall = { name: "deploy_check", args: { path: "/tmp" } };
  const deniedResult = {
    name: "deploy_check",
    args: { path: "/tmp" },
    result: { error: "denied" },
  };
  const frames = [
    `event: tool_call\ndata: ${JSON.stringify(deniedCall)}\n\n`,
    `event: tool_result\ndata: ${JSON.stringify(deniedResult)}\n\n`,
    `event: result\ndata: ${JSON.stringify({ reply: "Denied.", tool_calls: [deniedResult] })}\n\n`,
  ];
  currentFetch = async () => sseResponse(frames);
  await mod.sendCommsMessage("denied probe");
  assertTrue(
    htmlOf(thread).includes("tower denied"),
    "any tool result with an error key must surface the tower-denied role",
  );
  assertTrue(
    htmlOf(thread).includes("Denied."),
    "the final reply must still be rendered alongside the denied role",
  );
});

// ---------- preserved internals (membership only — comms.js is shared) ----------

await run("preserved internals: reset/export helpers, trace toggle, chip wiring and catch message survive", async () => {
  const src = readFileSync(COMMS_JS_URL, "utf8");
  for (const needle of [
    "resetCommsThread",
    "exportCommsThread",
    "comms-trace-toggle",
    ".comms-chip",
    "updateCommsSubtitle",
    "Couldn't reach the tower - try again.",
  ]) {
    assertTrue(src.includes(needle), `comms.js must still reference ${needle}`);
  }
});

// ---------- summary ----------

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log(`  - ${f.name}${f.detail ? ` — ${f.detail}` : ""}`);
  process.exit(1);
}