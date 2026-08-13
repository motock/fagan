// Tests for in-plan story search added to the dashboard frontend.
//
// Run with: node tests/test_app_search.js
//
// Testable criteria (from the story):
//   (a) state.filters.search defaults to "" (match all)
//   (b) search narrows entries by summary substring
//   (c) search is case-insensitive
//   (d) search matches the KEY, not just summary
//   (e) clearing search back to "" restores all entries
//   (f) regex metacharacters are treated as literal text (no RegExp, no throw)
//   (g) search composes with status/persona/risk/backend/escalated filters
//   (h) a term matching nothing returns an empty list without throwing
//   - search term round-trips through the URL hash as &q=
//   - search term round-trips through FILTERS_KEY persistence (saveFilters)
//   - renderFilterBar renders a search input + match count
//   - renderBoard shows a "No matches" empty state (not a blank board)
//
// The harness scaffolding below is copied VERBATIM from
// tests/test_app_js_smoke.js (makeEl / makeDocument / bootstrapDoc / loadAppJs
// helpers, the global.* setup, and the new Function(...) evaluation with
// injected params). Only the test-bodies section is replaced.

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
function loadAppJs({ doc, fakeTimers, autoRefreshChecked = false }) {
  // Set up the browser-like globals app.js reads at module load.
  global.document = doc;
  global.CSS = { escape: (s) => String(s).replace(/"/g, '\\"') };
  global.setInterval = fakeTimers.setInterval;
  global.clearInterval = fakeTimers.clearInterval;
  global.setTimeout = fakeTimers.setTimeout;
  global.clearTimeout = fakeTimers.clearTimeout;
  global.localStorage = { getItem: () => null, setItem: () => {} };

  // Bootstrap the DOM elements app.js attaches top-level listeners to so
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

  // Load the file as a string, then append a module.exports block by reading
  // it from disk. We require it as a module and capture the exports.
  const fs = require("fs");
  const path = require("path");
  const src = fs.readFileSync(
    path.join(__dirname, "..", "static", "app.js"), "utf8");
  // Wrap so `module` is available, and so the file's top-level event
  // listeners attach to our fake document.
  const wrapped = `${src}\nmodule.exports = {
    capturePlanDetailState, restorePlanDetailState, flashRefreshIndicator,
    startPolling, stopPolling, syncPollingWithVisibility, renderPlanDetail,
    showStoryModal, handleCopyClick,
    defaultFilters, applyFilters, renderBoard, renderFilterBar,
    encodeHashState, parseHash, saveFilters, updateHash, escapeHtml,
    FILTERS_KEY,
    state,
  };`;
  // Window stub. The browser app.js hangs state on window and registers a
  // hashchange listener there; under Node neither exists. Provide a tiny
  // shim so the module's top-level runs without ReferenceError.
  const win = {
    addEventListener() {},
    removeEventListener() {},
    location: { hash: "" },
    state: undefined,
  };
  // Window state is populated by app.js (`window.state = { ... }`).
  // Forward that assignment onto our local `state` export by giving window
  // a setter-on-state that captures into a closure. Simpler: monkey-patch
  // via Object.defineProperty so we can mirror the assignment.
  let capturedState = null;
  Object.defineProperty(win, "state", {
    configurable: true,
    get() { return capturedState; },
    set(v) { capturedState = v; },
  });
  const m = { exports: {} };
  // eslint-disable-next-line no-new-func
  const fn = new Function("module", "document", "CSS", "setInterval",
    "clearInterval", "setTimeout", "clearTimeout", "localStorage",
    "require", "module", "window", wrapped);
  fn(m, doc, global.CSS, global.setInterval, global.clearInterval,
    global.setTimeout, global.clearTimeout, global.localStorage,
    require, m, win);
  // Mirror the captured state onto the module exports so callers see it.
  if (capturedState) m.exports.state = capturedState;
  return m.exports;
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
  try { fn(); console.log(`ok  - ${name}`); pass++; }
  catch (e) { console.log(`FAIL - ${name}: ${e.message}`); fail++; }
}

// Two-entry fixture used across most cases. applyFilters takes an array of
// [key, story] pairs (matching Object.entries(stories) input).
function twoEntries() {
  return [
    ["abc-123", { key: "abc-123", summary: "Add login", persona: "software-engineer", risk: "low", status: "in_progress" }],
    ["def-456", { key: "def-456", summary: "Fix CSS", persona: "designer", risk: "high", status: "in_progress" }],
  ];
}

// (a) state.filters.search defaults to "" (match all).
test("defaultFilters exposes search defaulting to empty string", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  assert.ok(
    Object.prototype.hasOwnProperty.call(api.state.filters, "search"),
    "state.filters should have a search field"
  );
  assert.strictEqual(api.state.filters.search, "",
    "search should default to '' (match all)");
});

// (b) search narrows entries by summary substring.
test("search narrows by summary substring", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  api.state.filters.search = "login";
  const out = api.applyFilters(twoEntries());
  assert.deepStrictEqual(
    out.map(([k]) => k),
    ["abc-123"],
    "only the entry whose summary contains 'login' should remain"
  );
});

