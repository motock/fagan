// Tests for the fleet Overview landing view and dispatch_health refresh wiring.
//
// Run with: node tests/test_app_overview.js
//
// Testable criteria (from the story):
//   - refresh() fetches /api/dispatch_health
//   - Overview renders empty states gracefully (no NaN) when /api/plans is
//     {plans:[]} and /api/dispatch_health returns all-zero totals
//   - escalation_rate / success_rate display as "0%" not "NaN%" when
//     dispatched count is zero (backend returns 0.0, UI must format)
//   - a plan with no stories must not break the list
//   - selecting Overview clears the selection and renders the landing view
//
// These cases reuse the DOM stub from test_app_js_smoke.js. We append
// additional stubs for fetch so we can drive refresh() under test.

const assert = require("assert");

// --- minimal DOM stub (intentionally smaller than test_app_js_smoke.js) ---
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
    children: [],
    _innerHTML: "",
    _textContent: "",
    scrollTop: 0,
    style: {},
    setAttribute(k, v) { this[k] = v; },
    addEventListener(name, fn) {
      (el._listeners = el._listeners || {})[name]
        = (el._listeners[name] || []).concat(fn);
    },
    appendChild(child) {
      el.children.push(child);
      child.parentNode = el;
      return child;
    },
    querySelector(sel) { return querySelectorAll(el, sel)[0] || null; },
    querySelectorAll(sel) { return querySelectorAll(el, sel); },
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return el._innerHTML; },
    set(v) {
      el._innerHTML = v;
      el.children = parseInnerHtml(v);
      for (const c of el.children) c.parentNode = el;
    },
  });
  Object.defineProperty(el, "textContent", {
    get() { return el._textContent; },
    set(v) { el._textContent = v; },
  });
  return el;
}

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
      const key = dm[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      ds[key] = dm[2];
    }
    const child = makeEl(tag, { classList: cls, dataset: ds });
    child.children = parseInnerHtml(inner);
    for (const c of child.children) c.parentNode = child;
    out.push(child);
  }
  return out;
}

function querySelectorAll(root, sel) {
  // Support only the subset of CSS selectors the production code uses:
  //   .cls             -> single class match
  //   tag.cls          -> tag with class (used for .filter-chip, .card etc.)
  //   [data-x="y"]     -> exact attribute match
  // We always treat the leading selector as ".cls" if it begins with a dot,
  // and "[attr=val]" if it begins with a bracket.
  const out = [];
  function walk(n) {
    if (!n || !n.children) return;
    if (matches(n, sel)) out.push(n);
    for (const c of n.children) walk(c);
  }
  walk(root);
  return out;
}

function matches(el, sel) {
  if (sel.startsWith(".")) {
    const cls = sel.slice(1);
    return el.classList && el.classList.contains(cls);
  }
  if (sel.startsWith("[")) {
    const m = sel.match(/^\[([\w-]+)="([^"]*)"\]$/);
    if (!m) return false;
    return el.dataset && el.dataset[m[1]] === m[2];
  }
  return false;
}

function makeDocument() {
  const initial = {};
  const byId = new Map();
  const listeners = {};
  let activeElement = null;
  let hidden = false;
  const doc = {
    body: makeEl("body"),
    documentElement: makeEl("html"),
    createElement(tag) { return makeEl(tag); },
    get hidden() { return hidden; },
    set hidden(v) { hidden = !!v; },
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
      (doc._listeners = doc._listeners || {})[name]
        = (doc._listeners[name] || []).concat(fn);
    },
    fire(name) {
      (listeners[name] || []).forEach((fn) => fn({ target: doc.body }));
    },
  };
  doc._listeners = doc._listeners || {};
  doc.body._ownerDoc = doc;
  return doc;
}

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
    intervals, timeouts,
  };
}

