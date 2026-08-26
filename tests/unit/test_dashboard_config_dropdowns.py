"""Node-eval harness tests for static/app/main.js's `loadRegistry()`.

`/model_registry.json` 404s in production because that file lives at the
repo root, outside the static mount, which is why the Config view's Roles
table provider/model <select> dropdowns only ever show the currently
configured value with no other options to pick from. The fix points
`loadRegistry()` at the new `GET /api/config/providers` endpoint (added by
a sibling story to app/dashboard.py, returning `{"providers": {...}}` --
the full catalog from model_registry.json) instead.

This file mirrors the harness pattern in
tests/unit/test_dashboard_notification_filter.py: it builds a minimal DOM
shim (copied verbatim from that file, since static/app/main.js's top-level
side effects -- theme wiring, the story-modal close/backdrop listeners, the
Config nav item, `_wireBackendSelector`, an unawaited `refresh()` call, etc.
-- all execute the moment the ESM module loads, and this shim is already
proven to satisfy all of it) and evals static/app.js (which re-exports
static/app/main.js) under `node -e`.

`loadRegistry` is a plain module-scope function, not currently exported
from static/app/main.js, so it is unreachable from a test today. Making it
callable here requires adding it to main.js's ESM export list --allowed,
since that is not "changing" the function itself, just making the existing
fix testable, and the task's own instructions are to write a test that
calls `loadRegistry()` directly.

Because `loadRegistry()` never lets an exception escape (the `try`/`catch`
inside it swallows every failure and always resolves to a plain object),
there is no thrown exception type/message to assert anywhere below --
every failure mode is instead observed through loadRegistry's *return
value*, which is what these tests assert.

These tests are RED until the implementation lands: `loadRegistry` must
(a) be exported from static/app/main.js, (b) fetch `/api/config/providers`
instead of `/model_registry.json`, and (c) unwrap the new endpoint's
`{"providers": {...}}` response shape into the `{providers: {...}}` object
callers already expect.
"""
import json
import os

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")
MAIN_JS = os.path.join(REPO_ROOT, "static", "app", "main.js")


def _main_js_source():
    """Read static/app/main.js source for static-source (rename/removal) assertions."""
    with open(MAIN_JS, encoding="utf-8") as fh:
        return fh.read()


# === DOM shim (copied verbatim from test_dashboard_notification_filter.py) ===

_SHIM = r"""
        const noop = () => {};
        const fakeEl = {
            innerHTML: "",
            classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
            addEventListener: noop,
            setAttribute: noop,
            appendChild: noop,
            querySelectorAll: () => [],
            dataset: {},
        };
        globalThis.document = {
            addEventListener: noop,
            documentElement: { dataset: {} },
            getElementById: () => ({ ...fakeEl, dataset: {}, addEventListener: noop }),
            createElement: () => ({ ...fakeEl, classList: { add: noop, remove: noop, contains: () => false } }),
        };
        globalThis.window = {
            location: { hash: "" },
            addEventListener: noop,
        };
        globalThis.localStorage = { getItem: () => null, setItem: noop };
        globalThis.fetch = () => new Promise(() => {});
        process.on("unhandledRejection", () => {});
        globalThis.setInterval = () => 0;
        globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
"""


def _run_app_js(expr):
    """Evaluate a JS expression inside an environment where static/app.js
    (and, transitively, static/app/main.js) has been loaded. Returns the
    JSON-serialized result."""
    proc = _shared_run_app_js(expr, shim=_SHIM)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _call_load_registry(fetch_impl_js):
    """Stub globalThis.fetch with `fetch_impl_js` (a JS arrow-function
    source string taking `url` and pushing it onto the closed-over `calls`
    array), then call loadRegistry() and return a dict:
      {"exported": bool, "calls": [urls fetch was invoked with], "result": <return value>}

    `exported` is False (and `result` is None) when loadRegistry is not yet
    reachable off the module's exports -- the expected RED state before the
    implementation lands. Guarding with `typeof loadRegistry === "function"`
    (rather than calling the bare identifier directly) avoids a
    ReferenceError so the test always reports a clean, readable result
    instead of erroring out or hanging on an unhandled rejection.
    """
    expr = (
        "(async () => {"
        " const calls = [];"
        f" globalThis.fetch = {fetch_impl_js};"
        ' const fn = typeof loadRegistry === "function" ? loadRegistry : null;'
        " if (!fn) return { exported: false, calls, result: null };"
        " const result = await fn();"
        " return { exported: true, calls, result };"
        " })()"
    )
    return _run_app_js(expr)


# === loadRegistry is reachable at all ==========================================

def test_load_registry_is_exported_as_function():
    """static/app/main.js must export loadRegistry so callers (and this
    test) can reach it -- today it is an unexported module-scope function."""
    assert _run_app_js("(() => typeof loadRegistry)()") == "function"


# === happy path: correct URL, correct unwrapped shape ==========================

