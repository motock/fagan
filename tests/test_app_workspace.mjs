// Tests for static/app/workspace.js — workspace recents + selection.
//
// Run with:  node tests/test_app_workspace.mjs
//
// Follows the structure of tests/test_app_hash.mjs: a tiny record/assert
// harness, per-test setup, and a final summary + exit code.
//
// Two deliberate deviations from test_app_hash.mjs, both forced:
//   1. jsdom is not installed in this repo, and workspace.js is specified
//      as pure HTTP + HTML-string helpers (same conventions as
//      static/app/api.js: module-level functions, named exports at the
//      bottom, fetch for HTTP), so a plain window stub is enough.
//   2. tests/_app_js_loader.mjs hardcodes static/app.js as its import
//      target, so it cannot load workspace.js directly. We still bootstrap
//      the browser globals THROUGH the shared loader exactly like the
//      other frontend tests (the loader installs globalThis.window /
//      document / localStorage / fetch before its own import and keeps
//      them set for call-time reads — see its comments), then import the
//      module under test directly with the same cache-busting query the
//      loader uses.
//
// Written test-first: until static/app/workspace.js exists every test
// fails with ERR_MODULE_NOT_FOUND for workspace.js, which is the expected
// RED state.
//
// Testable criteria covered here:
//   - fetchWorkspaces GETs /api/workspaces and resolves the workspaces
//     array on a canned 200.
//   - fetchWorkspaces resolves [] on a non-2xx (500), when fetch rejects
//     (network error), and for malformed 200 bodies — it never throws.
//   - selectWorkspace POSTs {path, create} to /api/workspace (create:false
//     is falsy but must still be sent) and resolves the parsed result on
//     2xx.
//   - selectWorkspace surfaces the server's detail as `error` on a 400,
//     still yields a string error when detail is absent, and never throws
//     on a network error or an unparseable error body.
//   - renderWorkspaceList escapes all HTML metacharacters in interpolated
//     paths, visibly marks valid:false entries as unavailable while still
//     including them, does not mark entries missing the valid field, and
//     renders an explicit empty state for [].

import { readFileSync } from "node:fs";
import { loadAppInto } from "./_app_js_loader.mjs";

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok, detail });
  const tag = ok ? "PASS" : "FAIL";
  // eslint-disable-next-line no-console
  console.log(`${tag}  ${name}${detail ? ` — ${detail}` : ""}`);
}

// Key-order-insensitive deep equality (JSON round-trip over sorted keys)
// so a caller-built request body compares equal regardless of the order
// the implementation serialized its properties in.
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

// ---------- environment ----------

// Swapped out per test; the window stub's fetch delegates to whatever is
// currently installed, so globalThis.fetch (wired by the loader) always
// reaches the active handler.
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

// Bootstrap the browser globals (window/document/localStorage/fetch)
// through the shared loader, exactly as the other frontend tests do. The
// loader also imports the full static/app.js graph; that graph's top-level
// DOM wiring may need a fuller environment than this stub provides, but
// the globals are installed BEFORE that import and persist for call-time
// reads (the loader's own comments mandate this), which is all that
// workspace.js's bare `fetch` calls need.
let bootstrapped = false;
async function bootstrapGlobals() {
  if (bootstrapped) return;
  bootstrapped = true;
  currentFetch = async () => jsonResponse(200, {});
  try {
    await loadAppInto(makeWindowStub());
  } catch {
    // Globals are already wired at this point; the app graph's DOM wiring
    // is out of scope for these pure-helper tests.
  }
}

