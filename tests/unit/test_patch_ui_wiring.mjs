// Tests for the WAP-14 patch review/apply UI wiring.
//
// Run with:  node tests/unit/test_patch_ui_wiring.mjs
//
// Written test-first (TDD). Until the implementation lands these tests fail:
//   - static/app/render/notifications.js renders no "Review patch" control,
//   - static/app/main.js neither imports ./patch.js nor exports
//     initPatchReview / openPatchReview,
//   - static/index.html loads no /app/patch.js module script.
// That is the expected RED state; a later dispatch implements against them.
//
// Harness notes (mirrors tests/unit/test_plan_list_scoping.mjs and
// tests/unit/test_comms_sse_stream.mjs):
//   - jsdom is not installed in this repo, so a hand-rolled cached-element
//     document stub is used. It implements the small DOM surface main.js /
//     notifications.js actually touch: getElementById (per-id cached),
//     createElement, appendChild / remove, classList, addEventListener, a
//     recursive querySelector / querySelectorAll over the child tree,
//     textContent / innerHTML, plus a tiny flat HTML parser so that a
//     querySelector(".patch-apply") against an innerHTML-rendered panel
//     returns a real (listener-recording) element.
//   - tests/_app_js_loader.mjs hardcodes static/app.js as its import target,
//     so the browser globals are bootstrapped THROUGH the shared loader (it
//     installs globalThis.window / document / localStorage / fetch and keeps
//     them set for call-time reads), and the module namespace it returns is
//     app.js's re-export of main.js.
//   - Module identity: main.js is imported by the loader at its plain URL, so
//     its child specifiers (./render/notifications.js, ./patch.js) resolve to
//     the SAME plain URLs this file imports directly.
//
// Grading scope: THIS story only. main.js / notifications.js / index.html are
// shared, cumulative artifacts, so assertions are membership / behaviour
// based — never byte hashes, exact file contents, or exact totals.
//
// ---------------------------------------------------------------------------
// CONTRACT (this file is the spec the implementer codes against)
// ---------------------------------------------------------------------------
// static/app/render/notifications.js
//   renderNotifications(records) -> html string
//     * a record whose payload.kind === "patch_proposed" AND whose
//       payload.patch_id is a non-empty string renders, IN ADDITION to its
//       normal row, a control
//         <button class="notif-review-patch" data-patch-id="<patch_id>">Review patch</button>
//     * every other record renders exactly as before: no "Review patch" text
//       and no data-patch-id attribute.
//     * tolerates null / empty / malformed records (returns a string, no throw)
//
// static/app/main.js
//   * imports { fetchPatchRecord, applyPatch, renderPatchRecord } from
//     "./patch.js" (the WAP-13 module) — no parallel fetch path, and main.js
//     must NOT contain the "/api/worktree/patch" URL itself.
//   * exports initPatchReview({ fetchPatchRecord, applyPatch, renderPatchRecord })
//     (same injection pattern as initStoryModal / initPlanDetail) and calls it
//     once at init with the real imports.
//   * exports openPatchReview(patchId) -> Promise
//       - calls fetchPatchRecord(patchId)
//       - on success renders renderPatchRecord(record) into the patch panel
//         body (element id "patch-modal-body", created dynamically if absent)
//         and appends an Apply control
//           <button class="patch-apply">Apply patch</button>
//       - on failure renders an inline error (the rejection message) and NO
//         diff / no renderPatchRecord output
//   * attaches a delegated click listener on document:
//       - a click on .notif-review-patch (or [data-patch-id]) -> openPatchReview(id)
//       - a click on .patch-apply -> applyPatch(record.patch_id,
//         record.confirmation_token) using the SERVER record fetched by
//         openPatchReview (exactly two arguments)
//       - apply success -> panel shows the applied paths
//       - apply refusal -> panel shows the rejection message / detail
//
// static/index.html
//   * adds a <script type="module" src="/app/patch.js"></script> next to the
//     existing <script type="module" src="/app.js"></script> (no other change)

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { loadAppInto } from "../_app_js_loader.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.join(HERE, "..", "..");
const INDEX_HTML = path.join(REPO, "static", "index.html");
const MAIN_JS = path.join(REPO, "static", "app", "main.js");
const NOTIFICATIONS_JS = path.join(REPO, "static", "app", "render", "notifications.js");
const PATCH_JS = path.join(REPO, "static", "app", "patch.js");

// ---------------------------------------------------------------------------
// Tiny test runner
// ---------------------------------------------------------------------------
let passed = 0;
let failed = 0;
const failures = [];

