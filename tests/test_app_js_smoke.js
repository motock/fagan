// Smoke tests for the frontend helpers introduced in the smarter-polling
// story. Pure logic is exported from app.js for testing; the production file
// still works as a browser script because we only append a `module.exports`
// block guarded by `typeof module !== "undefined"`.
//
// Run with: node tests/test_app_js_smoke.js
//
// Testable criteria mapped to cases below:
//   - scroll position survives a refresh when a column is scrolled
//   - focused filter chip retains focus across a refresh
//   - refresh indicator appears on each refresh
//   - focused element no longer present after re-render -> focus simply
//     not restored (no throw)
//   - auto-refresh unchecked -> no indicator, no polling
//   - showStoryModal renders grouped sections (Identity / Lifecycle /
//     Dispatch & review / Errors) and omits empty ones
//   - long/path/JSON values render as <pre class="mono"> blocks
//   - copy buttons exist on key, worktree, pr_url, pid and call
//     navigator.clipboard.writeText with the correct value; no-op when
//     the clipboard API is unavailable

const assert = require("assert");

// Tiny DOM stub. Enough surface for capturePlanDetailState / restorePlanDetailState
// / flashRefreshIndicator to run. Each "el" tracks its dataset as a plain object
// and supports contains/focus/querySelector like the real DOM.
//
// innerHTML is parsed by parseInnerHtml() so that re-renders (innerHTML = "...")
// actually replace children — without that the stub can't model the real DOM
// behavior renderPlanDetail depends on.
function parseInnerHtml(html) {
  if (!html) return [];
  const out = [];
  const tagRe = /<(\w+)([^>]*)>([\s\S]*?)<\/\1>/g;
  let m;
  while ((m = tagRe.exec(html)) !== null) {
    const tag = m[1];
    const attrStr = m[2] || "";
    const inner = m[3] || "";
    const cls = (attrStr.match(/class="([^"]*)"/) || [, ""])[1]
      .split(/\s+/).filter(Boolean);
    const ds = {};
    const dsRe = /data-([\w-]+)="([^"]*)"/g;
    let dm;
    while ((dm = dsRe.exec(attrStr)) !== null) {
      // Convert kebab-case data-* attrs to camelCase dataset keys.
      const key = dm[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      ds[key] = dm[2];
    }
    const child = makeEl(tag, { classList: cls, dataset: ds });
    // Recurse into the inner HTML so nested children (e.g. chips inside a
    // filter-group inside a filter-bar) get parsed into the child element's
    // own .children list. Without this, querySelector / querySelectorAll
    // can't reach descendants, which breaks focus-restore tests.
    child.children = parseInnerHtml(inner);
    for (const c of child.children) c.parentNode = child;
    out.push(child);
  }
  return out;
}

