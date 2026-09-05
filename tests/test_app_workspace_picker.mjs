// Tests for static/app/workspace.js — active-workspace fetch + picker
// rendering, and the workspace picker markup in static/index.html.
//
// Run with:  node tests/test_app_workspace_picker.mjs
//
// Follows the structure of tests/test_app_workspace.mjs: a tiny record/
// assert harness, per-test setup via the shared window stub bootstrapped
// through tests/_app_js_loader.mjs (which hardcodes static/app.js as its
// import target, so workspace.js is imported directly afterwards with the
// same cache-busting query the loader uses), and a final summary + exit
// code.
//
// Written test-first: until fetchActiveWorkspace and renderWorkspacePicker
// are exported from workspace.js, and the workspace-view markup exists in
// index.html, the assertions below fail — that is the expected RED state.
//
// Testable criteria covered here:
//   - fetchActiveWorkspace GETs /api/workspace and resolves body.active when
//     it is a non-empty string.
//   - fetchActiveWorkspace resolves null on a non-2xx, a network error, and
//     a malformed/absent body — it never throws (mirrors fetchWorkspaces'
//     never-throw convention).
//   - renderWorkspacePicker escapes HTML metacharacters in both the visible
//     text and the data-path attribute value.
//   - renderWorkspacePicker marks the entry whose path === activePath as
//     active, with a visible "(active)" marker.
//   - renderWorkspacePicker still marks valid:false entries as unavailable
//     (existing renderWorkspaceList convention).
//   - renderWorkspacePicker renders an explicit empty-state string (the
//     existing 'empty-state' class convention) for a non-array or [].
//   - index.html contains the workspace picker markup: #workspace-nav,
//     #workspace-view, #workspace-form, #workspace-path-input,
//     #workspace-create, #workspace-select (membership only — never
//     asserted against the full file).
//
// CUMULATIVE-ARTIFACT NOTE: workspace.js's export list is shared with
// tests/test_app_workspace.mjs, which asserts the three original exports
// (fetchWorkspaces, selectWorkspace, renderWorkspaceList) by membership.
// This file asserts only the two NEW exports by membership — never the
// total export list, export count, or a hash of either file.

import { readFileSync } from "node:fs";
import { loadAppInto } from "./_app_js_loader.mjs";

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok, detail });
  const tag = ok ? "PASS" : "FAIL";
  // eslint-disable-next-line no-console
  console.log(`${tag}  ${name}${detail ? ` — ${detail}` : ""}`);
}

function assertEqual(actual, expected, label) {
  if (actual !== expected) {
    throw new Error(`${label || "values differ"}: got ${JSON.stringify(actual)} want ${JSON.stringify(expected)}`);
  }
}

function assertTrue(cond, label) {
  if (!cond) throw new Error(label || "assertion failed");
}

// ---------- environment ----------

let currentFetch = null;

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
    // is out of scope for these pure-helper tests.
  }
}

let importSeq = 0;
let wiringImportSeq = 0;
async function loadWorkspaceModule() {
  await bootstrapGlobals();
  return import(`../static/app/workspace.js?picker=${++importSeq}`);
}

async function run(name, fn) {
  try {
    await fn();
    record(name, true);
  } catch (err) {
    record(name, false, err && err.message ? err.message : String(err));
  }
}

// ---------- exports ----------

await run("exports fetchActiveWorkspace and renderWorkspacePicker", async () => {
  const mod = await loadWorkspaceModule();
  assertTrue(
    typeof mod.fetchActiveWorkspace === "function",
    "fetchActiveWorkspace should be an exported function",
  );
  assertTrue(
    typeof mod.renderWorkspacePicker === "function",
    "renderWorkspacePicker should be an exported function",
  );
});

// ---------- fetchActiveWorkspace ----------

await run(
  "fetchActiveWorkspace GETs /api/workspace and resolves the active path on 200",
  async () => {
    const mod = await loadWorkspaceModule();
    let calledUrl = null;
    let calledOpts = null;
    currentFetch = async (url, opts) => {
      calledUrl = url;
      calledOpts = opts;
      return jsonResponse(200, { active: "/tmp/x" });
    };
    const out = await mod.fetchActiveWorkspace();
    assertEqual(String(calledUrl), "/api/workspace", "fetchActiveWorkspace should GET /api/workspace");
    assertTrue(
      calledOpts == null || calledOpts.method == null || calledOpts.method === "GET",
      "fetchActiveWorkspace should issue a GET",
    );
    assertEqual(out, "/tmp/x", "fetchActiveWorkspace should resolve the active path");
  },
);

