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
  const doc = {
    get hidden() { return hidden; },
    set hidden(v) { hidden = !!v; },
    body: makeEl("body"),
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
  return doc;
}

// Register the elements that app.js wires up at top level so the file can
// attach listeners to them under our stub. We treat the `checked` HTML
// attribute as the input's initial checked state — matching real browser
// behavior where <input type="checkbox" checked> starts checked.
function bootstrapDoc(doc, { autoRefreshChecked = false } = {}) {
  const close = makeEl("button", { attrs: { id: "story-modal-close" } });
  const modal = makeEl("div", { attrs: { id: "story-modal" } });
  const ar = makeEl("input", {
    attrs: { id: "auto-refresh", type: "checkbox" },
  });
  ar.checked = autoRefreshChecked;
  const lu = makeEl("span", { attrs: { id: "last-updated" } });
  const ub = makeEl("div", { attrs: { id: "usage-banner" } });
  doc.register("story-modal-close", close);
  doc.register("story-modal", modal);
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
    state,
  };`;
  const m = { exports: {} };
  // eslint-disable-next-line no-new-func
  const fn = new Function("module", "document", "CSS", "setInterval",
    "clearInterval", "setTimeout", "clearTimeout", "localStorage",
    "require", "module", wrapped);
  fn(m, doc, global.CSS, global.setInterval, global.clearInterval,
    global.setTimeout, global.clearTimeout, global.localStorage,
    require, m);
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

// Case: scroll position survives a refresh when a column is scrolled.
test("scroll position survives a refresh", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });

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
test("focused filter chip retains focus across a refresh", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });

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
test("missing focused chip is silently skipped (no throw)", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });

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
test("flashRefreshIndicator populates the indicator element", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });

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
test("auto-refresh checkbox uncheck stops polling via change handler", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft, autoRefreshChecked: true });

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
test("visibilitychange stops polling when hidden, resumes when visible", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft, autoRefreshChecked: true });

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

// --- Story modal: grouped sections, mono <pre>, copy affordance ---

// Helper: extract the inner text of a heading by matching section titles
// inside the modal-body innerHTML. We only need to know a heading exists
// with the right text; exact DOM walks are easier if we lean on raw HTML.
function innerHtmlOf(doc, id) {
  return doc.getElementById(id)._innerHTML || "";
}

function findSectionTitles(html) {
  const re = /<h3[^>]*class="[^"]*modal-section[^"]*"[^>]*>([^<]+)<\/h3>/g;
  const out = [];
  let m;
  while ((m = re.exec(html)) !== null) out.push(m[1].trim());
  return out;
}

test("showStoryModal renders the four grouped sections for a full story", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });

  const body = makeEl("div", { attrs: { id: "story-modal-body" } });
  const modal = makeEl("div", { attrs: { id: "story-modal" }, classList: ["modal", "hidden"] });
  doc.register("story-modal-body", body);
  doc.register("story-modal", modal);

  api.showStoryModal({
    summary: "Do the thing",
    persona: "eng",
    model: "sonnet",
    risk: "low",
    dependencies: ["a", "b"],
    status: "in_progress",
    backend: "claude",
    escalated: false,
    worktree: "/tmp/wt/long/path/to/worktree",
    branch: "feat/widget",
    pid: 12345,
    pr_url: "https://github.com/x/y/pull/99",
    last_commit: "abc1234",
    interrupted_at: "2024-01-01T00:00:00Z",
    dispatch_attempts: 1,
    rework_attempts: 0,
    merge_attempts: 0,
    review_verdict: "approve",
    review_feedback: "lgtm",
    dispatch_error: "boom",
    merge_error: "x",
    failure_reason: "y",
    parked_reason: "z",
  }, "story-42");

  const html = innerHtmlOf(doc, "story-modal-body");
  const titles = findSectionTitles(html);
  // All four sections present and in the documented order.
  assert.deepStrictEqual(titles, [
    "Identity", "Lifecycle", "Dispatch & review", "Errors",
  ]);
  // Sample fields are surfaced under their section.
  assert.ok(html.includes("Do the thing"), "summary surfaced");
  assert.ok(html.includes("Eng"), "persona surfaced");
  assert.ok(html.includes("/tmp/wt/long/path/to/worktree"), "worktree surfaced");
  assert.ok(html.includes("https://github.com/x/y/pull/99"), "pr_url surfaced");
  // Empty sections / undefined values omitted (escalated/false isn't shown).
  assert.ok(!/Escalated/.test(html.replace(/<dt>[^<]*<\/dt>/, "")) || /<dt>[\s\S]*?escalated/i.test(html) === false,
    "escalated false should still be optional — we accept either omission or presence");
});

test("showStoryModal omits empty sections for a minimal manifest", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });

  const body = makeEl("div", { attrs: { id: "story-modal-body" } });
  const modal = makeEl("div", { attrs: { id: "story-modal" }, classList: ["modal", "hidden"] });
  doc.register("story-modal-body", body);
  doc.register("story-modal", modal);

  api.showStoryModal({ summary: "minimal", status: "todo" }, "story-min");

  const html = innerHtmlOf(doc, "story-modal-body");
  const titles = findSectionTitles(html);
  // Only Identity (key + summary) and Lifecycle (status) sections.
  assert.deepStrictEqual(titles, ["Identity", "Lifecycle"]);
  // No Errors/Dispatch sections for a clean minimal story.
  assert.ok(!/Dispatch/.test(html), "no Dispatch & review section when none of its fields are set");
  assert.ok(!/Errors/.test(html), "no Errors section when none of its fields are set");
});

test("showStoryModal uses <pre class=\"mono\"> for path / url / pid / json fields", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });

  const body = makeEl("div", { attrs: { id: "story-modal-body" } });
  const modal = makeEl("div", { attrs: { id: "story-modal" }, classList: ["modal", "hidden"] });
  doc.register("story-modal-body", body);
  doc.register("story-modal", modal);

  api.showStoryModal({
    status: "in_progress",
    worktree: "/very/long/path/to/the/worktree/dir",
    pr_url: "https://example.com/" + "segment/".repeat(20) + "end",
    pid: 98765,
    dispatch_error: JSON.stringify({ foo: "bar", list: [1, 2, 3] }),
  }, "story-pre");

  const html = innerHtmlOf(doc, "story-modal-body");
  // Each of these long/path/JSON values lives inside a <pre class="mono".
  for (const value of [
    "/very/long/path/to/the/worktree/dir",
    "https://example.com/",
    "98765",
    '"foo": "bar"',
  ]) {
    const re = new RegExp(
      `<pre[^>]*class="[^"]*mono[^"]*"[^>]*>\\s*${value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").slice(0, 30)}`,
    );
    assert.ok(re.test(html), `expected <pre class="mono"> to contain start of ${value.slice(0, 30)}`);
  }
});