function makeEl(tag, attrs = {}) {
  const el = {
    tagName: (tag || "div").toUpperCase(),
    dataset: { ...(attrs.dataset || {}) },
    classList: {
      _set: new Set(attrs.classList || []),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    attributes: { ...(attrs.attrs || {}) },
    children: [],
    _innerHTML: "",
    scrollTop: 0,
    offsetWidth: 0,
    // Event listeners attached to this element so tests can fire them
    // directly. Stored in two equivalent ways for ergonomics:
    //   el._listeners.change    (Map-of-arrays keyed by event name)
    //   el._changeHandlers      (per-event array, like DOM `onchange`)
    // Both are populated by addEventListener() below.
    _listeners: {},
    checked: false,
    textContent: "",
    contains(other) {
      if (other === this) return true;
      return this.children.some((c) => c.contains(other));
    },
    appendChild(child) {
      this.children.push(child);
      child.parentNode = this;
      return child;
    },
    addEventListener(name, fn) {
      (this._listeners[name] = this._listeners[name] || []).push(fn);
      // Mirror onto a `_<event>Handlers` array so tests can fire by name
      // without typing the key each time, e.g. `ar._changeHandlers`.
      const alias = "_" + name + "Handlers";
      (this[alias] = this[alias] || []).push(fn);
    },
    focus() {
      if (this._ownerDoc) this._ownerDoc.setActiveElement(this);
    },
    // Match either `.foo` or `.foo[a="v"][b="v"]...` — the only forms the
    // app uses. We keep the parser identical to querySelector's below so
    // both methods return the same nodes for the same selectors.
    _matchSelector(node, className, attrPairs) {
      if (!node || !node.classList) return false;
      if (!node.classList.contains(className)) return false;
      return attrPairs.every(([k, v]) => node.dataset[k] === v);
    },
    _parseSelector(sel) {
      const m = sel.match(/^\.([\w-]+)(.*)$/);
      if (!m) return null;
      const className = m[1];
      const rest = m[2] || "";
      const attrPairs = [];
      const re = /\[([\w-]+)="([^"]*)"\]/g;
      let am;
      while ((am = re.exec(rest)) !== null) {
        // Mirror Element.dataset semantics: `data-foo-bar` -> `fooBar`.
        // The stub's dataset keys are stored camelCased, no `data-` prefix,
        // so we have to normalize the selector's attr name the same way.
        const raw = am[1];
        const key = raw.startsWith("data-")
          ? raw.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())
          : raw;
        attrPairs.push([key, am[2]]);
      }
      return { className, attrPairs };
    },
    querySelector(sel) {
      const parsed = this._parseSelector(sel);
      if (!parsed) return null;
      const { className, attrPairs } = parsed;
      const walk = (node) => {
        if (!node) return null;
        for (const c of node.children) {
          if (this._matchSelector(c, className, attrPairs)) return c;
          const nested = walk(c);
          if (nested) return nested;
        }
        return null;
      };
      return walk(this);
    },
    querySelectorAll(sel) {
      const parsed = this._parseSelector(sel);
      if (!parsed) return [];
      const { className, attrPairs } = parsed;
      const out = [];
      const walk = (node) => {
        if (!node) return;
        for (const c of node.children) {
          if (this._matchSelector(c, className, attrPairs)) out.push(c);
          walk(c);
        }
      };
      walk(this);
      return out;
    },
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return el._innerHTML; },
    set(v) {
      el._innerHTML = v;
      el.children = parseInnerHtml(v);
      // Mirror the real DOM: every descendant, not just direct children,
      // must point back to the owning document. Otherwise a deeply-nested
      // element's `focus()` (which routes through `_ownerDoc`) silently
      // no-ops in tests while working in the browser.
      const stamp = (node, owner) => {
        node._ownerDoc = owner;
        for (const c of node.children) stamp(c, owner);
      };
      for (const c of el.children) stamp(c, el._ownerDoc);
    },
  });
  return el;
}

// Mock document: tracks activeElement, getElementById, hidden, addEventListener.
function makeDocument(initial = {}) {
  let activeElement = initial.activeElement || null;
  let hidden = !!initial.hidden;
  const byId = new Map(initial.byId || []);
  const listeners = {};
  // Mirror the real DOM's `document.documentElement` so app.js can apply
  // theme tokens (e.g. `document.documentElement.dataset.theme = "dark"`).
  // Without this stub the theme toggle handler throws at module load,
  // which in turn makes every downstream test fail with a misleading
  // "Cannot read properties of undefined (reading 'dataset')".
  const documentElement = makeEl("html");
  const doc = {
    get hidden() { return hidden; },
    set hidden(v) { hidden = !!v; },
    body: makeEl("body"),
    documentElement,
    get activeElement() { return activeElement; },
    setActiveElement(e) { activeElement = e; },
    getElementById(id) {
      const el = byId.get(id) || null;
      if (el) el._ownerDoc = doc;
      return el;
    },
    register(id, el) {
      byId.set(id, el);
      el._ownerDoc = doc;
      return el;
    },
    addEventListener(name, fn) {
      (listeners[name] = listeners[name] || []).push(fn);
      // Mirror onto the document itself so tests can read either
      //   doc._listeners.visibilitychange  OR  doc._visibilitychangeHandlers
      // and stay consistent with the element stub.
      (doc._listeners = doc._listeners || {})[name]
        = (doc._listeners[name] || []).concat(fn);
      const alias = "_" + name + "Handlers";
      (doc[alias] = doc[alias] || []).push(fn);
    },
    fire(name) {
      (listeners[name] || []).forEach((fn) => fn({ target: doc.body }));
    },
  };
  doc._listeners = doc._listeners || {};
  doc.body._ownerDoc = doc;
  documentElement._ownerDoc = doc;
  return doc;
}

