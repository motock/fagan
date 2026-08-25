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
    const mod = await import(appFileUrl);
    // Expose the browser globals app.js's top-level code reads (window,
    // document, localStorage, fetch) on globalThis so the ESM module
    // evaluates under Node. Mirror the .py loader's shim. These stay set
    // for the lifetime of the process because module functions reference
    // them at call time (e.g. updateHash reads window.location), not just
    // at import time; each loadAppInto call re-points them at its own win.
    globalThis.window = win;
    for (const k of ["document", "localStorage", "fetch"]) {
      if (win[k] === undefined && globalThis[k] !== undefined) win[k] = globalThis[k];
      globalThis[k] = win[k];
    }
    const mod = await import(appFileUrl);
    Object.assign(win, mod);
    return mod;
  }
  // CJS path (today): evaluate inside the target window.
  win.eval(src);
  return win;
}