await run("fetchActiveWorkspace resolves null on a 500", async () => {
  const mod = await loadWorkspaceModule();
  currentFetch = async () => jsonResponse(500, { detail: "boom" });
  const out = await mod.fetchActiveWorkspace(); // must not throw
  assertEqual(out, null, "a 500 should resolve to null");
});

await run("fetchActiveWorkspace resolves null when fetch rejects", async () => {
  const mod = await loadWorkspaceModule();
  currentFetch = async () => {
    throw new TypeError("Failed to fetch");
  };
  const out = await mod.fetchActiveWorkspace(); // must not throw
  assertEqual(out, null, "a rejected fetch should resolve to null");
});

await run("fetchActiveWorkspace resolves null for a body without active", async () => {
  const mod = await loadWorkspaceModule();
  currentFetch = async () => jsonResponse(200, {});
  assertEqual(
    await mod.fetchActiveWorkspace(),
    null,
    "a 200 body without an active field should resolve to null",
  );

  currentFetch = async () => jsonResponse(200, { active: null });
  assertEqual(
    await mod.fetchActiveWorkspace(),
    null,
    "active: null should resolve to null",
  );

  currentFetch = async () => jsonResponse(200, { active: "" });
  assertEqual(
    await mod.fetchActiveWorkspace(),
    null,
    "active: '' (empty string) should resolve to null, not a falsy-but-truthy string",
  );
});

// ---------- renderWorkspacePicker ----------

await run("renderWorkspacePicker escapes HTML metacharacters in text and data-path", async () => {
  const mod = await loadWorkspaceModule();
  const rawPath = `/tmp/a<b>&c"d'e`;
  const html = mod.renderWorkspacePicker([{ path: rawPath, valid: true }], null);
  assertTrue(typeof html === "string", "renderWorkspacePicker should return a string");
  assertTrue(!html.includes(rawPath), "the raw path must not appear unescaped anywhere");
  assertTrue(html.includes("&lt;b&gt;"), "< and > should be escaped in the visible text");
  assertTrue(html.includes("&amp;"), "& should be escaped");
  assertTrue(html.includes("&quot;"), "double quote should be escaped");
  assertTrue(
    html.includes("&#39;") || html.includes("&#x27;"),
    "single quote should be escaped",
  );
  assertTrue(
    /data-path="[^"]*"/.test(html),
    "each entry should carry a data-path attribute with an escaped value",
  );
});

await run("renderWorkspacePicker marks the active entry with class and marker", async () => {
  const mod = await loadWorkspaceModule();
  const html = mod.renderWorkspacePicker(
    [
      { path: "/tmp/alpha", valid: true },
      { path: "/tmp/beta", valid: true },
    ],
    "/tmp/beta",
  );
  assertTrue(html.includes("/tmp/alpha"), "the non-active entry should still render");
  assertTrue(html.includes("/tmp/beta"), "the active entry should render");
  assertTrue(html.includes("active"), "the active entry should carry an 'active' marking");
  assertTrue(
    html.toLowerCase().includes("(active)"),
    "the active entry should carry a visible (active) marker",
  );
  // The non-active entry must not itself claim to be active.
  const betaIndex = html.indexOf("/tmp/beta");
  const alphaIndex = html.indexOf("/tmp/alpha");
  assertTrue(betaIndex !== -1 && alphaIndex !== -1, "both entries present");
});

await run("renderWorkspacePicker still marks valid:false entries as unavailable", async () => {
  const mod = await loadWorkspaceModule();
  const html = mod.renderWorkspacePicker(
    [
      { path: "/tmp/good", valid: true },
      { path: "/tmp/gone", valid: false },
    ],
    null,
  );
  assertTrue(html.includes("/tmp/good"), "the valid entry should still be rendered");
  assertTrue(html.includes("/tmp/gone"), "the invalid entry must still be included");
  assertTrue(
    html.toLowerCase().includes("unavailable"),
    "the invalid entry should be visibly marked unavailable",
  );
});

await run("renderWorkspacePicker renders an explicit empty-state for [] and non-arrays", async () => {
  const mod = await loadWorkspaceModule();
  const html = mod.renderWorkspacePicker([], null);
  assertTrue(typeof html === "string", "renderWorkspacePicker should return a string for []");
  assertTrue(
    html.includes("empty-state"),
    "the empty state should use the existing 'empty-state' class convention",
  );

  const htmlNonArray = mod.renderWorkspacePicker(undefined, null);
  assertTrue(
    htmlNonArray.includes("empty-state"),
    "a non-array input should also yield the empty-state string",
  );
});