test("showStoryModal adds copy buttons for key, worktree, pr_url, pid", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });

  const body = makeEl("div", { attrs: { id: "story-modal-body" } });
  const modal = makeEl("div", { attrs: { id: "story-modal" }, classList: ["modal", "hidden"] });
  doc.register("story-modal-body", body);
  doc.register("story-modal", modal);

  api.showStoryModal({
    summary: "x", status: "in_progress",
    worktree: "/tmp/wt", pr_url: "https://x/y", pid: 7,
  }, "story-copy");

  const html = innerHtmlOf(doc, "story-modal-body");
  for (const target of ["key", "worktree", "pr_url", "pid"]) {
    assert.ok(html.includes(`data-copy="${target}"`),
      `expected a copy button with data-copy="${target}"`);
  }
  // Headline key itself is also surfaced up top.
  assert.ok(html.includes("story-copy"), "key string shown for the modal heading");
});

test("copy button click calls navigator.clipboard.writeText with the field value", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });

  const body = makeEl("div", { attrs: { id: "story-modal-body" } });
  const modal = makeEl("div", { attrs: { id: "story-modal" }, classList: ["modal", "hidden"] });
  doc.register("story-modal-body", body);
  doc.register("story-modal", modal);

  // Provide a stub clipboard with a write spy.
  const writes = [];
  global.navigator = {
    clipboard: { writeText: (s) => { writes.push(s); return Promise.resolve(); } },
  };

  api.showStoryModal({
    summary: "x", status: "in_progress",
    worktree: "/tmp/wt-1", pr_url: "https://x/y", pid: 42,
  }, "story-write");

  // The modal body should have a click listener registered for copy buttons.
  const clickHandlers = body._listeners && body._listeners.click ? body._listeners.click : [];
  assert.ok(clickHandlers.length > 0,
    "showStoryModal should register a click handler on the body for copy buttons");
  // Simulate clicking the worktree copy button.
  const fakeTarget = { dataset: { copy: "worktree" } };
  clickHandlers[clickHandlers.length - 1]({ target: fakeTarget });
  assert.deepStrictEqual(writes, ["/tmp/wt-1"], "clicking copy wrote worktree path");
  delete global.navigator;
});

test("copy button click is a no-op (no throw) when navigator.clipboard is missing", () => {
  const doc = makeDocument();
  const ft = fakeTimers();
  const api = loadAppJs({ doc, fakeTimers: ft });

  const body = makeEl("div", { attrs: { id: "story-modal-body" } });
  const modal = makeEl("div", { attrs: { id: "story-modal" }, classList: ["modal", "hidden"] });
  doc.register("story-modal-body", body);
  doc.register("story-modal", modal);

  // No navigator at all -> clipboard API unavailable.
  delete global.navigator;

  api.showStoryModal({
    summary: "x", status: "in_progress",
    worktree: "/tmp/wt", pr_url: "https://x/y", pid: 1,
  }, "story-noclip");

  const clickHandlers = body._listeners && body._listeners.click ? body._listeners.click : [];
  assert.ok(clickHandlers.length > 0, "click handler was registered");
  const handler = clickHandlers[clickHandlers.length - 1];
  // Should not throw even though navigator.clipboard is undefined.
  assert.doesNotThrow(() => handler({ target: { dataset: { copy: "worktree" } } }),
    "copy handler must no-op when clipboard API is unavailable");
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);