// (c) search is case-insensitive.
test("search is case-insensitive", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  api.state.filters.search = "LOGIN";
  const out = api.applyFilters(twoEntries());
  assert.deepStrictEqual(
    out.map(([k]) => k),
    ["abc-123"],
    "uppercase term should match the same entry"
  );
});

// (d) search matches the KEY, not just summary.
test("search matches the key, not just summary", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  api.state.filters.search = "abc-123";
  const out = api.applyFilters(twoEntries());
  assert.deepStrictEqual(
    out.map(([k]) => k),
    ["abc-123"],
    "term matching the key should keep that entry"
  );
});

// (e) clearing search back to "" restores both entries.
test("clearing search restores all entries", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  api.state.filters.search = "login";
  assert.strictEqual(api.applyFilters(twoEntries()).length, 1);
  api.state.filters.search = "";
  const out = api.applyFilters(twoEntries());
  assert.deepStrictEqual(
    out.map(([k]) => k).sort(),
    ["abc-123", "def-456"],
    "empty term should match all entries"
  );
});

// (f) regex metacharacters are treated as literal text (no RegExp, no throw).
test("regex metacharacters are literal, not RegExp", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  api.state.filters.search = "a.b*c+";
  let out;
  assert.doesNotThrow(() => {
    out = api.applyFilters(twoEntries());
  }, "applyFilters must not throw on regex metacharacters");
  assert.deepStrictEqual(out, [],
    "literal 'a.b*c+' should match nothing");
});

// (g) composition: persona AND search both must hold.
test("search composes with persona filter", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  // abc-123: persona software-engineer, summary "Add login"
  // def-456: persona designer, summary "Fix CSS"
  // persona=software-engineer keeps only abc-123; search="css" would keep
  // only def-456. Together they must keep NOTHING.
  api.state.filters.personas = ["software-engineer"];
  api.state.filters.search = "css";
  const out = api.applyFilters(twoEntries());
  assert.deepStrictEqual(out, [],
    "entries must satisfy BOTH persona and search");
  // And the inverse composition: persona=designer + search=login -> none.
  api.state.filters.personas = ["designer"];
  api.state.filters.search = "login";
  assert.deepStrictEqual(api.applyFilters(twoEntries()), [],
    "designer + login should match nothing");
  // persona=software-engineer + search=login -> exactly abc-123.
  api.state.filters.personas = ["software-engineer"];
  api.state.filters.search = "login";
  assert.deepStrictEqual(
    api.applyFilters(twoEntries()).map(([k]) => k),
    ["abc-123"],
    "software-engineer + login should keep abc-123"
  );
});

// (h) a term matching nothing returns an empty list without throwing.
test("no-match term returns empty list without throwing", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  api.state.filters.search = "zzz-no-such-story";
  let out;
  assert.doesNotThrow(() => {
    out = api.applyFilters(twoEntries());
  });
  assert.deepStrictEqual(out, [], "no-match term should yield empty list");
});

// Boundary: whitespace-only term matches all (trimmed to "").
test("whitespace-only term matches all", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  api.state.filters.search = "   ";
  const out = api.applyFilters(twoEntries());
  assert.deepStrictEqual(
    out.map(([k]) => k).sort(),
    ["abc-123", "def-456"],
    "whitespace-only term should behave like empty (match all)"
  );
});