// ---------- index.html markup ----------

await run("index.html contains the workspace picker markup", async () => {
  const html = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");
  for (const id of [
    "workspace-view",
    "workspace-nav",
    "workspace-form",
    "workspace-path-input",
    "workspace-create",
    "workspace-select",
  ]) {
    assertTrue(
      html.includes(`id="${id}"`),
      `index.html should contain an element with id="${id}"`,
    );
  }
});

// ---------- wiring: nav click, picker render, select success/failure ----------
//
// These tests drive the wiring added to static/app/main.js: clicking
// #workspace-nav, submitting #workspace-form, and clicking a rendered
// picker entry. They need a document stub whose elements are STATEFUL
// (the same object returned across repeated getElementById calls, with a
// working classList/addEventListener/innerHTML) rather than the fresh-
// element-per-call stub used above, so main.js's attached listeners are
// observable and dispatchable. Bootstrapped by pointing the browser globals
// at the wiring window and importing main.js with a unique cache-busting
// query — NOT via the shared loadAppInto()/bootstrapGlobals() above, whose
// app.js-only cache-bust would reuse the helper phase's main.js instance
// (its listeners are bound to throwaway elements, and a failed helper-phase
// evaluation would poison the bare-URL module for the whole process), so
// wireWorkspaceView() wires listeners onto THESE tracked elements.
//
// Asserts only this story's additions (state.workspaceActive, the
// workspace-nav/workspace-form/workspace-picker wiring) — never the total
// contents of main.js or state.js.

function makeTrackedElement(id) {
  const classes = new Set();
  const listeners = {};
  const el = {
    id,
    style: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c, v) => (v === undefined ? (classes.has(c) ? classes.delete(c) : classes.add(c)) : (v ? classes.add(c) : classes.delete(c))),
      contains: (c) => classes.has(c),
    },
    addEventListener: (evt, fn) => {
      (listeners[evt] = listeners[evt] || []).push(fn);
    },
    removeEventListener: () => {},
    // Invokes all listeners for evt and awaits any returned promises, so
    // tests can `await dispatch(...)` and observe the handler's effects.
    dispatch: (evt, evtObj) => Promise.all((listeners[evt] || []).map((fn) => fn(evtObj))),
    appendChild: (child) => {
      el.children.push(child);
      return child;
    },
    setAttribute: () => {},
    querySelector: () => null,
    querySelectorAll: () => [],
    children: [],
    innerHTML: "",
    textContent: "",
    value: "",
    checked: false,
    dataset: {},
    closest: () => el,
  };
  return el;
}

const WIRING_TRACKED_IDS = [
  "workspace-nav", "workspace-view", "workspace-picker", "workspace-form",
  "workspace-path-input", "workspace-create", "comms-view", "plan-detail", "config-view",
];

async function bootstrapWiringApp() {
  const elements = new Map();
  for (const id of WIRING_TRACKED_IDS) elements.set(id, makeTrackedElement(id));
  const noop = () => {};
  let fetchImpl = async (url) => {
    if (String(url).includes("/api/plans")) return jsonResponse(200, { plans: [] });
    if (String(url).includes("/api/usage")) return jsonResponse(200, { available: false });
    return jsonResponse(200, {});
  };
  const doc = {
    getElementById: (id) => elements.get(id) || makeTrackedElement(`fallback:${id}`),
    createElement: () => makeTrackedElement("created"),
    querySelector: () => null,
    querySelectorAll: () => [],
    getElementsByClassName: () => [],
    getElementsByTagName: () => [],
    body: makeTrackedElement("body"),
    documentElement: makeTrackedElement("documentElement"),
    head: makeTrackedElement("head"),
    title: "",
    addEventListener: noop,
    removeEventListener: noop,
  };
  const win = {
    fetch: (url, opts) => fetchImpl(url, opts),
    document: doc,
    localStorage: { getItem: () => null, setItem: noop, removeItem: noop, clear: noop },
    location: { href: "http://localhost/", pathname: "/", search: "", hash: "" },
    matchMedia: () => ({ matches: false, addListener: noop, removeListener: noop }),
    addEventListener: noop,
    removeEventListener: noop,
    requestAnimationFrame: () => 0,
  };
  win.window = win;
  win.self = win;
  // Mirror _app_js_loader.mjs's global wiring (these must stay set for the
  // process lifetime), then force a FRESH main.js evaluation bound to this
  // test's tracked elements — see the comment above.
  globalThis.window = win;
  for (const k of ["document", "localStorage", "fetch"]) globalThis[k] = win[k];
  await import(`../static/app/main.js?wiring=${++wiringImportSeq}`);
  const { state } = await import("../static/app/state.js");
  return {
    elements,
    state,
    setFetch: (fn) => {
      fetchImpl = fn;
    },
  };
}