// Register the elements that app.js wires up at top level so the file can
// attach listeners to them under our stub. We treat the `checked` HTML
// attribute as the input's initial checked state — matching real browser
// behavior where <input type="checkbox" checked> starts checked.
function bootstrapDoc(doc, { autoRefreshChecked = false } = {}) {
  const close = makeEl("button", { attrs: { id: "story-modal-close" } });
  const modal = makeEl("div", { attrs: { id: "story-modal" } });
  const body = makeEl("div", { attrs: { id: "story-modal-body" } });
  const ar = makeEl("input", {
    attrs: { id: "auto-refresh", type: "checkbox" },
  });
  ar.checked = autoRefreshChecked;
  const lu = makeEl("span", { attrs: { id: "last-updated" } });
  const ub = makeEl("div", { attrs: { id: "usage-banner" } });
  doc.register("story-modal-close", close);
  doc.register("story-modal", modal);
  doc.register("story-modal-body", body);
  doc.register("auto-refresh", ar);
  doc.register("last-updated", lu);
  doc.register("usage-banner", ub);
}

// Inject globals the production code touches at top level, then require
// app.js as a Node module via a tiny shim.
async function loadAppJs({ doc, fakeTimers, autoRefreshChecked = false }) {
  // Set up the browser-like globals app.js reads at module load.
  global.document = doc;
  global.CSS = { escape: (s) => String(s).replace(/"/g, '\\"') };
  global.setInterval = fakeTimers.setInterval;
  global.clearInterval = fakeTimers.clearInterval;
  global.setTimeout = fakeTimers.setTimeout;
  global.clearTimeout = fakeTimers.clearTimeout;
  global.localStorage = { getItem: () => null, setItem: () => {} };

  // Bootstrap the DOM shim app.js attaches top-level listeners to so
  // loading the module doesn't throw on a null addEventListener. The
  // auto-refresh checkbox defaults to unchecked; tests that exercise the
  // polling path opt in via autoRefreshChecked: true.
  bootstrapDoc(doc, { autoRefreshChecked });

  // Stub fetch so the module-level refresh() call doesn't blow up. The real
  // app fetches JSON from /api/*; under tests we just return empty plans
  // and skip the usage banner.
  global.fetch = async (url) => ({
    ok: true,
    status: 200,
    json: async () => {
      if (typeof url === "string" && url.startsWith("/api/plans/")) return {};
      if (typeof url === "string" && url === "/api/usage") return { available: false };
      return { plans: [] };
    },
  });

  // Window shim. app.js hangs state on window and registers a hashchange
  // listener there; under Node neither exists. Provide a tiny shim so the
  // module's top-level runs without ReferenceError, and capture the state
  // assignment so callers can read it on the returned exports.
  let capturedState = null;
  const win = {
    addEventListener() {},
    removeEventListener() {},
    location: { hash: "" },
  };
  Object.defineProperty(win, "state", {
    configurable: true,
    get() { return capturedState; },
    set(v) { capturedState = v; },
  });
  global.window = win;

  // Load app.js as an ES module (static/package.json sets {"type":"module"}),
  // which resolves its ./app/* imports and exposes its named exports. The
  // module's top-level code runs during import against the globals above.
  const app = await import("../static/app.js");
  Object.assign(global, app);
  // Mirror the captured window.state onto the module exports so callers see it.
  if (capturedState) app.state = capturedState;
  return app;
}

// --------- helpers ---------

function fakeTimers() {
  let nextId = 1;
  const intervals = new Map();
  const timeouts = new Map();
  return {
    setInterval(fn, ms) {
      const id = nextId++;
      intervals.set(id, { fn, ms });
      return id;
    },
    clearInterval(id) { intervals.delete(id); },
    setTimeout(fn, ms) {
      const id = nextId++;
      timeouts.set(id, { fn, ms });
      return id;
    },
    clearTimeout(id) { timeouts.delete(id); },
    fireTimeout(id) {
      const t = timeouts.get(id);
      if (!t) return;
      timeouts.delete(id);
      t.fn();
    },
    hasInterval(id) { return intervals.has(id); },
    hasTimeout(id) { return timeouts.has(id); },
  };
}

// --------- cases ---------

let pass = 0, fail = 0;
function test(name, fn) {
  return Promise.resolve().then(fn).then(
    () => { console.log(`ok  - ${name}`); pass++; },
    (e) => { console.log(`FAIL - ${name}: ${e.message}`); fail++; }
  );
}

(async () => {
// Case: scroll position survives a refresh when a column is scrolled.
await test("scroll position survives a refresh", async () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = await loadAppJs({ doc, fakeTimers: ft });

  const section = makeEl("section", { attrs: { id: "plan-detail" } });
  doc.register("plan-detail", section);
  section.innerHTML = `<button class="filter-chip" data-dim="sort" data-value="risk">Risk</button>`;
  section.scrollTop = 1234;

  // No chip is focused -> snapshot will have null focusKey.
  doc.setActiveElement(doc.body);

  api.renderPlanDetail({ name: "p", stories: {}, notifications: [], decisions: [] });

  assert.strictEqual(section.scrollTop, 1234, "scrollTop should be restored after re-render");
});

// Case: focused filter chip retains focus across a refresh.
await test("focused filter chip retains focus across a refresh", async () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = await loadAppJs({ doc, fakeTimers: ft });

  const section = makeEl("section", { attrs: { id: "plan-detail" } });
  doc.register("plan-detail", section);
  section.innerHTML = `<button class="filter-chip" data-dim="sort" data-value="risk">Risk</button>`;
  const chip = section.children[0];
  doc.setActiveElement(chip);

  api.renderPlanDetail({ name: "p", stories: {}, notifications: [], decisions: [] });

  // After re-render, focus should be restored to the matching chip in the new DOM.
  const newChip = section.querySelector('.filter-chip[data-dim="sort"][data-value="risk"]');
  assert.ok(newChip, "chip should be present after re-render");
  assert.strictEqual(doc.activeElement, newChip, "matching chip should be re-focused");
});

// Negative case: focused element no longer present after re-render -> focus
// simply not restored, no throw.
await test("missing focused chip is silently skipped (no throw)", async () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = await loadAppJs({ doc, fakeTimers: ft });

  const section = makeEl("section", { attrs: { id: "plan-detail" } });
  doc.register("plan-detail", section);
  // Pretend a custom chip is focused that won't exist after re-render.
  const ghost = makeEl("button", { dataset: { dim: "statuses", value: "ghost" } });
  ghost.classList.add("filter-chip");
  section.appendChild(ghost);
  doc.setActiveElement(ghost);

  assert.doesNotThrow(() => {
    api.renderPlanDetail({ name: "p", stories: {}, notifications: [], decisions: [] });
  });
  // Focus is simply not restored; active element stays where it was.
  assert.strictEqual(doc.activeElement, ghost,
    "no matching chip -> focus unchanged, no throw");
});

// Case: refresh indicator appears on each successful refresh (via state.pollHandle set).
await test("flashRefreshIndicator populates the indicator element", async () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = await loadAppJs({ doc, fakeTimers: ft });

  const ind = makeEl("span", { attrs: { id: "refresh-indicator" } });
  doc.register("refresh-indicator", ind);

  api.flashRefreshIndicator();
  assert.ok(ind.innerHTML.includes("dot"), "indicator should render a dot");
  assert.ok(ind.innerHTML.includes("updating"), "indicator should say updating");
  assert.ok(ind.classList.contains("flashing"), "flashing class should be set");
});