async function run(name, fn) {
  try {
    await fn();
    passed++;
    console.log("ok - " + name);
  } catch (err) {
    failed++;
    failures.push([name, err]);
    console.error("not ok - " + name);
    console.error("    " + ((err && err.stack) || err));
  }
}

function assertTrue(cond, msg) {
  if (!cond) throw new Error(msg || "expected a truthy value");
}
function assertEqual(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error(
      `${msg || "values differ"}: got ${JSON.stringify(actual)} want ${JSON.stringify(expected)}`,
    );
  }
}
function assertIncludes(haystack, needle, msg) {
  const h = String(haystack);
  if (!h.includes(needle)) {
    throw new Error(
      `${msg || "missing substring"}: ${JSON.stringify(needle)} not found in ${JSON.stringify(h.slice(0, 600))}`,
    );
  }
}
function assertNotIncludes(haystack, needle, msg) {
  const h = String(haystack);
  if (h.includes(needle)) {
    throw new Error(`${msg || "unexpected substring"}: ${JSON.stringify(needle)} was found`);
  }
}

// ---------------------------------------------------------------------------
// DOM stubs
// ---------------------------------------------------------------------------
const noop = () => {};

function matches(el, sel) {
  const s = String(sel).trim();
  if (!s) return false;
  if (s.startsWith("[") && s.endsWith("]")) {
    return el.getAttribute(s.slice(1, -1)) !== null;
  }
  if (s.startsWith(".")) {
    return el._className.split(/\s+/).includes(s.slice(1));
  }
  return el.tagName === s.toUpperCase();
}

// Minimal flat HTML parser: for every `<tag ...>` in the string, create a child
// element carrying its class / id / data-* attributes. This is enough for
// querySelector(".patch-apply") / querySelector(".notif-review-patch") to
// return a real element that records listeners, so a direct-listener
// implementation is exercised as well as a delegated one.
function parseHtmlInto(el, html) {
  el.children = [];
  const tagRe = /<([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>/g;
  let m;
  while ((m = tagRe.exec(html)) !== null) {
    const child = makeElement(m[1]);
    const attrs = m[2];
    const classM = /\bclass="([^"]*)"/.exec(attrs);
    if (classM) child._className = classM[1];
    const idM = /\bid="([^"]*)"/.exec(attrs);
    if (idM) child.id = idM[1];
    const dataRe = /\bdata-([a-z0-9-]+)="([^"]*)"/g;
    let dm;
    while ((dm = dataRe.exec(attrs)) !== null) {
      child.setAttribute("data-" + dm[1], dm[2]);
    }
    el.children.push(child);
    child.parentNode = el;
  }
}

function makeStyle() {
  const props = Object.create(null);
  return {
    setProperty(k, v) {
      props[k] = String(v);
    },
    getPropertyValue(k) {
      return props[k] || "";
    },
    removeProperty(k) {
      delete props[k];
    },
  };
}

