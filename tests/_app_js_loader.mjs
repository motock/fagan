// Shared dual-mode Node loader for static/app.js.
//
// Mirrors tests/unit/_app_js.py: if app.js contains a top-level
// `import`/`export` statement it is loaded via Node dynamic `import()`
// (exposing its named exports on the target window/global); otherwise the
// legacy synchronous `eval(src)` CommonJS path is used, preserving today's
// behaviour byte-for-byte.
//
// The target may be a jsdom instance (whose `.window` is used) or a plain
// window stub passed directly. Both paths return the module namespace (ESM)
// or the window (CJS) so callers can pull named helpers.

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const _APP_JS = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "static",
  "app.js",
);

// Same top-level import/export detection rule the .py loader uses.
const _ESM_RE = /^\s*(?:import|export)\s/m;

export async function loadAppInto(dom, { srcOverride } = {}) {
  const src = srcOverride ?? readFileSync(_APP_JS, "utf8");
  // Accept either a jsdom instance (dom.window) or a plain window stub.
  const win = dom.window || dom;
  if (_ESM_RE.test(src)) {
    const appFileUrl = pathToFileURL(_APP_JS).href;
    // Expose browser globals on globalThis for BOTH the ESM module's
    // top-level reads AND its call-time reads. app.js functions read
    // window.location / document / localStorage / fetch when INVOKED by
    // tests, not just at import time, so these MUST STAY SET for the
    // process lifetime -- do NOT restore them in a finally block (that was
    // a prior brief's bug: it set globalThis.window back to undefined and
    // broke every call-time window.location read). Each loadAppInto call
    // re-points these at its own win. Propagate the caller's existing
    // globals onto win first so we never clobber a test's setup (e.g. a
    // test that set global.document = doc) with an undefined win stub.
    globalThis.window = win;
    for (const k of ["document", "localStorage", "fetch"]) {
      if (win[k] === undefined && globalThis[k] !== undefined) win[k] = globalThis[k];
      globalThis[k] = win[k];
    }
    const mod = await import(appFileUrl);
    Object.assign(win, mod);
    return mod;
  }
  // CJS path: evaluate inside the target window.
  win.eval(src);
  return win;
}
