// Tests for repo-scoped plan-list rendering (the `repo` filter wired into the
// dashboard UI).
//
// Run with:  node tests/unit/test_plan_list_scoping.mjs
//
// Written test-first (TDD). Until the implementation lands these tests fail:
//   - state.js has no `showAllRepos` field,
//   - main.js's refresh() never appends a `repo=` query param,
//   - plan-list.js has no "Show plans from all repositories" checkbox,
//   - renderPlanList has no repo-scoped empty state,
//   - the workspace-switch path never re-runs refresh().
// That is the expected RED state; a later dispatch implements against them.
//
// Harness notes (mirrors tests/test_app_workspace.mjs and
// tests/unit/test_comms_sse_stream.mjs):
//   - jsdom is not installed in this repo, so a hand-rolled cached-element
//     document stub is used. It implements the small DOM surface main.js /
//     plan-list.js actually touch: getElementById (per-id cached), createElement,
//     appendChild / insertBefore / remove, classList, addEventListener, a real
//     recursive querySelector / querySelectorAll over the child tree, and
//     textContent / innerHTML.
//   - tests/_app_js_loader.mjs hardcodes static/app.js as its import target, so
//     the browser globals are bootstrapped THROUGH the shared loader (it installs
//     globalThis.window / document / localStorage / fetch and keeps them set for
//     call-time reads), and the module namespace it returns is app.js's
//     re-export of main.js (so `refresh`, `stopPolling`, `state` are available).
//   - Module identity: main.js is imported by the loader at its plain URL, so its
//     child specifiers (./state.js, ./render/plan-list.js) resolve to the SAME
//     plain URLs this file imports directly. Mutating the state singleton here is
//     therefore visible inside refresh() and renderPlanList().
//   - fetch is a module-level `currentFetch` swapped per test; the window stub's
//     fetch delegates to it at call time, so globalThis.fetch (wired by the
//     loader) always reaches the active handler. Every requested URL is recorded
//     so the plan-list URL can be asserted.
//
// Grading scope: THIS story only. main.js / plan-list.js / state.js are shared,
// cumulative artifacts, so assertions are membership / behaviour based — never
// byte hashes, exact file contents, exact full-URL strings, or exact totals.
//
// Covered:
//   POSITIVE:
//     - state exposes `showAllRepos`, defaulting to false, and resetState()
//       clears it.
//     - default load with a workspace selected fetches /api/plans WITH a
//       `repo=` param equal to that workspace path.
//     - checking the all-repos toggle refetches WITHOUT the repo param;
//       unchecking re-applies it.
//     - `include_archived=true` and `repo=` compose in one URL.
//     - switching the active workspace triggers a refetch carrying the new
//       workspace's path.
//     - the all-repos checkbox lives in the sidebar footer beside the existing
//       "Show dismissed plans" checkbox and reflects state.showAllRepos.
//     - with a repo filter active and zero plans, the scoped empty message
//       renders (naming the repository).
//   NEGATIVE / BOUNDARY:
//     - with NO workspace selected, no repo param is sent (all-plans fallback).
//     - with the opt-out on and zero plans, the global/blank empty state is
//       unchanged (no scoped message).
//     - the existing "Show dismissed plans" toggle behaviour is unchanged
//       (flips state.showArchived, refetches with include_archived=true, keeps
//       the repo scope).

import { loadAppInto } from "../_app_js_loader.mjs";

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

// ---------- element / document stubs ----------

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