function makeElement(tag) {
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    nodeType: 1,
    children: [],
    parentNode: null,
    style: makeStyle(),
    dataset: {},
    _attrs: Object.create(null),
    _className: "",
    _innerHTML: "",
    _textContent: "",
    _listeners: Object.create(null),
    value: "",
    checked: false,
    type: "",
    title: "",
    id: "",
    offsetWidth: 0,
    scrollTop: 0,
    scrollHeight: 0,
    hidden: false,
  };

  el.classList = {
    add(c) {
      const set = new Set(el._className.split(/\s+/).filter(Boolean));
      set.add(c);
      el._className = [...set].join(" ");
    },
    remove(c) {
      const set = new Set(el._className.split(/\s+/).filter(Boolean));
      set.delete(c);
      el._className = [...set].join(" ");
    },
    contains(c) {
      return el._className.split(/\s+/).includes(c);
    },
    toggle(c, force) {
      const has = el.classList.contains(c);
      const want = force === undefined ? !has : !!force;
      if (want && !has) el.classList.add(c);
      if (!want && has) el.classList.remove(c);
      return want;
    },
  };

  el.addEventListener = (type, fn) => {
    (el._listeners[type] || (el._listeners[type] = [])).push(fn);
  };
  el.removeEventListener = (type, fn) => {
    const arr = el._listeners[type];
    if (!arr) return;
    const i = arr.indexOf(fn);
    if (i !== -1) arr.splice(i, 1);
  };
  el.setAttribute = (k, v) => {
    const key = String(k);
    el._attrs[key] = String(v);
    if (key.startsWith("data-")) {
      const camel = key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      el.dataset[camel] = String(v);
    }
  };
  el.getAttribute = (k) =>
    Object.prototype.hasOwnProperty.call(el._attrs, k) ? el._attrs[k] : null;
  el.removeAttribute = (k) => {
    delete el._attrs[k];
  };
  el.appendChild = (child) => {
    if (child.parentNode) child.remove();
    el.children.push(child);
    child.parentNode = el;
    return child;
  };
  el.insertBefore = (child, ref) => {
    if (child.parentNode) child.remove();
    const i = ref ? el.children.indexOf(ref) : -1;
    if (i === -1) el.children.push(child);
    else el.children.splice(i, 0, child);
    child.parentNode = el;
    return child;
  };
  el.removeChild = (child) => {
    const i = el.children.indexOf(child);
    if (i !== -1) el.children.splice(i, 1);
    child.parentNode = null;
    return child;
  };
  el.remove = () => {
    if (el.parentNode) el.parentNode.removeChild(el);
  };
  el.querySelectorAll = (sel) => {
    const out = [];
    const walk = (node) => {
      for (const c of node.children || []) {
        if (matches(c, sel)) out.push(c);
        walk(c);
      }
    };
    walk(el);
    return out;
  };
  el.querySelector = (sel) => {
    const found = el.querySelectorAll(sel);
    return found.length ? found[0] : null;
  };
  el.closest = (sel) => {
    let n = el;
    while (n) {
      if (matches(n, sel)) return n;
      n = n.parentNode;
    }
    return null;
  };
  el.focus = noop;
  el.click = () => dispatchClick(el);
  el.append = (...nodes) => {
    for (const n of nodes) el.appendChild(n);
  };
  el.prepend = (...nodes) => {
    for (const n of nodes.reverse()) el.insertBefore(n, el.children[0]);
  };
  el.replaceChildren = (...nodes) => {
    el.children = [];
    el._innerHTML = "";
    el._textContent = "";
    el.append(...nodes);
  };
  el.insertAdjacentHTML = (_pos, html) => {
    el._innerHTML += String(html);
    parseHtmlInto(el, el._innerHTML);
  };
  el.getBoundingClientRect = () => ({ top: 0, left: 0, width: 0, height: 0 });
  el.contains = (other) => {
    let n = other;
    while (n) {
      if (n === el) return true;
      n = n.parentNode;
    }
    return false;
  };

  Object.defineProperty(el, "className", {
    get() {
      return el._className;
    },
    set(v) {
      el._className = String(v);
    },
  });
  Object.defineProperty(el, "innerHTML", {
    get() {
      return el._innerHTML;
    },
    set(v) {
      el._innerHTML = String(v);
      parseHtmlInto(el, el._innerHTML);
    },
  });
  Object.defineProperty(el, "textContent", {
    get() {
      if (el.children.length) return el.children.map((c) => c.textContent).join("");
      return el._textContent;
    },
    set(v) {
      el._textContent = String(v);
      el.children = [];
    },
  });
  return el;
}

// Process-wide singleton document with per-id cached elements, so
// document.getElementById("patch-modal-body") always returns the SAME node
// the implementation rendered into.
const doc = {
  _byId: new Map(),
  _listeners: Object.create(null),
  getElementById(id) {
    if (!this._byId.has(id)) this._byId.set(id, makeElement("div"));
    return this._byId.get(id);
  },
  querySelector(sel) {
    const s = String(sel).trim();
    if (s.startsWith("#")) {
      const id = s.slice(1);
      return this._byId.has(id) ? this._byId.get(id) : null;
    }
    for (const root of [this.documentElement, this.body]) {
      const hit = root.querySelector(s);
      if (hit) return hit;
    }
    return null;
  },
  querySelectorAll(sel) {
    const out = [];
    for (const root of [this.documentElement, this.body]) {
      out.push(...root.querySelectorAll(sel));
    }
    return out;
  },
  createElement: (tag) => makeElement(tag),
  createTextNode: (t) => ({ nodeType: 3, textContent: String(t) }),
  body: makeElement("body"),
  documentElement: makeElement("html"),
  head: makeElement("head"),
  title: "",
  hidden: false,
  addEventListener(type, fn) {
    (this._listeners[type] || (this._listeners[type] = [])).push(fn);
  },
  removeEventListener(type, fn) {
    const arr = this._listeners[type];
    if (!arr) return;
    const i = arr.indexOf(fn);
    if (i !== -1) arr.splice(i, 1);
  },
};