await run("state.workspaceActive exists and defaults to false", async () => {
  const { state } = await bootstrapWiringApp();
  assertTrue(
    Object.prototype.hasOwnProperty.call(state, "workspaceActive"),
    "state should have an own workspaceActive property",
  );
  assertEqual(state.workspaceActive, false, "state.workspaceActive default");
});

await run(
  "clicking #workspace-nav sets workspaceActive, shows the view, and populates the picker",
  async () => {
    const { elements, state, setFetch } = await bootstrapWiringApp();
    setFetch(async (url) => {
      if (String(url).includes("/api/workspaces")) {
        return jsonResponse(200, { workspaces: [{ path: "/tmp/alpha", valid: true }] });
      }
      if (String(url).includes("/api/workspace")) {
        return jsonResponse(200, { active: "/tmp/alpha" });
      }
      return jsonResponse(200, {});
    });
    const nav = elements.get("workspace-nav");
    const workspaceView = elements.get("workspace-view");
    await nav.dispatch("click");
    assertEqual(state.workspaceActive, true, "workspaceActive should be set true by the nav click");
    assertTrue(
      workspaceView.classList.contains("hidden") === false,
      "getElementById('workspace-view') should be shown (hidden class removed)",
    );
    const picker = elements.get("workspace-picker");
    assertTrue(
      picker.innerHTML.includes("/tmp/alpha"),
      "the picker should be populated via renderWorkspacePicker output",
    );
    assertTrue(
      picker.innerHTML.toLowerCase().includes("(active)"),
      "the active workspace should carry the active marker",
    );
  },
);

// Regression: the workspace nav handler sets state.workspaceActive = true,
// but every OTHER nav function must clear it (the established invariant is
// that each nav function clears every view flag it does not set). Otherwise
// one visit to the workspace view leaks workspaceActive = true forever and
// _applyActiveView — which checks workspaceActive before commsActive and
// plan-detail — shows the workspace picker instead of plan detail or the
// overview landing on every subsequent navigation.
await run(
  "selectPlan and selectOverview clear a stale workspaceActive so navigation is not hijacked",
  async () => {
    const { elements, state } = await bootstrapWiringApp();
    const main = await import(`../static/app/main.js?wiring=${wiringImportSeq}`);
    const planDetail = elements.get("plan-detail");
    const workspaceView = elements.get("workspace-view");
    const commsView = elements.get("comms-view");
    const configView = elements.get("config-view");

    // Seed the stale flag exactly as the wireWorkspaceView nav handler does.
    await elements.get("workspace-nav").dispatch("click");
    assertEqual(state.workspaceActive, true, "precondition: nav click sets workspaceActive");

    // Follow-up navigation: clicking a plan must clear the flag and land on
    // the plan-detail branch, not the workspace branch.
    await main.selectPlan("plan-42");
    assertEqual(
      state.workspaceActive,
      false,
      "selectPlan must clear workspaceActive",
    );
    assertTrue(
      planDetail.classList.contains("hidden") === false,
      "plan detail should be shown after selectPlan",
    );
    assertTrue(
      workspaceView.classList.contains("hidden") === true,
      "workspace view must be hidden after selectPlan",
    );

    // Follow-up navigation: Overview must also clear the flag (idempotently)
    // and land on the overview landing (every view element hidden).
    await main.selectOverview();
    assertEqual(
      state.workspaceActive,
      false,
      "selectOverview must clear workspaceActive",
    );
    assertTrue(
      workspaceView.classList.contains("hidden") === true,
      "workspace view must stay hidden after selectOverview",
    );
    // On the overview landing the overview is rendered INTO #plan-detail
    // (refresh's no-plan branch), so plan-detail is the visible host element.
    assertTrue(
      planDetail.classList.contains("hidden") === false,
      "plan-detail (hosting the overview) should be shown on the overview landing",
    );
    assertTrue(
      commsView.classList.contains("hidden") === true,
      "comms view must be hidden on the overview landing",
    );
    assertTrue(
      configView.classList.contains("hidden") === true,
      "config view must be hidden on the overview landing",
    );
  },
);