function makeElement(tag) {
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    nodeType: 1,
    children: [],
    parentNode: null,
    style: {},
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
    const visit = (node) => {
      for (const c of node.children) {
        if (matches(c, sel)) out.push(c);
        visit(c);
      }
    };
    visit(el);
    return out;
  };
  // Real recursive search; falls back to a throwaway stub when nothing matches
  // so innerHTML-parsed markup (e.g. .plan-archive-btn) never crashes a caller.
  el.querySelector = (sel) => {
    const found = el.querySelectorAll(sel);
    return found.length ? found[0] : makeElement("div");
  };
  el.closest = (sel) => {
    let n = el;
    while (n) {
      if (matches(n, sel)) return n;
      n = n.parentNode;
    }
    return null;
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
      el.children = [];
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
// document.getElementById("plan-list") always returns the SAME node the
// implementation rendered into.
const doc = {
  _byId: new Map(),
  getElementById(id) {
    if (!this._byId.has(id)) this._byId.set(id, makeElement("div"));
    return this._byId.get(id);
  },
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (tag) => makeElement(tag),
  createTextNode: (t) => ({ nodeType: 3, textContent: String(t) }),
  body: makeElement("body"),
  documentElement: makeElement("html"),
  head: makeElement("head"),
  title: "",
  hidden: false,
  addEventListener: noop,
  removeEventListener: noop,
};

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

// ---------- fetch stub ----------

let currentFetch = null;
let fetchLog = [];
let plansPayload = { plans: [] };

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function installFetch() {
  currentFetch = async (url, opts) => {
    const u = String(url);
    fetchLog.push(u);
    if (u === "/api/plans" || u.startsWith("/api/plans?")) {
      return jsonResponse(200, plansPayload);
    }
    if (/^\/api\/plans\/[^/?]+$/.test(u)) {
      return jsonResponse(200, {
        name: decodeURIComponent(u.split("/").pop()),
        notification_records: [],
      });
    }
    if (u.startsWith("/api/usage")) return jsonResponse(200, { available: false });
    if (u.startsWith("/api/dispatch_health")) return jsonResponse(200, {});
    if (u.startsWith("/api/health")) return jsonResponse(200, {});
    if (u === "/api/workspaces") return jsonResponse(200, { workspaces: [] });
    if (u === "/api/workspace") {
      if (opts && opts.method === "POST") return jsonResponse(200, { ok: true });
      return jsonResponse(200, { active: null });
    }
    return jsonResponse(200, {});
  };
}

// ---------- bootstrap ----------

let appMod = null;
let stateMod = null;
let planListMod = null;

async function flush() {
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
}

async function bootstrap() {
  if (appMod) return;
  installFetch();
  appMod = await loadAppInto(makeWindowStub());
  // Kill the 4s poll so it cannot inject stray fetches mid-test.
  if (typeof appMod.stopPolling === "function") appMod.stopPolling();
  // Same plain URLs main.js resolves its own imports to -> same module
  // instances, so the state singleton is shared.
  stateMod = await import("../../static/app/state.js");
  planListMod = await import("../../static/app/render/plan-list.js");
  await flush();
}

// ---------- per-test reset ----------

function reset() {
  stateMod.state.selectedWorkspace = null;
  stateMod.state.showAllRepos = false;
  stateMod.state.showArchived = false;
  stateMod.state.selectedPlan = null;
  stateMod.state.commsActive = true;
  stateMod.state.configActive = false;
  stateMod.state.workspaceActive = false;
  if (typeof planListMod.resetPlanListState === "function") {
    planListMod.resetPlanListState();
  }
  doc.getElementById("plan-list").innerHTML = "";
  fetchLog = [];
  plansPayload = { plans: [] };
}

// ---------- helpers ----------

function nav() {
  return doc.getElementById("plan-list");
}

function plansListUrls() {
  return fetchLog.filter((u) => u === "/api/plans" || u.startsWith("/api/plans?"));
}

function lastPlansUrl() {
  const urls = plansListUrls();
  return urls.length ? urls[urls.length - 1] : null;
}

function queryParams(url) {
  const i = String(url).indexOf("?");
  return new URLSearchParams(i === -1 ? "" : String(url).slice(i + 1));
}

// Every text-ish string that ever landed on the element (own textContent,
// innerHTML, and recursively its children), so "contains" assertions work
// regardless of whether the implementation renders via textContent or innerHTML.
function textOf(el) {
  if (!el) return "";
  let out = `${el._textContent || ""} ${el._innerHTML || ""}`;
  for (const c of el.children || []) out += ` ${textOf(c)}`;
  return out;
}

function walk(root, fn) {
  for (const c of root.children || []) {
    fn(c);
    walk(c, fn);
  }
}

// Find a checkbox whose surrounding label/wrapper text mentions `text`.
// Prefers a <label> whose own text mentions `text` (the file's construction),
// so two checkboxes sharing one footer div can't both match the same string.
function findCheckboxByNearbyText(root, text) {
  let labelMatch = null;
  walk(root, (el) => {
    if (labelMatch) return;
    if (el.tagName !== "LABEL" || !textOf(el).includes(text)) return;
    const inputs = [];
    walk(el, (c) => {
      if (c.tagName === "INPUT" && c.type === "checkbox") inputs.push(c);
    });
    if (inputs.length) labelMatch = inputs[0];
  });
  if (labelMatch) return labelMatch;

  // Fallback: nearest ancestor whose text mentions `text`.
  let best = null;
  let bestDepth = Infinity;
  walk(root, (el) => {
    if (el.tagName !== "INPUT" || el.type !== "checkbox") return;
    let n = el.parentNode;
    let depth = 0;
    while (n && n !== root) {
      depth++;
      if (textOf(n).includes(text)) {
        if (depth < bestDepth) {
          bestDepth = depth;
          best = el;
        }
        break;
      }
      n = n.parentNode;
    }
  });
  return best;
}

function hasAncestorClass(el, cls) {
  let n = el.parentNode;
  while (n) {
    if (n._className && n._className.split(/\s+/).includes(cls)) return true;
    n = n.parentNode;
  }
  return false;
}

// Fire a `change` event at a checkbox the way a user click would, then let the
// (un-awaited) _refresh() chain settle.
async function fireChange(el, checked) {
  el.checked = checked;
  const listeners = el._listeners.change || [];
  for (const fn of listeners) fn({ type: "change", target: el });
  await flush();
}

async function run(name, fn) {
  try {
    await fn();
    record(name, true);
  } catch (err) {
    record(name, false, err && err.message ? err.message : String(err));
  }
}

// ---------- state.js: the showAllRepos flag ----------

await run("state exposes showAllRepos, defaulting to false", async () => {
  await bootstrap();
  assertTrue(
    Object.prototype.hasOwnProperty.call(stateMod.state, "showAllRepos"),
    "state should have a showAllRepos field",
  );
  assertEqual(stateMod.state.showAllRepos, false, "showAllRepos should default to false");
});

await run("resetState clears showAllRepos", async () => {
  await bootstrap();
  stateMod.state.showAllRepos = true;
  stateMod.resetState();
  assertEqual(
    stateMod.state.showAllRepos,
    false,
    "resetState should reset showAllRepos to false",
  );
});

// ---------- main.js refresh(): repo-scoped plan-list URL ----------

await run("default load with a workspace selected fetches /api/plans with repo=<path>", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = false;
  await appMod.refresh();
  const url = lastPlansUrl();
  assertTrue(url !== null, "no /api/plans request was captured");
  assertEqual(queryParams(url).get("repo"), "/tmp/alpha", "repo param should equal the workspace path");
});