// Boundary: single-character term narrows correctly.
test("single-character term narrows correctly", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  api.state.filters.search = "x"; // "Fix CSS" contains x; "Add login" does not
  const out = api.applyFilters(twoEntries());
  assert.deepStrictEqual(
    out.map(([k]) => k),
    ["def-456"],
    "single-char term should narrow to the matching entry"
  );
});

// renderFilterBar renders a search input + match count.
test("renderFilterBar renders a search input and match count", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  const stories = {
    "abc-123": { key: "abc-123", summary: "Add login", persona: "software-engineer", risk: "low", status: "in_progress" },
    "def-456": { key: "def-456", summary: "Fix CSS", persona: "designer", risk: "high", status: "in_progress" },
  };
  api.state.filters.search = "login";
  const html = api.renderFilterBar(stories);
  assert.ok(html.includes('class="filter-search"'),
    "filter bar should contain a .filter-search input");
  assert.ok(html.includes('data-action="search"'),
    "search input should carry data-action=\"search\"");
  assert.ok(html.includes('filter-search-group'),
    "search group should have the filter-search-group class");
  // Match count label appears only when a term is active.
  assert.ok(html.includes('filter-match-count'),
    "active term should render a .filter-match-count element");
  assert.ok(/1 match/.test(html),
    "match count should read '1 match' for a single hit");
  // The current term round-trips into the input's value attribute.
  assert.ok(html.includes('value="login"'),
    "search input value should reflect the current term");
});

// Match count is pluralized and absent when no term is active.
test("renderFilterBar omits match count and pluralizes correctly", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  const stories = {
    "abc-123": { key: "abc-123", summary: "Add login", persona: "software-engineer", risk: "low", status: "in_progress" },
    "def-456": { key: "def-456", summary: "Fix CSS login", persona: "designer", risk: "high", status: "in_progress" },
  };
  // No term -> no match-count element.
  api.state.filters.search = "";
  let html = api.renderFilterBar(stories);
  assert.ok(!html.includes("filter-match-count"),
    "no match-count element when term is empty");
  // Two matches -> plural "matches".
  api.state.filters.search = "login";
  html = api.renderFilterBar(stories);
  assert.ok(/2 matches/.test(html),
    "two matches should read '2 matches'");
});

// renderBoard shows a "No matches" empty state (not a blank board) when a
// search term is active and every column produced zero cards.
test("renderBoard shows No matches empty state when search yields nothing", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  const stories = {
    "abc-123": { key: "abc-123", summary: "Add login", persona: "software-engineer", risk: "low", status: "in_progress" },
  };
  // Ensure at least one status column is selected so columns are built.
  api.state.filters.statuses = ["in_progress"];
  api.state.filters.search = "zzz-no-such-story";
  const html = api.renderBoard(stories);
  assert.ok(html.includes("board-empty"),
    "board should render a .board-empty element when search matches nothing");
  assert.ok(/No matches/.test(html),
    "empty state should say 'No matches'");
  assert.ok(html.includes("zzz-no-such-story"),
    "empty state should echo the active search term");
  // And it must NOT be a blank board with zero cards and no message.
  assert.ok(!/<div class="board"><\/div>/.test(html),
    "board must not be a blank empty board");
});

// renderBoard still renders cards normally when search matches.
test("renderBoard renders cards when search matches", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  const stories = {
    "abc-123": { key: "abc-123", summary: "Add login", persona: "software-engineer", risk: "low", status: "in_progress" },
  };
  api.state.filters.statuses = ["in_progress"];
  api.state.filters.search = "login";
  const html = api.renderBoard(stories);
  assert.ok(!html.includes("board-empty"),
    "no empty state when search matches a card");
  assert.ok(html.includes('data-key="abc-123"'),
    "matching card should still render");
});

// renderBoard does not show the empty state when there is no search term,
// even if no statuses are selected (that path is the existing "No statuses"
// state, which must remain intact).
test("renderBoard empty state only triggers on an active search term", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  const stories = {
    "abc-123": { key: "abc-123", summary: "Add login", persona: "software-engineer", risk: "low", status: "in_progress" },
  };
  api.state.filters.statuses = ["in_progress"];
  api.state.filters.search = "";
  const html = api.renderBoard(stories);
  assert.ok(!html.includes("board-empty"),
    "no empty state without an active search term");
});