// Cache-busted re-import per test, mirroring the loader's own dynamic
// import() strategy so a stateful implementation cannot leak across tests.
let importSeq = 0;
async function loadWorkspaceModule() {
  await bootstrapGlobals();
  return import(`../static/app/workspace.js?t=${++importSeq}`);
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

await run("exports fetchWorkspaces, selectWorkspace and renderWorkspaceList", async () => {
  const mod = await loadWorkspaceModule();
  assertTrue(
    typeof mod.fetchWorkspaces === "function",
    "fetchWorkspaces should be an exported function",
  );
  assertTrue(
    typeof mod.selectWorkspace === "function",
    "selectWorkspace should be an exported function",
  );
  assertTrue(
    typeof mod.renderWorkspaceList === "function",
    "renderWorkspaceList should be an exported function",
  );
});

await run("workspace.js uses fetch for HTTP (api.js convention)", async () => {
  const src = readFileSync(
    new URL("../static/app/workspace.js", import.meta.url),
    "utf8",
  );
  assertTrue(/\bfetch\s*\(/.test(src), "workspace.js should issue HTTP via fetch");
});

// ---------- fetchWorkspaces ----------

await run(
  "fetchWorkspaces GETs /api/workspaces and returns the workspaces array on 200",
  async () => {
    const mod = await loadWorkspaceModule();
    const canned = [
      { path: "/tmp/alpha", valid: true },
      { path: "/tmp/beta", valid: false },
    ];
    let calledUrl = null;
    let calledOpts = null;
    currentFetch = async (url, opts) => {
      calledUrl = url;
      calledOpts = opts;
      return jsonResponse(200, { workspaces: canned });
    };
    const out = await mod.fetchWorkspaces();
    assertEqual(
      String(calledUrl),
      "/api/workspaces",
      "fetchWorkspaces should GET /api/workspaces",
    );
    assertTrue(
      calledOpts == null || calledOpts.method == null || calledOpts.method === "GET",
      "fetchWorkspaces should issue a GET",
    );
    assertTrue(
      calledOpts == null || calledOpts.body == null,
      "fetchWorkspaces should not send a body",
    );
    assertEqual(out, canned, "fetchWorkspaces should return the workspaces array");
  },
);

await run("fetchWorkspaces returns [] on a 500", async () => {
  const mod = await loadWorkspaceModule();
  currentFetch = async () => jsonResponse(500, { detail: "boom" });
  const out = await mod.fetchWorkspaces(); // must not throw
  assertEqual(out, [], "a 500 should resolve to []");
});

await run("fetchWorkspaces returns [] when fetch rejects", async () => {
  const mod = await loadWorkspaceModule();
  currentFetch = async () => {
    throw new TypeError("Failed to fetch");
  };
  const out = await mod.fetchWorkspaces(); // must not throw
  assertEqual(out, [], "a rejected fetch should resolve to []");
});

await run("fetchWorkspaces tolerates malformed 200 bodies", async () => {
  const mod = await loadWorkspaceModule();
  currentFetch = async () => jsonResponse(200, {});
  assertTrue(
    Array.isArray(await mod.fetchWorkspaces()),
    "a 200 without a workspaces key should still resolve to an array",
  );
  currentFetch = async () => jsonResponse(200, { workspaces: null });
  assertTrue(
    Array.isArray(await mod.fetchWorkspaces()),
    "a null workspaces value should still resolve to an array",
  );
  currentFetch = async () => jsonResponse(200, { workspaces: [] });
  assertEqual(
    await mod.fetchWorkspaces(),
    [],
    "an empty workspaces array should pass through",
  );
});

// ---------- selectWorkspace ----------

await run("selectWorkspace POSTs path and create in the body to /api/workspace", async () => {
  const mod = await loadWorkspaceModule();
  const canned = { ok: true, path: "/tmp/alpha" };
  let calledUrl = null;
  let calledOpts = null;
  currentFetch = async (url, opts) => {
    calledUrl = url;
    calledOpts = opts;
    return jsonResponse(200, canned);
  };
  const out = await mod.selectWorkspace("/tmp/alpha", true);
  assertEqual(
    String(calledUrl),
    "/api/workspace",
    "selectWorkspace should POST /api/workspace",
  );
  assertEqual(calledOpts && calledOpts.method, "POST", "selectWorkspace should use POST");
  assertTrue(
    Boolean(calledOpts) && typeof calledOpts.body === "string",
    "selectWorkspace should send a JSON body",
  );
  const body = JSON.parse(calledOpts.body);
  assertEqual(body, { path: "/tmp/alpha", create: true }, "body should carry path and create");
  assertEqual(out, canned, "selectWorkspace should return the parsed result on 2xx");

  // Boundary: create:false is falsy but must still be sent in the body.
  let body2 = null;
  currentFetch = async (_url, opts) => {
    body2 = JSON.parse(opts.body);
    return jsonResponse(200, canned);
  };
  await mod.selectWorkspace("/tmp/alpha", false);
  assertTrue(Boolean(body2) && "create" in body2, "create:false must still be present in the body");
  assertEqual(body2.create, false, "create:false should be serialized as false");
  assertEqual(body2.path, "/tmp/alpha", "path should be sent alongside create:false");
});

await run("selectWorkspace surfaces the server's detail as error on a 400", async () => {
  const mod = await loadWorkspaceModule();
  currentFetch = async () => jsonResponse(400, { detail: "not a directory" });
  const out = await mod.selectWorkspace("/tmp/nope", false); // must not throw
  assertTrue(Boolean(out) && typeof out === "object", "a 400 should resolve to an object");
  assertEqual(out.error, "not a directory", "error should carry the server's detail");
  assertTrue(typeof out.error === "string", "error should be a string");

  // A non-2xx without a detail still yields a string error.
  currentFetch = async () => jsonResponse(500, {});
  const out2 = await mod.selectWorkspace("/tmp/nope", false);
  assertTrue(
    Boolean(out2) && typeof out2.error === "string",
    "a non-2xx without detail should still carry a string error",
  );
});

await run("selectWorkspace does not throw on a network error", async () => {
  const mod = await loadWorkspaceModule();
  currentFetch = async () => {
    throw new TypeError("Failed to fetch");
  };
  const out = await mod.selectWorkspace("/tmp/nope", true); // must not throw
  assertTrue(out !== undefined && out !== null, "a network error should still resolve to a value");
});

await run("selectWorkspace does not throw when the error body is unparseable", async () => {
  const mod = await loadWorkspaceModule();
  currentFetch = async () => ({
    ok: false,
    status: 400,
    json: async () => {
      throw new SyntaxError("Unexpected token in JSON");
    },
  });
  const out = await mod.selectWorkspace("/tmp/nope", false); // must not throw
  assertTrue(
    Boolean(out) && typeof out.error === "string",
    "an unparseable non-2xx body should still resolve to an object with a string error",
  );
});

// ---------- renderWorkspaceList ----------

await run("renderWorkspaceList escapes HTML metacharacters in a path", async () => {
  const mod = await loadWorkspaceModule();
  const rawPath = `/tmp/a<b>&c"d'e`;
  const html = mod.renderWorkspaceList([{ path: rawPath, valid: true }]);
  assertTrue(typeof html === "string", "renderWorkspaceList should return a string");
  assertTrue(!html.includes(rawPath), "the raw path must not appear unescaped");
  assertTrue(html.includes("&lt;b&gt;"), "< and > should be escaped");
  assertTrue(html.includes("&amp;"), "& should be escaped");
  assertTrue(html.includes("&quot;"), 'double quote should be escaped');
  assertTrue(
    html.includes("&#39;") || html.includes("&#x27;"),
    "single quote should be escaped",
  );
});

await run(
  "renderWorkspaceList marks a valid:false entry as unavailable and still includes it",
  async () => {
    const mod = await loadWorkspaceModule();
    const html = mod.renderWorkspaceList([
      { path: "/tmp/good", valid: true },
      { path: "/tmp/gone", valid: false },
    ]);
    assertTrue(typeof html === "string", "renderWorkspaceList should return a string");
    assertTrue(html.includes("/tmp/good"), "the valid entry should still be rendered");
    assertTrue(
      html.includes("/tmp/gone"),
      "the invalid entry must still be included, not omitted",
    );
    assertTrue(
      html.toLowerCase().includes("unavailable"),
      "the invalid entry should be visibly marked unavailable",
    );

    // Boundary: a list containing only the invalid entry behaves the same.
    const only = mod.renderWorkspaceList([{ path: "/tmp/gone", valid: false }]);
    assertTrue(only.includes("/tmp/gone"), "a lone invalid entry should still be rendered");
    assertTrue(
      only.toLowerCase().includes("unavailable"),
      "a lone invalid entry should be marked unavailable",
    );
  },
);

await run("renderWorkspaceList does not mark entries missing the valid field", async () => {
  const mod = await loadWorkspaceModule();
  const html = mod.renderWorkspaceList([{ path: "/tmp/unknown" }]);
  assertTrue(
    html.includes("/tmp/unknown"),
    "an entry without a valid field should still render",
  );
  assertTrue(
    !html.toLowerCase().includes("unavailable"),
    "a missing valid field should not be treated as valid:false",
  );
});

await run("renderWorkspaceList renders an explicit empty state for []", async () => {
  const mod = await loadWorkspaceModule();
  const html = mod.renderWorkspaceList([]);
  assertTrue(typeof html === "string", "renderWorkspaceList should return a string for []");
  assertTrue(html.trim().length > 0, "an empty list should not render an empty result");
  assertTrue(
    /empty|no workspaces/i.test(html),
    "the empty state should say so explicitly",
  );
});

// ---------- summary ----------

const failed = results.filter((r) => !r.ok).length;
// eslint-disable-next-line no-console
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed === 0 ? 0 : 1);