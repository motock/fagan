"""Shared Node test harness for loading static/app.js in either ESM or
CommonJS-global mode, so the dashboard subprocess tests keep working as
app.js is converted from a single global file into ES modules.

Detection: if app.js contains a top-level ``import``/``export`` statement it
is loaded via Node dynamic ``import()`` (which resolves its ``./app/*``
imports through ``static/package.json`` ``{"type":"module"}`` and exposes
its named exports on ``globalThis``); otherwise the legacy synchronous
``eval(fs.readFileSync(...))`` CommonJS path is used, preserving today's
behaviour byte-for-byte. Both paths return the same ``CompletedProcess``
shape the existing call sites already consume.
"""
import json
import os
import re
import subprocess
import urllib.parse

_APP_JS_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "static", "app.js",
)

_ESM_RE = re.compile(r"^\s*(?:import|export)\s", re.MULTILINE)


def _has_esm_top_level(src: str) -> bool:
    return bool(_ESM_RE.search(src))


def run_app_js(expr, app_js=_APP_JS_DEFAULT, shim=""):
    """Load ``app_js`` (ESM or CJS-global) then evaluate ``expr`` against
    ``globalThis`` and print its JSON-serialized value on stdout.

    ``shim`` is a snippet of JS run before app.js is loaded, used to stub
    ``document``/``window``/``localStorage``/``fetch`` so app.js's top-level
    DOM wiring does not throw under Node. Returns the ``CompletedProcess``
    (call sites read ``.stdout``/``.returncode``/``.stderr`` unchanged).
    """
    with open(app_js, encoding="utf-8") as f:
        src = f.read()
    app_url = "file:" + urllib.parse.quote(os.path.abspath(app_js))
    if _has_esm_top_level(src):
        script = (
            shim
            + f"\nconst __app = await import({json.dumps(app_url)});"
            + "\nObject.assign(globalThis, __app);"
            + "\nglobalThis.module = { exports: __app };"
            + f"\nprocess.stdout.write(JSON.stringify(await eval({json.dumps(expr)})));"
        )
        return subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True, text=True, check=False,
        )
    script = (
        shim
        + f"\nconst fs=require('fs');eval(fs.readFileSync({json.dumps(app_js)},'utf8'));"
        + "\nif (globalThis.window) globalThis.state = globalThis.window.state;"
        + f"\n(async () => {{ process.stdout.write(JSON.stringify(await eval({json.dumps(expr)}))); }})();"
    )
    return subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