// Dispatch a click on `target`, invoking every click listener on the target
// and each of its ancestors, then any document-level listener. This makes the
// test pass whether the implementation delegates on document, on a container,
// or attaches directly to the rendered control.
function dispatchClick(target) {
  const event = {
    type: "click",
    target,
    currentTarget: null,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
    stopPropagation() {
      this._stopped = true;
    },
  };
  const visited = new Set();
  const fire = (node) => {
    if (!node || visited.has(node)) return;
    visited.add(node);
    const arr = node._listeners && node._listeners.click;
    if (arr) {
      for (const fn of arr.slice()) fn(event);
    }
  };
  let node = target;
  while (node) {
    fire(node);
    if (event._stopped) return event;
    node = node.parentNode;
  }
  // A detached panel (never appended to body) still bubbles through the
  // document-level delegation points an implementation may have chosen.
  fire(doc.body);
  fire(doc.documentElement);
  if (event._stopped) return event;
  fire(doc);
  return event;
}

function makeWindowStub() {
  const win = {
    fetch: (url, opts) => currentFetch(url, opts),
    document: doc,
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
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
  };
  win.window = win;
  win.self = win;
  return win;
}

// ---------------------------------------------------------------------------
// fetch stub (benign: main.js's top-level refresh must not throw)
// ---------------------------------------------------------------------------
let currentFetch = null;

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function installFetch() {
  currentFetch = async () => jsonResponse(200, {});
}

// ---------------------------------------------------------------------------
// bootstrap
// ---------------------------------------------------------------------------
let appMod = null;
let notifMod = null;
let realPatch = null;
let initError = null;

async function flush() {
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
}

async function bootstrap() {
  if (appMod) return;
  installFetch();
  appMod = await loadAppInto(makeWindowStub());
  if (typeof appMod.stopPolling === "function") appMod.stopPolling();
  // Default collaborators: the REAL renderPatchRecord (so the panel's
  // path/count/expiry assertions exercise the WAP-13 renderer) and inert
  // fetch/apply stubs. Tests swap these via inject()/injectRaw().
  const realPatchMod = await import(pathToFileURL(PATCH_JS).href);
  realPatch = realPatchMod;
  activeSpies.fetch = async () => {
    throw new Error("no fetch spy installed");
  };
  activeSpies.render = (rec) => realPatchMod.renderPatchRecord(rec);
  activeSpies.apply = async () => ({ ok: true, applied: [] });
  // Register the patch wiring exactly once with the stable forwarders (see
  // activeSpies above) so a listener registered inside initPatchReview is
  // never stacked by later tests. A missing export must NOT abort bootstrap:
  // the notification-rendering tests below have to run and fail on their own
  // merits rather than on this harness error.
  try {
    requireFn("initPatchReview")({
      fetchPatchRecord: stableFetch,
      applyPatch: stableApply,
      renderPatchRecord: stableRender,
    });
  } catch (err) {
    initError = err;
  }
  notifMod = await import(pathToFileURL(NOTIFICATIONS_JS).href);
  await flush();
}