function bootstrapDoc(doc, { autoRefreshChecked = false } = {}) {
  const close = makeEl("button", { attrs: { id: "story-modal-close" } });
  const modal = makeEl("div", { attrs: { id: "story-modal" } });
  const ar = makeEl("input", { attrs: { id: "auto-refresh", type: "checkbox" } });
  ar.checked = autoRefreshChecked;
  const lu = makeEl("span", { attrs: { id: "last-updated" } });
  const ub = makeEl("div", { attrs: { id: "usage-banner" } });
  doc.register("story-modal-close", close);
  doc.register("story-modal", modal);
  const smb = makeEl("div", { attrs: { id: "story-modal-body" } });
  doc.register("story-modal-body", smb);
  doc.register("auto-refresh", ar);
  doc.register("last-updated", lu);
  doc.register("usage-banner", ub);
  const pl = makeEl("nav", { attrs: { id: "plan-list" } });
  doc.register("plan-list", pl);
}

// Capture fetch URLs seen during refresh() so we can assert what was called.
function makeFetchSpy({ plans = { plans: [] }, health = null, usage = { available: false } }) {
  const calls = [];
  const handler = async (url) => {
    calls.push(url);
    if (typeof url === "string" && url === "/api/dispatch_health") {
      return { ok: true, status: 200, json: async () => health };
    }
    if (typeof url === "string" && url === "/api/usage") {
      return { ok: true, status: 200, json: async () => usage };
    }
    if (typeof url === "string" && url === "/api/plans") {
      return { ok: true, status: 200, json: async () => plans };
    }
    if (typeof url === "string" && url.startsWith("/api/plans/")) {
      return { ok: true, status: 200, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  return { fetch: handler, calls };
}

function loadAppJs({ doc, ft, fetchImpl }) {
  global.document = doc;
  global.CSS = { escape: (s) => String(s).replace(/"/g, '\\"') };
  global.setInterval = ft.setInterval;
  global.clearInterval = ft.clearInterval;
  global.setTimeout = ft.setTimeout;
  global.clearTimeout = ft.clearTimeout;
  global.localStorage = { getItem: () => null, setItem: () => {} };
  global.fetch = fetchImpl;
  // Provide a minimal `window` stub. The production code attaches `state`
  // and `BACKEND_VALUES` to it for cross-realm test access, and reads
  // `window.location.hash` for deep-linking.
  const winListeners = {};
  const win = {
    location: { hash: "" },
    documentElement: makeEl("html"),
    addEventListener(name, fn) {
      (winListeners[name] = winListeners[name] || []).push(fn);
    },
    removeEventListener(name, fn) {
      const arr = winListeners[name] || [];
      const i = arr.indexOf(fn);
      if (i !== -1) arr.splice(i, 1);
    },
    fire(name) { (winListeners[name] || []).forEach((fn) => fn()); },
  };
  global.window = win;

  bootstrapDoc(doc);
  const fs = require("fs");
  const path = require("path");
  const src = fs.readFileSync(
    path.join(__dirname, "..", "static", "app.js"), "utf8");
  const wrapped = `${src}\nmodule.exports = {
    capturePlanDetailState, restorePlanDetailState, flashRefreshIndicator,
    startPolling, stopPolling, syncPollingWithVisibility, renderPlanDetail,
    renderOverview, selectOverview, refresh, state,
  };`;
  const m = { exports: {} };
  const fn = new Function("module", "document", "window", "CSS", "setInterval",
    "clearInterval", "setTimeout", "clearTimeout", "localStorage",
    "require", "module", wrapped);
  fn(m, doc, win, global.CSS, global.setInterval, global.clearInterval,
    global.setTimeout, global.clearTimeout, global.localStorage,
    require, m);
  return m.exports;
}

const EMPTY_HEALTH = {
  with_acceptance: {
    dispatched: 0, done: 0, escalated: 0, stories: 0,
    escalation_rate: 0.0, success_rate: 0.0,
  },
  without_acceptance: {
    dispatched: 0, done: 0, escalated: 0, stories: 0,
    escalation_rate: 0.0, success_rate: 0.0,
  },
};

let pass = 0, fail = 0;
function test(name, fn) {
  return Promise.resolve().then(fn).then(
    () => { console.log(`ok  - ${name}`); pass++; },
    (e) => { console.log(`FAIL - ${name}: ${e.message}`); fail++; }
  );
}

(async () => {
  // Case: refresh() fetches /api/dispatch_health.
  await test("refresh() fetches /api/dispatch_health", async () => {
    const doc = makeDocument();
    const ft = fakeTimers();
    const spy = makeFetchSpy({ health: EMPTY_HEALTH });
    const api = loadAppJs({ doc, ft, fetchImpl: spy.fetch });
    const section = makeEl("section", { attrs: { id: "plan-detail" } });
    doc.register("plan-detail", section);
    await api.refresh();
    assert.ok(spy.calls.includes("/api/dispatch_health"),
      `expected refresh() to call /api/dispatch_health, got: ${JSON.stringify(spy.calls)}`);
  });

  // Case: empty Overview renders with no NaN.
  await test("renderOverview with empty data renders no NaN", async () => {
    const doc = makeDocument();
    const ft = fakeTimers();
    const spy = makeFetchSpy({ health: EMPTY_HEALTH });
    const api = loadAppJs({ doc, ft, fetchImpl: spy.fetch });
    const section = makeEl("section", { attrs: { id: "plan-detail" } });
    doc.register("plan-detail", section);

    assert.doesNotThrow(() => {
      api.renderOverview({ plans: [] }, EMPTY_HEALTH);
    });

    const html = section.innerHTML;
    assert.ok(!html.includes("NaN"),
      `Overview HTML should not contain 'NaN'; got: ${html}`);
  });

  // Case: zero-dispatched rates must display as "0%" not "NaN%".
  await test("zero-dispatched rates show '0%' not 'NaN%'", async () => {
    const doc = makeDocument();
    const ft = fakeTimers();
    const spy = makeFetchSpy({ health: EMPTY_HEALTH });
    const api = loadAppJs({ doc, ft, fetchImpl: spy.fetch });
    const section = makeEl("section", { attrs: { id: "plan-detail" } });
    doc.register("plan-detail", section);

    api.renderOverview({ plans: [] }, EMPTY_HEALTH);

    const html = section.innerHTML;
    assert.ok(html.includes("0%"),
      `expected Overview to include '0%', got: ${html}`);
    assert.ok(!html.includes("NaN%"),
      `expected no 'NaN%' substring, got: ${html}`);
    assert.ok(!html.includes("undefined%"),
      `expected no 'undefined%' substring, got: ${html}`);
  });

  // Case: a plan with no stories must not break the list rendering.
  await test("plan with no stories renders cleanly in Overview", async () => {
    const doc = makeDocument();
    const ft = fakeTimers();
    const spy = makeFetchSpy({
      health: EMPTY_HEALTH,
      plans: { plans: [{ name: "empty-plan", story_count: 0, status_counts: {}, paused: false }] },
    });
    const api = loadAppJs({ doc, ft, fetchImpl: spy.fetch });
    const section = makeEl("section", { attrs: { id: "plan-detail" } });
    doc.register("plan-detail", section);

    assert.doesNotThrow(() => {
      api.renderOverview({ plans: [{ name: "empty-plan", story_count: 0, status_counts: {}, paused: false }] }, EMPTY_HEALTH);
    });

    const html = section.innerHTML;
    assert.ok(!html.includes("NaN"),
      `expected no NaN when plan has zero stories; got: ${html}`);
    assert.ok(html.includes("0/0"),
      `expected plan to render '0/0' done/total; got: ${html}`);
  });

  // Case: refresh with empty data renders the Overview without throwing.
  await test("refresh() with empty plans + zero health renders Overview", async () => {
    const doc = makeDocument();
    const ft = fakeTimers();
    const spy = makeFetchSpy({ health: EMPTY_HEALTH });
    const api = loadAppJs({ doc, ft, fetchImpl: spy.fetch });
    const section = makeEl("section", { attrs: { id: "plan-detail" } });
    doc.register("plan-detail", section);
    // Make sure no plan is selected.
    api.state.selectedPlan = null;

    await assert.doesNotReject(async () => {
      await api.refresh();
    });

    const html = section.innerHTML;
    assert.ok(!html.includes("NaN"),
      `expected no NaN after refresh() with empty data; got: ${html}`);
  });

  // Case: selectOverview clears selection and re-renders Overview.
  await test("selectOverview clears state.selectedPlan and renders Overview", async () => {
    const doc = makeDocument();
    const ft = fakeTimers();
    const spy = makeFetchSpy({ health: EMPTY_HEALTH });
    const api = loadAppJs({ doc, ft, fetchImpl: spy.fetch });
    const section = makeEl("section", { attrs: { id: "plan-detail" } });
    doc.register("plan-detail", section);
    api.state.selectedPlan = "some-plan";

    api.selectOverview();

    assert.strictEqual(api.state.selectedPlan, null,
      "selectOverview must clear selectedPlan");
    // The section should now have some Overview-ish content.
    const html = section.innerHTML;
    assert.ok(html.length > 0,
      "selectOverview must render Overview content into #plan-detail");
  });

  // Case: Overview shows done count and paused flag for plans.
  await test("Overview list shows done/total and paused flag", async () => {
    const doc = makeDocument();
    const ft = fakeTimers();
    const spy = makeFetchSpy({ health: EMPTY_HEALTH });
    const api = loadAppJs({ doc, ft, fetchImpl: spy.fetch });
    const section = makeEl("section", { attrs: { id: "plan-detail" } });
    doc.register("plan-detail", section);

    api.renderOverview({
      plans: [
        { name: "alpha", story_count: 5, status_counts: { done: 3 }, paused: false },
        { name: "beta",  story_count: 2, status_counts: { done: 0 }, paused: true },
      ],
    }, EMPTY_HEALTH);

    const html = section.innerHTML;
    assert.ok(html.includes("alpha"),
      "expected 'alpha' plan name in Overview HTML");
    assert.ok(html.includes("3/5"),
      "expected '3/5' done/total for alpha; got: " + html);
    assert.ok(html.includes("beta"),
      "expected 'beta' plan name in Overview HTML");
    assert.ok(html.toLowerCase().includes("paused"),
      "expected paused flag rendered for beta");
  });

  // Case: Overview shows both with_acceptance and without_acceptance stat cards.
  await test("Overview renders with_acceptance and without_acceptance stat cards", async () => {
    const doc = makeDocument();
    const ft = fakeTimers();
    const spy = makeFetchSpy({
      health: {
        with_acceptance: {
          dispatched: 4, done: 3, escalated: 1, stories: 4,
          escalation_rate: 0.25, success_rate: 0.75,
        },
        without_acceptance: {
          dispatched: 6, done: 1, escalated: 4, stories: 6,
          escalation_rate: 0.6667, success_rate: 0.1667,
        },
      },
    });
    const api = loadAppJs({ doc, ft, fetchImpl: spy.fetch });
    const section = makeEl("section", { attrs: { id: "plan-detail" } });
    doc.register("plan-detail", section);

    api.renderOverview({ plans: [] }, spy.fetch && (await spy.fetch("/api/dispatch_health")).json ? null : null);
    // We can't easily await the json from spy.fetch above without keeping a handle.
    // Just call renderOverview directly with the matching payload:
    api.renderOverview({ plans: [] }, {
      with_acceptance: {
        dispatched: 4, done: 3, escalated: 1, stories: 4,
        escalation_rate: 0.25, success_rate: 0.75,
      },
      without_acceptance: {
        dispatched: 6, done: 1, escalated: 4, stories: 6,
        escalation_rate: 0.6667, success_rate: 0.1667,
      },
    });

    const html = section.innerHTML;
    assert.ok(html.toLowerCase().includes("with"),
      "expected a 'with' acceptance stat card; got: " + html);
    assert.ok(html.toLowerCase().includes("without"),
      "expected a 'without' acceptance stat card; got: " + html);
    assert.ok(html.includes("25%"),
      "expected '25%' escalation rate for with_acceptance; got: " + html);
    assert.ok(html.includes("67%") || html.includes("66%"),
      "expected ~67% escalation rate for without_acceptance; got: " + html);
    assert.ok(html.includes("75%"),
      "expected '75%' success rate for with_acceptance; got: " + html);
    assert.ok(!html.includes("NaN"),
      "expected no NaN in rendered Overview; got: " + html);
  });

  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail === 0 ? 0 : 1);
})().catch((e) => {
  console.error("test runner crashed:", e);
  process.exit(2);
});