await run("no workspace selected falls back to all plans (no repo param)", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = null;
  stateMod.state.showAllRepos = false;
  await appMod.refresh();
  const url = lastPlansUrl();
  assertTrue(url !== null, "no /api/plans request was captured");
  assertEqual(
    queryParams(url).get("repo"),
    null,
    "repo param must be omitted when no workspace is selected",
  );
});

await run("empty-string workspace is treated as no workspace (no repo param)", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "";
  stateMod.state.showAllRepos = false;
  await appMod.refresh();
  const url = lastPlansUrl();
  assertTrue(url !== null, "no /api/plans request was captured");
  assertEqual(
    queryParams(url).get("repo"),
    null,
    "an empty workspace path must not produce a repo param",
  );
});

await run("workspace path round-trips through the repo param (space boundary)", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/my repo";
  stateMod.state.showAllRepos = false;
  await appMod.refresh();
  const url = lastPlansUrl();
  assertTrue(url !== null, "no /api/plans request was captured");
  assertEqual(
    queryParams(url).get("repo"),
    "/tmp/my repo",
    "the repo param should decode back to the exact workspace path",
  );
});

await run("include_archived=true and repo= compose in one URL", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = false;
  stateMod.state.showArchived = true;
  await appMod.refresh();
  const url = lastPlansUrl();
  assertTrue(url !== null, "no /api/plans request was captured");
  const p = queryParams(url);
  assertEqual(p.get("include_archived"), "true", "include_archived should be set");
  assertEqual(p.get("repo"), "/tmp/alpha", "repo should be set alongside include_archived");
});

// ---------- plan-list.js: the all-repos sidebar toggle ----------