function requireFn(name) {
  const fn = appMod && appMod[name];
  if (typeof fn !== "function") {
    throw new Error(
      `static/app/main.js must export a function named ${name} ` +
        `(re-exported through static/app.js)`,
    );
  }
  return fn;
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
const RENDER_MARKER = '<div class="patch-record-marker">RENDERED-RECORD-MARKER</div>';

const PATCH_NOTIF = {
  severity: "info",
  message: "patch proposed for W1-01",
  ts: "2024-01-01T00:00:00Z",
  story_key: "K",
  payload: { kind: "patch_proposed", patch_id: "wp-x", paths: ["src/a.py"], diff_hash: "abc" },
};

const PLAIN_NOTIF = {
  severity: "info",
  message: "hello world",
  ts: "2024-01-01T00:00:00Z",
  story_key: "K",
};

const RECORD = {
  ok: true,
  patch_id: "wp-x",
  plan_name: "P",
  story_key: "K",
  paths: ["src/a.py", "src/b.py"],
  added_lines: 42,
  diff_text: "@@ -1 +1 @@\n-a\n+b",
  status: "pending",
  created_at: "2024-01-01T00:00:00+00:00",
  expires_at: "2030-01-01T00:00:00+00:00",
  confirmation_token: "tok-123",
};

function makeSpies({ record = RECORD, fetchError = null, applyResult = null, applyError = null } = {}) {
  const calls = { fetch: [], apply: [], render: [] };
  const spyFetch = async (patchId) => {
    calls.fetch.push([patchId]);
    if (fetchError) throw fetchError;
    return record;
  };
  const spyRender = (rec) => {
    calls.render.push([rec]);
    return RENDER_MARKER;
  };
  const spyApply = async (patchId, token) => {
    calls.apply.push([patchId, token]);
    if (applyError) throw applyError;
    return applyResult || { ok: true, applied: [] };
  };
  return { calls, spyFetch, spyRender, spyApply };
}

// main.js calls initPatchReview() once at init with the REAL imports, and the
// implementation may register its delegated click listener inside that call.
// Calling initPatchReview() again per test would then stack a second listener
// and double every recorded call, so init is called exactly ONCE (in
// bootstrap) with these stable forwarders; each test only swaps the active spy
// set they delegate to.
const activeSpies = { fetch: null, render: null, apply: null };
const stableFetch = (patchId) => activeSpies.fetch(patchId);
const stableRender = (rec) => activeSpies.render(rec);
const stableApply = (patchId, token) => activeSpies.apply(patchId, token);

function inject(spies) {
  activeSpies.fetch = spies.spyFetch;
  activeSpies.render = spies.spyRender;
  activeSpies.apply = spies.spyApply;
}

// Swap individual collaborators (e.g. the real renderPatchRecord) without
// re-running initPatchReview, which would stack a second click listener.
function injectRaw({ fetch, render, apply } = {}) {
  if (fetch) activeSpies.fetch = fetch;
  if (render) activeSpies.render = render;
  if (apply) activeSpies.apply = apply;
}

// Every recorded call must have exactly `expected` as its argument list.
function assertAllCalls(calls, expected, label) {
  assertTrue(calls.length >= 1, `${label} must be called at least once`);
  for (const call of calls) {
    assertEqual(
      JSON.stringify(call),
      JSON.stringify(expected),
      `${label} must be called with exactly ${JSON.stringify(expected)}`,
    );
  }
}

// Every text-ish string that ever landed in a candidate panel container.
// Includes both innerHTML and textContent so an implementation that builds a
// control with createElement + textContent is seen as well as one that
// interpolates an HTML string.
function collectHtml(el) {
  let out = (el._innerHTML || "") + " " + (el._textContent || "") + " " + (el._className || "");
  for (const c of el.children || []) out += " " + collectHtml(c);
  return out;
}

// Scan the WHOLE stubbed tree (documentElement, body, and every element ever
// handed out by getElementById) so the assertion does not depend on which
// container id the implementation chose to mount the panel into.
function allRoots() {
  const roots = [doc.documentElement, doc.body];
  for (const el of doc._byId.values()) roots.push(el);
  return roots;
}

function panelHtml() {
  let out = "";
  for (const root of allRoots()) out += " " + collectHtml(root);
  return out;
}

function resetPanel() {
  for (const root of allRoots()) {
    root.innerHTML = "";
    root._textContent = "";
  }
  doc.body.innerHTML = "";
  doc.body._textContent = "";
}

function makeReviewButton(patchId) {
  const btn = doc.createElement("button");
  btn.className = "notif-review-patch";
  btn.setAttribute("data-patch-id", patchId);
  btn.textContent = "Review patch";
  return btn;
}

function makeApplyButton() {
  const btn = doc.createElement("button");
  btn.className = "patch-apply";
  btn.textContent = "Apply patch";
  return btn;
}

// Find the first element matching `sel` anywhere in the stubbed tree.
function findInTree(sel) {
  for (const root of allRoots()) {
    const hit = root.querySelector(sel);
    if (hit) return hit;
  }
  return null;
}

// Find the element the implementation mounted the panel into: the one whose
// own innerHTML carries renderPatchRecord's output.
function findPanelElement() {
  for (const root of allRoots()) {
    const stack = [root];
    while (stack.length) {
      const el = stack.pop();
      if ((el._innerHTML || "").includes(RENDER_MARKER)) return el;
      for (const c of el.children || []) stack.push(c);
    }
  }
  return null;
}

// Drive the user flow the way a human does: render the notification, click
// its Review patch control, and let the delegated handler open the panel.
// Falls back to a synthesized control so the panel tests do not depend on the
// notifications.js change landing first.
async function clickReviewPatch(patchId) {
  const container = doc.getElementById("plan-detail");
  doc.body.appendChild(container);
  container.innerHTML = notifMod.renderNotifications([
    {
      severity: "info",
      message: "patch proposed",
      ts: "2024-01-01T00:00:00Z",
      story_key: "K",
      payload: { kind: "patch_proposed", patch_id: patchId },
    },
  ]);
  let btn = container.querySelector(".notif-review-patch");
  if (!btn) {
    btn = makeReviewButton(patchId);
    container.appendChild(btn);
  }
  dispatchClick(btn);
  await flush();
}

// Click the Apply control wherever the implementation mounted it.
async function clickApply() {
  let btn = findInTree(".patch-apply");
  if (!btn) {
    btn = makeApplyButton();
    doc.getElementById("patch-modal-body").appendChild(btn);
  }
  dispatchClick(btn);
  await flush();
}

// ---------------------------------------------------------------------------
// 1. static wiring: index.html loads the WAP-13 patch module
// ---------------------------------------------------------------------------
await run("index.html loads static/app/patch.js as a module script", async () => {
  const html = readFileSync(INDEX_HTML, "utf8");
  assertTrue(
    /<script[^>]*\bsrc="[^"]*patch\.js"[^>]*>/.test(html),
    "index.html must add a <script ... src=...patch.js...> tag",
  );
  assertTrue(
    /<script[^>]*\btype="module"[^>]*\bsrc="[^"]*patch\.js"[^>]*>/.test(html) ||
      /<script[^>]*\bsrc="[^"]*patch\.js"[^>]*\btype="module"[^>]*>/.test(html),
    "the patch.js script tag must be type=\"module\"",
  );
  // The existing app.js module script must survive (membership, not exact).
  assertTrue(
    /<script[^>]*\bsrc="[^"]*app\.js"[^>]*>/.test(html),
    "the existing app.js module script tag must still be present",
  );
  // ...and the new tag must sit NEXT TO it, not somewhere unrelated.
  const lines = html.split("\n");
  const appIdx = lines.findIndex((l) => /<script[^>]*\bsrc="[^"]*app\.js"/.test(l));
  const patchIdx = lines.findIndex((l) => /<script[^>]*\bsrc="[^"]*patch\.js"/.test(l));
  assertTrue(appIdx !== -1 && patchIdx !== -1, "both script tags must be on their own line");
  assertTrue(
    Math.abs(appIdx - patchIdx) <= 2,
    "the patch.js script tag must be added next to the existing app.js one",
  );
});