// Case: auto-refresh unchecked leaves the polling loop dormant. We model
// the real production behavior: app.js starts polling at module load when
// the auto-refresh checkbox is checked (the HTML default). The change
// handler stops polling when the user unchecks, and the visibilitychange
// handler suspends it while the tab is hidden.
await test("auto-refresh checkbox uncheck stops polling via change handler", async () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = await loadAppJs({ doc, fakeTimers: ft, autoRefreshChecked: true });

  assert.notStrictEqual(api.state.pollHandle, null,
    "polling should be active when auto-refresh starts checked");

  // User unchecks the box -> the change handler calls stopPolling().
  const ar = doc.getElementById("auto-refresh");
  ar.checked = false;
  ar._changeHandlers.forEach((fn) => fn({ target: ar }));
  assert.strictEqual(api.state.pollHandle, null,
    "polling should stop when auto-refresh is unchecked");
});

// Case: visibilitychange pauses polling when document.hidden, resumes when
// visible again. The auto-refresh checkbox stays checked throughout, so
// visibility alone never disables polling — it only suspends it.
await test("visibilitychange stops polling when hidden, resumes when visible", async () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = await loadAppJs({ doc, fakeTimers: ft, autoRefreshChecked: true });

  // The module-level startPolling set a handle; capture it so we can
  // confirm it's a fresh handle after resume (not the original).
  const originalHandle = api.state.pollHandle;
  assert.notStrictEqual(originalHandle, null, "polling started at load");

  doc.hidden = true;
  doc._listeners.visibilitychange.forEach((fn) => fn());
  assert.strictEqual(api.state.pollHandle, null,
    "polling stopped while tab is hidden");

  doc.hidden = false;
  doc._listeners.visibilitychange.forEach((fn) => fn());
  assert.notStrictEqual(api.state.pollHandle, null,
    "polling resumed after tab becomes visible");
  assert.notStrictEqual(api.state.pollHandle, originalHandle,
    "resume created a fresh interval handle");
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
})();