await run("all-repos checkbox sits in the sidebar footer beside 'Show dismissed plans'", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = false;
  await appMod.refresh();
  const allRepos = findCheckboxByNearbyText(nav(), "all repositories");
  assertTrue(allRepos !== null, "no 'Show plans from all repositories' checkbox found in the sidebar");
  assertTrue(
    textOf(nav()).includes("Show plans from all repositories"),
    "the sidebar should label the opt-out 'Show plans from all repositories'",
  );
  const dismissed = findCheckboxByNearbyText(nav(), "Show dismissed plans");
  assertTrue(dismissed !== null, "the existing 'Show dismissed plans' checkbox should still render");
  assertTrue(
    hasAncestorClass(allRepos, "plan-list-footer") ||
      allRepos.parentNode === dismissed.parentNode,
    "the all-repos checkbox should sit alongside the dismissed-plans checkbox in the sidebar footer",
  );
  assertTrue(
    hasAncestorClass(dismissed, "plan-list-footer"),
    "the dismissed-plans checkbox should still live in the .plan-list-footer",
  );
});

await run("all-repos checkbox reflects state.showAllRepos", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = true;
  await appMod.refresh();
  const cb = findCheckboxByNearbyText(nav(), "all repositories");
  assertTrue(cb !== null, "no 'Show plans from all repositories' checkbox found");
  assertEqual(cb.checked, true, "checkbox should be checked when showAllRepos is true");

  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = false;
  await appMod.refresh();
  const cb2 = findCheckboxByNearbyText(nav(), "all repositories");
  assertTrue(cb2 !== null, "no 'Show plans from all repositories' checkbox found");
  assertEqual(cb2.checked, false, "checkbox should be unchecked when showAllRepos is false");
});

await run("checking the all-repos toggle refetches without repo; unchecking re-applies it", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = false;
  await appMod.refresh();
  const cb = findCheckboxByNearbyText(nav(), "all repositories");
  assertTrue(cb !== null, "no 'Show plans from all repositories' checkbox found");

  fetchLog = [];
  await fireChange(cb, true);
  assertEqual(stateMod.state.showAllRepos, true, "checking should flip state.showAllRepos to true");
  let url = lastPlansUrl();
  assertTrue(url !== null, "checking the toggle should trigger a refetch");
  assertEqual(
    queryParams(url).get("repo"),
    null,
    "repo param must be omitted when showing all repositories",
  );

  fetchLog = [];
  await fireChange(cb, false);
  assertEqual(stateMod.state.showAllRepos, false, "unchecking should flip state.showAllRepos back to false");
  url = lastPlansUrl();
  assertTrue(url !== null, "unchecking the toggle should trigger a refetch");
  assertEqual(queryParams(url).get("repo"), "/tmp/alpha", "repo param should be re-applied");
});

// ---------- workspace switch re-scopes the list ----------

await run("switching the active workspace refetches with the new workspace path", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = false;
  await appMod.refresh();

  const picker = doc.getElementById("workspace-picker");
  const listeners = picker._listeners.click || [];
  assertTrue(listeners.length > 0, "the workspace picker has no click listener wired");

  fetchLog = [];
  const fakeTarget = { closest: () => ({ dataset: { path: "/tmp/beta" } }) };
  for (const fn of listeners) await fn({ type: "click", target: fakeTarget });
  await flush();

  assertEqual(
    stateMod.state.selectedWorkspace,
    "/tmp/beta",
    "clicking a workspace should update state.selectedWorkspace",
  );
  const url = lastPlansUrl();
  assertTrue(url !== null, "switching workspace should trigger a plan-list refetch");
  assertEqual(
    queryParams(url).get("repo"),
    "/tmp/beta",
    "the refetch should carry the newly selected workspace path",
  );
});

await run("submitting the workspace form refetches with the new workspace path", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = false;
  await appMod.refresh();

  const form = doc.getElementById("workspace-form");
  const listeners = form._listeners.submit || [];
  assertTrue(listeners.length > 0, "the workspace form has no submit listener wired");
  doc.getElementById("workspace-path-input").value = "/tmp/gamma";
  doc.getElementById("workspace-create").checked = false;

  fetchLog = [];
  for (const fn of listeners) await fn({ type: "submit", preventDefault: noop });
  await flush();

  assertEqual(
    stateMod.state.selectedWorkspace,
    "/tmp/gamma",
    "submitting the form should update state.selectedWorkspace",
  );
  const url = lastPlansUrl();
  assertTrue(url !== null, "submitting the workspace form should trigger a plan-list refetch");
  assertEqual(
    queryParams(url).get("repo"),
    "/tmp/gamma",
    "the refetch should carry the newly selected workspace path",
  );
});

// ---------- scoped empty state ----------