// ---------------------------------------------------------------------------
// 2. static wiring: main.js imports the WAP-13 module and exports the wiring
// ---------------------------------------------------------------------------
await run("main.js imports ./patch.js and exports initPatchReview/openPatchReview", async () => {
  const src = readFileSync(MAIN_JS, "utf8");
  const importStmt = /import\s*\{([^}]*)\}\s*from\s*["']\.\/patch\.js["']/.exec(src);
  assertTrue(
    importStmt !== null,
    'main.js must import { ... } from "./patch.js" (the WAP-13 module)',
  );
  for (const name of ["fetchPatchRecord", "applyPatch", "renderPatchRecord"]) {
    assertTrue(
      new RegExp(`\\b${name}\\b`).test(importStmt[1]),
      `main.js must import ${name} from ./patch.js`,
    );
  }
  assertTrue(
    /export\s*\{[^}]*\binitPatchReview\b/.test(src) ||
      /export\s+(?:async\s+)?function\s+initPatchReview\b/.test(src),
    "main.js must export initPatchReview",
  );
  assertTrue(
    /export\s*\{[^}]*\bopenPatchReview\b/.test(src) ||
      /export\s+(?:async\s+)?function\s+openPatchReview\b/.test(src),
    "main.js must export openPatchReview",
  );
});