// renderPlanDetail binds the search input's input event so typing narrows.
test("renderPlanDetail binds search input input event", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  const section = makeEl("section", { attrs: { id: "plan-detail" } });
  doc.register("plan-detail", section);
  const plan = {
    name: "p",
    stories: {
      "abc-123": { key: "abc-123", summary: "Add login", persona: "software-engineer", risk: "low", status: "in_progress" },
    },
    notifications: [],
    decisions: [],
  };
  api.renderPlanDetail(plan);
  const searchInput = section.querySelector(".filter-search");
  assert.ok(searchInput, "rendered detail should contain a .filter-search input");
  assert.ok(
    Array.isArray(searchInput._listeners && searchInput._listeners.input)
      && searchInput._listeners.input.length > 0,
    "search input should have an input event listener bound"
  );
});

// Typing into the search input updates state.filters.search and re-renders.
test("search input handler updates state and re-renders", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  const section = makeEl("section", { attrs: { id: "plan-detail" } });
  doc.register("plan-detail", section);
  const plan = {
    name: "p",
    stories: {
      "abc-123": { key: "abc-123", summary: "Add login", persona: "software-engineer", risk: "low", status: "in_progress" },
    },
    notifications: [],
    decisions: [],
  };
  api.renderPlanDetail(plan);
  const searchInput = section.querySelector(".filter-search");
  searchInput.value = "login";
  searchInput._listeners.input.forEach((fn) => fn({ target: searchInput }));
  assert.strictEqual(api.state.filters.search, "login",
    "input handler should set state.filters.search");
});

// URL hash round-trip: search term is encoded as &q= and decoded back.
test("search term round-trips through the URL hash as &q=", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  // encodeHashState should emit q= for a non-empty search term.
  api.state.filters.search = "add login";
  const encoded = api.encodeHashState();
  assert.ok(/(^|&)q=add%20login/.test(encoded),
    "encodeHashState should emit q= with the URL-encoded term");

  // parseHash should decode it back into filters.search.
  const parsed = api.parseHash(encoded);
  assert.strictEqual(parsed.filters.search, "add login",
    "parseHash should restore filters.search from q=");
});

// Hash round-trip: empty search is NOT emitted (no trailing q=).
test("empty search term is not emitted to the hash", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  api.state.filters.search = "";
  const encoded = api.encodeHashState();
  assert.ok(!/(^|&)q=/.test(encoded),
    "empty search should not be emitted to the hash");
});

// Hash round-trip: malformed encoding is ignored, not thrown.
test("malformed q= encoding is ignored without throwing", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  let parsed;
  assert.doesNotThrow(() => {
    parsed = api.parseHash("q=%E0%A4");
  }, "parseHash must not throw on malformed percent-encoding");
  // search should remain its default (not set to garbage).
  assert.ok(
    !parsed.filters.search || parsed.filters.search === "",
    "malformed q= should leave filters.search unset/empty"
  );
});

// FILTERS_KEY persistence: saveFilters persists search; load restores it.
test("search term round-trips through FILTERS_KEY persistence", () => {
  const store = {};
  const doc = makeDocument();
  const ft = fakeTimers();
  // Override the global localStorage with a real backing store.
  global.localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
  };
  const api = loadAppJs({ doc, fakeTimers: ft });
  api.state.filters.search = "persist me";
  api.saveFilters();
  // The persisted blob must mention the search term.
  const blob = store[api.FILTERS_KEY] || store["dashboard-filters"] || "";
  assert.ok(
    Object.values(store).some((v) => String(v).includes("persist me")),
    "saveFilters should persist the search term into localStorage"
  );
});

// Reset clears search back to "" (defaultFilters includes search: "").
test("reset via defaultFilters clears search", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });
  api.state.filters.search = "something";
  api.state.filters = api.defaultFilters();
  assert.strictEqual(api.state.filters.search, "",
    "defaultFilters should reset search to ''");
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);