await run("scoped empty state renders when a repo filter is active and there are no plans", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = false;
  plansPayload = { plans: [] };
  await appMod.refresh();
  const scopedText = textOf(nav());
  assertTrue(
    /no plans/i.test(scopedText),
    `scoped empty state should mention there are no plans; got: ${JSON.stringify(scopedText)}`,
  );
  assertTrue(
    scopedText.includes("/tmp/alpha") || /repositor/i.test(scopedText),
    "scoped empty state should name the repository",
  );
});

await run("global empty state is unchanged when the opt-out is on and there are no plans", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = true;
  plansPayload = { plans: [] };
  await appMod.refresh();
  const globalText = textOf(nav());
  assertTrue(
    !/no plans/i.test(globalText),
    `the all-repos empty state should stay blank/global; got: ${JSON.stringify(globalText)}`,
  );
});

await run("scoped empty state does not render when the repo has plans", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = false;
  plansPayload = {
    plans: [{ name: "plan-one", story_count: 1, status_counts: { done: 0 } }],
  };
  await appMod.refresh();
  const text = textOf(nav());
  assertTrue(
    !/no plans/i.test(text),
    `the scoped empty message must not render when plans exist; got: ${JSON.stringify(text)}`,
  );
  assertTrue(text.includes("plan-one"), "the plan row should still render");
});

await run("scoped empty state renders after switching to an empty repo (keyed-diff path)", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = false;
  plansPayload = {
    plans: [{ name: "plan-one", story_count: 1, status_counts: { done: 0 } }],
  };
  await appMod.refresh();
  assertTrue(textOf(nav()).includes("plan-one"), "the first plan row should render");

  // Second renderPlanList call (planListRowsByName is now populated) with an
  // empty list — the real "switched to a repo with no plans" scenario.
  plansPayload = { plans: [] };
  await appMod.refresh();
  const text = textOf(nav());
  assertTrue(
    /no plans/i.test(text),
    `the scoped empty state should render on the keyed-diff path too; got: ${JSON.stringify(text)}`,
  );
  assertTrue(
    text.includes("/tmp/alpha") || /repositor/i.test(text),
    "the scoped empty state should name the repository",
  );
});

await run("scoped empty message clears once plans return", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = false;
  plansPayload = { plans: [] };
  await appMod.refresh();
  assertTrue(/no plans/i.test(textOf(nav())), "the scoped empty state should render first");

  plansPayload = {
    plans: [{ name: "plan-two", story_count: 1, status_counts: { done: 0 } }],
  };
  await appMod.refresh();
  const text = textOf(nav());
  assertTrue(
    !/no plans/i.test(text),
    `the scoped empty message should clear once plans exist; got: ${JSON.stringify(text)}`,
  );
  assertTrue(text.includes("plan-two"), "the new plan row should render");
});

// ---------- regression: the dismissed-plans toggle is unchanged ----------

await run("'Show dismissed plans' toggle still flips showArchived and refetches", async () => {
  await bootstrap();
  reset();
  stateMod.state.selectedWorkspace = "/tmp/alpha";
  stateMod.state.showAllRepos = false;
  stateMod.state.showArchived = false;
  await appMod.refresh();

  const cb = findCheckboxByNearbyText(nav(), "Show dismissed plans");
  assertTrue(cb !== null, "the 'Show dismissed plans' checkbox should still render");
  assertEqual(cb.checked, false, "the dismissed-plans checkbox should start unchecked");

  fetchLog = [];
  await fireChange(cb, true);
  assertEqual(stateMod.state.showArchived, true, "toggling should flip state.showArchived to true");
  const url = lastPlansUrl();
  assertTrue(url !== null, "toggling dismissed plans should trigger a refetch");
  const p = queryParams(url);
  assertEqual(p.get("include_archived"), "true", "include_archived should be set");
  assertEqual(p.get("repo"), "/tmp/alpha", "the repo scope should still apply");
});

// ---------- summary ----------

const failures = results.filter((r) => !r.ok);
// eslint-disable-next-line no-console
console.log(`\n${results.length - failures.length}/${results.length} passed`);
if (failures.length) {
  // eslint-disable-next-line no-console
  console.log(`\n${failures.length} failing:`);
  for (const f of failures) {
    // eslint-disable-next-line no-console
    console.log(`  - ${f.name}: ${f.detail}`);
  }
}
process.exit(failures.length ? 1 : 0);