await run("main.js builds no parallel patch fetch path (no /api/worktree/patch URL)", async () => {
  const src = readFileSync(MAIN_JS, "utf8");
  assertTrue(
    !/fetch\s*\(\s*[`"'][^`"']*\/api\/worktree\/patch/.test(src),
    "all patch fetches must go through static/app/patch.js, not a parallel fetch() in main.js",
  );
});

// ---------------------------------------------------------------------------
// 3. notifications.js: patch_proposed renders a Review patch control
// ---------------------------------------------------------------------------
await run("patch_proposed notification renders a Review patch control", async () => {
  await bootstrap();
  const html = notifMod.renderNotifications([PATCH_NOTIF]);
  assertIncludes(html, "Review patch", "the patch_proposed row must offer a Review patch action");
  assertIncludes(
    html,
    'data-patch-id="wp-x"',
    "the Review patch control must carry the payload's patch_id",
  );
  assertIncludes(
    html,
    "notif-review-patch",
    "the Review patch control must carry the notif-review-patch class",
  );
  // Normal rendering is preserved alongside the new control.
  assertIncludes(html, "patch proposed for W1-01", "the notification message must still render");
  assertIncludes(html, "log-line", "the normal notification row markup must be unchanged");
});

await run("a plain notification renders WITHOUT a Review patch control", async () => {
  await bootstrap();
  const html = notifMod.renderNotifications([PLAIN_NOTIF]);
  assertNotIncludes(html, "Review patch", "a plain notification must not offer Review patch");
  assertNotIncludes(html, "data-patch-id", "a plain notification must not carry a patch id");
  assertIncludes(html, "hello world", "the plain notification message must still render");
  assertIncludes(html, "log-line", "the normal notification row markup must be unchanged");
  // Existing behaviour is pinned exactly: a non-patch_proposed record's row
  // markup must be byte-identical to what it rendered before this story.
  assertIncludes(
    html,
    '<div class="log-line" data-dedup-key="2024-01-01T00:00:00Z-hello world"> ' +
      '<span class="badge" style="--badge-color: var(--c-unknown)">info</span> ' +
      '<span class="mono">K</span> hello world 2024-01-01T00:00:00Z </div>',
    "a plain notification's row markup must be unchanged",
  );
});

await run("boundary: malformed / empty notification records render no control and never throw", async () => {
  await bootstrap();
  const cases = [
    { severity: "info", message: "no payload", ts: "t" },
    { severity: "info", message: "wrong kind", ts: "t", payload: { kind: "other", patch_id: "wp-x" } },
    { severity: "info", message: "no patch id", ts: "t", payload: { kind: "patch_proposed" } },
    { severity: "info", message: "empty patch id", ts: "t", payload: { kind: "patch_proposed", patch_id: "" } },
    { severity: "info", message: "null payload", ts: "t", payload: null },
  ];
  for (const rec of cases) {
    const html = notifMod.renderNotifications([rec]);
    assertEqual(typeof html, "string", "renderNotifications must return a string");
    assertNotIncludes(html, "Review patch", `no control for ${JSON.stringify(rec.payload)}`);
    assertNotIncludes(html, "data-patch-id", `no patch id for ${JSON.stringify(rec.payload)}`);
  }
  assertEqual(typeof notifMod.renderNotifications([]), "string", "empty list must return a string");
  assertEqual(typeof notifMod.renderNotifications(null), "string", "null must return a string");
});

// ---------------------------------------------------------------------------
// 4. clicking Review patch calls fetchPatchRecord(patch_id) and mounts the panel
// ---------------------------------------------------------------------------
await run("clicking Review patch calls fetchPatchRecord with the payload patch_id", async () => {
  await bootstrap();
  resetPanel();
  const spies = makeSpies();
  inject(spies);

  await clickReviewPatch("wp-x");

  assertAllCalls(spies.calls.fetch, ["wp-x"], "fetchPatchRecord");
});

await run("the mounted panel's HTML comes from renderPatchRecord's output", async () => {
  await bootstrap();
  resetPanel();
  const spies = makeSpies();
  inject(spies);

  await requireFn("openPatchReview")("wp-x");
  await flush();

  assertAllCalls(spies.calls.render, [RECORD], "renderPatchRecord");
  assertIncludes(
    panelHtml(),
    RENDER_MARKER,
    "the panel must be mounted with renderPatchRecord's output",
  );
  assertIncludes(panelHtml(), "Apply patch", "the panel must offer an Apply patch control");
  assertIncludes(panelHtml(), "patch-apply", "the Apply control must carry the patch-apply class");
});

await run("the panel shows the path list, added-lines count and expiry (real renderPatchRecord)", async () => {
  await bootstrap();
  resetPanel();
  injectRaw({ fetch: async () => RECORD, render: realPatch.renderPatchRecord });

  await requireFn("openPatchReview")("wp-x");
  await flush();

  const html = panelHtml();
  assertIncludes(html, "src/a.py", "the panel must show the first path");
  assertIncludes(html, "src/b.py", "the panel must show the second path");
  assertIncludes(html, "42", "the panel must show the added-lines count");
  assertIncludes(html, "2030-01-01", "the panel must show the expiry");
});

// ---------------------------------------------------------------------------
// 5. the Apply control calls applyPatch(patch_id, confirmation_token)
// ---------------------------------------------------------------------------
await run("Apply calls applyPatch with the patch_id and the record's confirmation_token only", async () => {
  await bootstrap();
  resetPanel();
  const spies = makeSpies({ applyResult: { ok: true, applied: ["applied/one.py", "applied/two.py"] } });
  inject(spies);

  await requireFn("openPatchReview")("wp-x");
  await flush();
  await clickApply();

  assertAllCalls(spies.calls.apply, ["wp-x", "tok-123"], "applyPatch");
  for (const call of spies.calls.apply) {
    assertTrue(
      !call.includes(RECORD.diff_text),
      "applyPatch must never be handed the diff text",
    );
  }
});

await run("apply success shows the applied paths", async () => {
  await bootstrap();
  resetPanel();
  const spies = makeSpies({ applyResult: { ok: true, applied: ["applied/one.py", "applied/two.py"] } });
  inject(spies);

  await requireFn("openPatchReview")("wp-x");
  await flush();
  await clickApply();

  const html = panelHtml();
  assertIncludes(html, "applied/one.py", "the panel must show the first applied path");
  assertIncludes(html, "applied/two.py", "the panel must show the second applied path");
});

await run("apply refusal shows the returned error detail", async () => {
  await bootstrap();
  resetPanel();
  const refusal = new Error("plan busy");
  refusal.status = 409;
  refusal.detail = "plan busy";
  const spies = makeSpies({ applyError: refusal });
  inject(spies);

  await requireFn("openPatchReview")("wp-x");
  await flush();
  await clickApply();

  assertIncludes(
    panelHtml(),
    "plan busy",
    "a refused apply must surface the returned error detail",
  );
});

// ---------------------------------------------------------------------------
// 6. fetch failure renders an inline error, not a partial diff
// ---------------------------------------------------------------------------
await run("a fetch failure renders an inline error and no partial diff", async () => {
  await bootstrap();
  resetPanel();
  const gone = new Error("no such patch");
  gone.status = 404;
  const spies = makeSpies({ fetchError: gone });
  inject(spies);

  await requireFn("openPatchReview")("wp-gone");
  await flush();

  const html = panelHtml();
  assertTrue(html.trim().length > 0, "the panel must be mounted even on failure");
  assertIncludes(
    html,
    "no such patch",
    "the panel must render the fetch error inline (the rejection message)",
  );
  assertNotIncludes(html, RENDER_MARKER, "a failed fetch must not render a partial diff");
  assertNotIncludes(html, "patch-diff", "a failed fetch must not render a diff block");
  assertEqual(spies.calls.render.length, 0, "renderPatchRecord must not run on a failed fetch");
  assertEqual(spies.calls.apply.length, 0, "applyPatch must not run after a failed fetch");
});

await run("boundary: a non-Error rejection still renders an inline error", async () => {
  await bootstrap();
  resetPanel();
  const spies = makeSpies({ fetchError: "boom" });
  inject(spies);

  await requireFn("openPatchReview")("wp-x");
  await flush();

  const html = panelHtml();
  assertTrue(html.trim().length > 0, "the panel must be mounted even on a non-Error rejection");
  assertNotIncludes(html, RENDER_MARKER, "a failed fetch must not render a partial diff");
  assertEqual(spies.calls.render.length, 0, "renderPatchRecord must not run on a failed fetch");
});

// ---------------------------------------------------------------------------
// 7. the Apply control is only reachable through the server-record flow
// ---------------------------------------------------------------------------
await run("no Apply control exists before a record is fetched", async () => {
  await bootstrap();
  resetPanel();
  assertTrue(
    typeof appMod.initPatchReview === "function",
    "main.js must export initPatchReview (the Apply flow is wired through it)",
  );
  const spies = makeSpies();
  inject(spies);

  const html = panelHtml();
  assertNotIncludes(html, "patch-apply", "no Apply control may exist before the server record loads");
  assertEqual(spies.calls.apply.length, 0, "applyPatch must not run without a fetched record");
});

await run("the panel never renders chat-reply text: only the server record", async () => {
  await bootstrap();
  resetPanel();
  const spies = makeSpies();
  inject(spies);

  const chatReply = {
    severity: "info",
    message: "CHAT-REPLY-DIFF-MARKER @@ -1 +1 @@ +evil",
    ts: "2024-01-01T00:00:00Z",
    story_key: "K",
    payload: { kind: "patch_proposed", patch_id: "wp-x" },
  };
  const container = doc.getElementById("plan-detail");
  doc.body.appendChild(container);
  container.innerHTML = notifMod.renderNotifications([chatReply]);
  let btn = container.querySelector(".notif-review-patch");
  if (!btn) {
    btn = makeReviewButton("wp-x");
    container.appendChild(btn);
  }
  dispatchClick(btn);
  await flush();

  assertAllCalls(spies.calls.fetch, ["wp-x"], "fetchPatchRecord");
  assertAllCalls(spies.calls.render, [RECORD], "renderPatchRecord");
  const panel = findPanelElement();
  assertTrue(panel !== null, "the panel must be mounted with renderPatchRecord's output");
  assertNotIncludes(
    collectHtml(panel),
    "CHAT-REPLY-DIFF-MARKER",
    "the panel must never render chat-reply text as the diff",
  );
});

// ---------------------------------------------------------------------------
// summary
// ---------------------------------------------------------------------------
console.log(`\n${passed} passed, ${failed} failed`);
if (failed) {
  for (const [name, err] of failures) {
    console.error(`FAILED: ${name}\n  ${(err && err.message) || err}`);
  }
  process.exit(1);
}
process.exit(0);
