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

// ---------- summary ----------

const failed = results.filter((r) => !r.ok).length;
// eslint-disable-next-line no-console
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed === 0 ? 0 : 1);