def test_fetches_the_new_config_providers_endpoint():
    canned = {"providers": {"claude": {"models": {"sonnet": {}, "opus": {}}}}}
    fetch_impl = (
        "(url) => { calls.push(url); return Promise.resolve({ ok: true, json: async () => ("
        + json.dumps(canned)
        + ") }); }"
    )
    out = _call_load_registry(fetch_impl)
    assert out["exported"] is True, "loadRegistry must be exported from static/app/main.js"
    assert "/api/config/providers" in out["calls"]


def test_does_not_request_the_legacy_model_registry_json_path():
    canned = {"providers": {}}
    fetch_impl = (
        "(url) => { calls.push(url); return Promise.resolve({ ok: true, json: async () => ("
        + json.dumps(canned)
        + ") }); }"
    )
    out = _call_load_registry(fetch_impl)
    assert "/model_registry.json" not in out["calls"]


def test_requested_url_is_exactly_api_config_providers_no_extras():
    """Guards against a query string, trailing slash, or leftover old-path
    fallback sneaking into the request."""
    canned = {"providers": {}}
    fetch_impl = (
        "(url) => { calls.push(url); return Promise.resolve({ ok: true, json: async () => ("
        + json.dumps(canned)
        + ") }); }"
    )
    out = _call_load_registry(fetch_impl)
    assert out["calls"] == ["/api/config/providers"]


def test_calls_fetch_exactly_once():
    canned = {"providers": {}}
    fetch_impl = (
        "(url) => { calls.push(url); return Promise.resolve({ ok: true, json: async () => ("
        + json.dumps(canned)
        + ") }); }"
    )
    out = _call_load_registry(fetch_impl)
    assert len(out["calls"]) == 1


def test_returns_providers_matching_the_response_body():
    canned = {
        "providers": {
            "ollama": {"models": {"glm": {}}},
            "claude": {"models": {"sonnet": {}, "opus": {}}},
        }
    }
    fetch_impl = (
        "(url) => { calls.push(url); return Promise.resolve({ ok: true, json: async () => ("
        + json.dumps(canned)
        + ") }); }"
    )
    out = _call_load_registry(fetch_impl)
    assert out["result"]["providers"] == canned["providers"]


# === negative / boundary cases =================================================

def test_non_ok_response_returns_empty_object():
    """Mirrors the pre-existing `if (!res.ok) return {};` guard, which must
    survive unchanged."""
    fetch_impl = (
        "(url) => { calls.push(url); return Promise.resolve("
        "{ ok: false, status: 500, json: async () => ({}) }); }"
    )
    out = _call_load_registry(fetch_impl)
    assert out["result"] == {}


def test_network_error_returns_empty_object():
    """fetch() itself rejecting (e.g. offline) must be caught, not thrown."""
    fetch_impl = "(url) => { calls.push(url); return Promise.reject(new Error('network down')); }"
    out = _call_load_registry(fetch_impl)
    assert out["result"] == {}


def test_malformed_json_body_returns_empty_object():
    """res.json() throwing (malformed response body) must be caught, not thrown."""
    fetch_impl = (
        "(url) => { calls.push(url); return Promise.resolve({ ok: true, "
        "json: async () => { throw new Error('bad json'); } }); }"
    )
    out = _call_load_registry(fetch_impl)
    assert out["result"] == {}


def test_missing_providers_field_defaults_to_empty_object():
    """Response body present but with no `providers` key at all (malformed/
    unexpected shape) -- must degrade to an empty providers catalog, not throw
    or return undefined."""
    fetch_impl = (
        "(url) => { calls.push(url); return Promise.resolve({ ok: true, json: async () => ({}) }); }"
    )
    out = _call_load_registry(fetch_impl)
    assert out["result"] == {"providers": {}}


def test_null_providers_field_defaults_to_empty_object():
    fetch_impl = (
        "(url) => { calls.push(url); return Promise.resolve("
        "{ ok: true, json: async () => ({ providers: null }) }); }"
    )
    out = _call_load_registry(fetch_impl)
    assert out["result"] == {"providers": {}}


def test_empty_providers_object_is_preserved_not_dropped():
    """Boundary: an empty (but present) providers catalog is a valid,
    distinct value from "missing" -- it must round-trip as {} rather than,
    say, being treated as falsy and replaced by something else."""
    fetch_impl = (
        "(url) => { calls.push(url); return Promise.resolve("
        "{ ok: true, json: async () => ({ providers: {} }) }); }"
    )
    out = _call_load_registry(fetch_impl)
    assert out["result"] == {"providers": {}}


# === static-source assertions (the exact rename this story makes) =============

def test_source_no_longer_contains_the_legacy_model_registry_json_literal():
    src = _main_js_source()
    assert '"/model_registry.json"' not in src


def test_source_contains_the_new_api_config_providers_literal():
    src = _main_js_source()
    assert '"/api/config/providers"' in src