await run(
  "a successful selectWorkspace (form submit) updates state and re-renders with the active marker",
  async () => {
    const { elements, state, setFetch } = await bootstrapWiringApp();
    let selectCalls = 0;
    setFetch(async (url, opts) => {
      const u = String(url);
      if (u.includes("/api/workspaces")) {
        return jsonResponse(200, { workspaces: [{ path: "/tmp/beta", valid: true }] });
      }
      if (u === "/api/workspace" && (!opts || !opts.method || opts.method === "GET")) {
        return jsonResponse(200, { active: "/tmp/beta" });
      }
      if (u === "/api/workspace" && opts && opts.method === "POST") {
        selectCalls += 1;
        return jsonResponse(200, { ok: true, path: "/tmp/beta", error: null });
      }
      return jsonResponse(200, {});
    });
    const input = elements.get("workspace-path-input");
    input.value = "/tmp/beta";
    const form = elements.get("workspace-form");
    await form.dispatch("submit", { preventDefault: () => {} });
    assertEqual(selectCalls, 1, "selectWorkspace should POST to /api/workspace once");
    assertEqual(state.selectedWorkspace, "/tmp/beta", "state.selectedWorkspace should be updated on success");
    const picker = elements.get("workspace-picker");
    assertTrue(
      picker.innerHTML.toLowerCase().includes("(active)"),
      "re-render after a successful select should mark the workspace active",
    );
  },
);

await run(
  "a failed selectWorkspace (form submit) surfaces the escaped error and leaves state unchanged",
  async () => {
    const { elements, state, setFetch } = await bootstrapWiringApp();
    setFetch(async (url, opts) => {
      const u = String(url);
      if (u.includes("/api/workspaces")) return jsonResponse(200, { workspaces: [] });
      if (u === "/api/workspace" && (!opts || !opts.method || opts.method === "GET")) {
        return jsonResponse(200, {});
      }
      if (u === "/api/workspace" && opts && opts.method === "POST") {
        return jsonResponse(400, { detail: `bad <path> for "you"` });
      }
      return jsonResponse(200, {});
    });
    const input = elements.get("workspace-path-input");
    input.value = "/not/a/repo";
    const form = elements.get("workspace-form");
    await form.dispatch("submit", { preventDefault: () => {} });
    assertEqual(
      state.selectedWorkspace,
      null,
      "a failed select must not change state.selectedWorkspace",
    );
    const formChild = form.children.find((c) => c.id === "workspace-error");
    assertTrue(formChild != null, "a #workspace-error element should be created inside the form");
    assertTrue(
      !formChild.innerHTML.includes("<path>"),
      "the error message must be HTML-escaped, not injected raw",
    );
    assertTrue(
      formChild.innerHTML.includes("&lt;path&gt;"),
      "the escaped error text should be present",
    );
  },
);

await run(
  "clicking a picker item with data-path selects that workspace",
  async () => {
    const { elements, state, setFetch } = await bootstrapWiringApp();
    let postedBody = null;
    setFetch(async (url, opts) => {
      const u = String(url);
      if (u.includes("/api/workspaces")) return jsonResponse(200, { workspaces: [] });
      if (u === "/api/workspace" && (!opts || !opts.method || opts.method === "GET")) {
        return jsonResponse(200, {});
      }
      if (u === "/api/workspace" && opts && opts.method === "POST") {
        postedBody = JSON.parse(opts.body);
        return jsonResponse(200, { ok: true, path: postedBody.path, error: null });
      }
      return jsonResponse(200, {});
    });
    const picker = elements.get("workspace-picker");
    const item = makeTrackedElement("li");
    item.dataset.path = "/tmp/gamma";
    await picker.dispatch("click", { target: item });
    assertTrue(postedBody != null, "selectWorkspace should have POSTed");
    assertEqual(postedBody.path, "/tmp/gamma", "the clicked item's data-path should be selected");
    assertEqual(postedBody.create, false, "picker-item selection should never pass create:true");
    assertEqual(state.selectedWorkspace, "/tmp/gamma", "state.selectedWorkspace should update");
  },
);

// ---------- summary ----------

const failed = results.filter((r) => !r.ok).length;
// eslint-disable-next-line no-console
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed === 0 ? 0 : 1);
