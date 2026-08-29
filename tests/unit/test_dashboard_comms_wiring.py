"""Regression guards for the Comms header buttons and multi-call send state.

Written for the review findings on the TRACE-toggle branch:

Blocking 1: the top-of-file TRACE hunk REPLACED `let commsHistory = [];`
(base line 4 of static/app/comms.js) with the showTrace declaration
instead of adding alongside it. comms.js is an ES module (strict mode),
so the first touch of commsHistory throws
`ReferenceError: commsHistory is not defined`; sendCommsMessage's catch
swallows it and renders a fake "denied" tower bubble, and
resetCommsThread throws.

Blocking 2: the second hunk REPLACED the #comms-reset / #comms-export
click wiring (base lines: commsResetBtn/commsExportBtn lookups plus
addEventListener('click', resetCommsThread / exportCommsThread)) with the
trace-toggle wiring, leaving both existing header buttons dead.

The harness pattern is copied from tests/unit/test_dashboard_comms_send.py
(same shared loader, same DOM shim style) with additions IN THIS FILE ONLY:
a listener recorder for #comms-reset/#comms-export, a fetch log capturing
url+body, and innerHTML/style recorders on #comms-thread so the reset and
export handlers' observable effects can be asserted.

Both deletions ( Blocking 1 and Blocking 2 ) have been restored in
static/app/comms.js; these tests are permanent regression guards for
that restoration.
"""
import json
import os

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMS_JS = os.path.join(REPO_ROOT, "static", "app", "comms.js")


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
        // Recorders so tests can observe message appends, thread DOM mutations,
        // and the header-button click wiring without a real DOM.
        globalThis.__commsThread = {
            appendChildCalls: 0,
            appendedClasses: [],
            innerHTMLSets: [],
            styleDisplays: [],
            childrenRef: null,
        };
        globalThis.__commsLanding = { styleCalls: [] };
        globalThis.__onAir = { classListAdd: [], classListRemove: [] };
        globalThis.__commsSend = { disabledSet: [], classListAdd: [], classListRemove: [] };
        globalThis.__wiring = { reset: [], export: [] };
        globalThis.__createdTags = [];
        globalThis.__anchorClicks = 0;
        const commsThreadEl = {
            ...fakeEl,
            dataset: {},
            addEventListener: noop,
            children: [],
            set innerHTML(v) { globalThis.__commsThread.innerHTMLSets.push(v); },
            get innerHTML() { return ""; },
            style: {
                set display(v) { globalThis.__commsThread.styleDisplays.push(v); },
                get display() { return ""; },
            },
            appendChild: function (child) {
                globalThis.__commsThread.appendChildCalls += 1;
                if (child && typeof child.className === "string") {
                    globalThis.__commsThread.appendedClasses.push(child.className);
                }
                return child;
            },
        };
        globalThis.__commsThread.childrenRef = commsThreadEl.children;
        const commsLandingEl = {
            ...fakeEl,
            dataset: {},
            style: { display: "", set display(v) { globalThis.__commsLanding.styleCalls.push(v); }, get display() { return ""; } },
            classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
        };
        const onAirEl = {
            ...fakeEl,
            dataset: {},
            classList: {
                add: function (c) { globalThis.__onAir.classListAdd.push(c); },
                remove: function (c) { globalThis.__onAir.classListRemove.push(c); },
                toggle: noop, contains: () => false,
            },
        };
        const commsSendEl = {
            ...fakeEl,
            dataset: {},
            disabled: false,
            set disabled(v) { globalThis.__commsSend.disabledSet.push(v); },
            get disabled() { return false; },
            classList: {
                add: function (c) { globalThis.__commsSend.classListAdd.push(c); },
                remove: function (c) { globalThis.__commsSend.classListRemove.push(c); },
                toggle: noop, contains: () => false,
            },
        };
        const commsInputEl = {
            ...fakeEl,
            dataset: {},
            value: "",
            addEventListener: noop,
        };
        globalThis.document = {
            addEventListener: noop,
            documentElement: { dataset: {} },
            body: {
                appendChild: noop,
                removeChild: noop,
                classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
                dataset: {},
            },
            querySelectorAll: () => [],
            getElementById: function (id) {
                if (id === "comms-thread") return commsThreadEl;
                if (id === "comms-landing") return commsLandingEl;
                if (id === "on-air") return onAirEl;
                if (id === "comms-send") return commsSendEl;
                if (id === "comms-input") return commsInputEl;
                if (id === "comms-reset" || id === "comms-export") {
                    // Fresh element per lookup, but every addEventListener call
                    // is recorded so tests can assert the click wiring exists
                    // and dispatch the recorded handler.
                    const key = id === "comms-reset" ? "reset" : "export";
                    return {
                        ...fakeEl,
                        dataset: {},
                        disabled: false,
                        style: { display: "" },
                        addEventListener: function (type, fn) {
                            globalThis.__wiring[key].push({ type: type, fn: fn });
                        },
                    };
                }
                return { ...fakeEl, dataset: {}, addEventListener: noop, children: [] };
            },
            createElement: function (tag) {
                globalThis.__createdTags.push(tag);
                return {
                    tagName: tag,
                    innerHTML: "",
                    textContent: "",
                    className: "",
                    href: "",
                    download: "",
                    classList: {
                        add: noop, remove: noop,
                        toggle: function (c) { this._toggled = c; },
                        contains: () => false,
                    },
                    addEventListener: noop,
                    setAttribute: noop,
                    click: function () { globalThis.__anchorClicks += 1; },
                    appendChild: function (child) { return child; },
                    querySelectorAll: () => [],
                    dataset: {},
                    style: { display: "" },
                };
            },
        };
        // NOTE: window.confirm is deliberately NOT defined so
        // resetCommsThread's confirm branch (typeof window.confirm ===
        // 'function') is skipped and the clear proceeds unconditionally.
        globalThis.window = {
            location: { hash: "" },
            addEventListener: noop,
        };
        globalThis.localStorage = { getItem: () => null, setItem: noop };
"""

_SHIM_FETCH_DEFAULT = "globalThis.fetch = () => new Promise(() => {});"

_SHIM_TAIL = """
        process.on("unhandledRejection", () => {});
        globalThis.setInterval = () => 0;
        globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
        if (typeof Blob === "undefined") {
            globalThis.Blob = function (parts, opts) { this.parts = parts; this.opts = opts; };
        }
        if (typeof URL !== "undefined") {
            if (typeof URL.createObjectURL !== "function") URL.createObjectURL = function () { return "blob:stub"; };
            if (typeof URL.revokeObjectURL !== "function") URL.revokeObjectURL = function () {};
        }
"""

# Canned fetch: records every call (url + body) into __fetchLog and resolves
# with a tower-ish reply. Several plausible response keys are populated so
# the assertion target is the wiring/state, not one response key name.
_FETCH_IMPL = """(function () {
  globalThis.__fetchLog = [];
  return function (url, opts) {
    globalThis.__fetchLog.push({
      url: String(url),
      body: opts && opts.body != null ? String(opts.body) : "",
    });
    return Promise.resolve({
      ok: true,
      status: 200,
      json: function () {
        return Promise.resolve({
          reply: "Roger that.",
          message: "Roger that.",
          response: "Roger that.",
          text: "Roger that.",
        });
      },
    });
  };
})()"""


def _run_comms_js(expr, fetch_impl=None, extra_setup=""):
    """Evaluate a JS expression in the shared app.js harness environment.

    Mirrors tests/unit/test_dashboard_comms_send.py:_run_app_js: the shim
    (including extra_setup) runs before app.js loads, then the test's
    fetch_impl (if any) is swapped in and expr is evaluated.
    """
    shim_fetch_swap = (
        "globalThis.fetch = " + fetch_impl + ";"
        if fetch_impl is not None
        else ""
    )
    built_shim = _SHIM + _SHIM_FETCH_DEFAULT + _SHIM_TAIL + extra_setup
    expr = shim_fetch_swap + expr
    proc = _shared_run_app_js(expr, shim=built_shim)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _run_comms_js_async(expr, fetch_impl=None, extra_setup=""):
    """Like _run_comms_js but awaits the expression (for async code paths
    that drive fetch). `expr` is a `;`-separated statement list; it is
    wrapped in an async IIFE whose completion value the shared loader
    awaits (tests/unit/_app_js.py evaluates
    ``JSON.stringify(await eval(expr))`` at module top level, so the IIFE
    promise is awaited and its resolved value JSON-serialized).

    Note: a nested-eval wrapper (the pattern in
    test_dashboard_comms_send.py:_run_app_js_async) does NOT work here on
    this Node version — `await` inside the *nested* direct eval raises
    "SyntaxError: await is only valid in async functions", so the bare
    IIFE form is used instead."""
    wrapped = "(async () => {" + expr + "})()"
    return _run_comms_js(wrapped, fetch_impl=fetch_impl, extra_setup=extra_setup)


# === Blocking 2: header button click wiring ==============================


def test_module_load_survives_body_without_classlist():
    """Harness shims vary in how complete their document.body is: some
    (e.g. test_dashboard_search_preserve.py) define body as a bare marker
    object with no classList. applyTraceVisibility() runs at module load,
    so it must not throw - the whole app module graph fails to load for
    every such test otherwise (comms-trace-toggle-01, 2026-08-29)."""
    result = _run_comms_js(
        "({loaded: true})",
        extra_setup="globalThis.document.body = { _isBody: true };",
    )
    assert result["loaded"] is True


def test_comms_reset_and_export_buttons_have_click_listeners():
    """#comms-reset and #comms-export must each get a click listener at
    module load (base wiring restored alongside the trace-toggle wiring)."""
    result = _run_comms_js(
        "({"
        " resetClicks: globalThis.__wiring.reset.filter(l => l.type === 'click').length,"
        " exportClicks: globalThis.__wiring.export.filter(l => l.type === 'click').length"
        " })"
    )
    assert result["resetClicks"] >= 1, (
        "#comms-reset has no click listener registered at module load; "
        f"recorded wiring: {result}"
    )
    assert result["exportClicks"] >= 1, (
        "#comms-export has no click listener registered at module load; "
        f"recorded wiring: {result}"
    )


def test_comms_reset_button_click_clears_thread():
    """Dispatching the recorded #comms-reset click handler must clear the
    thread (innerHTML emptied / children dropped) without throwing."""
    result = _run_comms_js("""
        (() => {
            const thread = globalThis.__commsThread;
            thread.childrenRef.push({ className: "msg user" }, { className: "msg tower" });
            const htmlBefore = thread.innerHTMLSets.length;
            const listener = globalThis.__wiring.reset.find(l => l.type === 'click');
            if (!listener) return { wired: false };
            let err = null;
            try {
                listener.fn({ preventDefault: () => {}, stopPropagation: () => {} });
            } catch (e) {
                err = String((e && e.message) || e);
            }
            return {
                wired: true,
                err: err,
                innerHTMLCleared: thread.innerHTMLSets.slice(htmlBefore).indexOf("") !== -1,
                childrenEmptied: thread.childrenRef.length === 0,
            };
        })()
    """)
    assert result["wired"] is True, f"#comms-reset click listener missing: {result}"
    assert result["err"] is None, f"reset click handler threw: {result['err']}"
    assert result["innerHTMLCleared"] or result["childrenEmptied"], (
        f"reset click handler did not clear the thread: {result}"
    )


def test_comms_export_button_click_triggers_download():
    """Dispatching the recorded #comms-export click handler must run the
    export path (anchor download) without throwing."""
    result = _run_comms_js("""
        (() => {
            globalThis.__commsThread.childrenRef.push({ className: "msg user" });
            const listener = globalThis.__wiring.export.find(l => l.type === 'click');
            if (!listener) return { wired: false };
            let err = null;
            try {
                listener.fn({ preventDefault: () => {}, stopPropagation: () => {} });
            } catch (e) {
                err = String((e && e.message) || e);
            }
            return {
                wired: true,
                err: err,
                anchorClicks: globalThis.__anchorClicks,
                createdTags: globalThis.__createdTags.slice(),
            };
        })()
    """)
    assert result["wired"] is True, f"#comms-export click listener missing: {result}"
    assert result["err"] is None, f"export click handler threw: {result['err']}"
    assert result["anchorClicks"] >= 1 or "a" in result["createdTags"], (
        f"export click handler produced no download anchor: {result}"
    )


# === Blocking 1: multi-call send/reset state behavior ====================

def test_send_reset_send_history_persists_then_reset_clears():
    """Drive send -> send -> reset -> send and assert the exact trace:

    1. first send: exactly 1 /api/chat fetch, bubbles ["msg user",
       "msg tower"] (no "denied" fake-error bubble);
    2. second send: fetch count 1 -> 2 and the request body carries the
       earlier 'hello' too (module-level commsHistory persisted and
       accumulated across calls);
    3. resetCommsThread(): no throw;
    4. post-reset send: fetch count 2 -> 3, body contains 'fresh' and NOT
       'hello' (reset actually cleared the persistent history), no
       'denied' bubble.
    """
    result = _run_comms_js_async("""
        const chatFetches = () => globalThis.__fetchLog.filter(e => e.url.indexOf('/api/chat') !== -1);
        await sendCommsMessage('hello');
        const s1 = {
            fetches: chatFetches().length,
            classes: globalThis.__commsThread.appendedClasses.slice(),
        };
        await sendCommsMessage('status?');
        const s2 = {
            fetches: chatFetches().length,
            classes: globalThis.__commsThread.appendedClasses.slice(),
            body2: globalThis.__fetchLog[1] ? globalThis.__fetchLog[1].body : "",
        };
        let resetErr = null;
        try { resetCommsThread(); } catch (e) { resetErr = String((e && e.message) || e); }
        await sendCommsMessage('fresh');
        const s4 = {
            fetches: chatFetches().length,
            classes: globalThis.__commsThread.appendedClasses.slice(),
            body3: globalThis.__fetchLog[2] ? globalThis.__fetchLog[2].body : "",
        };
        return { s1: s1, s2: s2, resetErr: resetErr, s4: s4 };
    """, fetch_impl=_FETCH_IMPL)

    s1 = result["s1"]
    assert s1["fetches"] == 1, f"expected 1 /api/chat fetch after first send, got {s1}"
    assert s1["classes"] == ["msg user", "msg tower"], (
        f"first send bubble classes wrong (fake 'denied' bubble?): {s1}"
    )

    s2 = result["s2"]
    assert s2["fetches"] == 2, f"expected 2 /api/chat fetches after second send, got {s2}"
    assert "hello" in s2["body2"] and "status?" in s2["body2"], (
        f"second request body must carry the accumulated history "
        f"(hello + status?), got: {s2['body2']!r}"
    )

    assert result["resetErr"] is None, (
        f"resetCommsThread threw: {result['resetErr']}"
    )

    s4 = result["s4"]
    assert s4["fetches"] == 3, f"expected 3 /api/chat fetches after post-reset send, got {s4}"
    assert "fresh" in s4["body3"], f"post-reset request body missing 'fresh': {s4}"
    assert "hello" not in s4["body3"], (
        f"post-reset request body still carries pre-reset history: {s4}"
    )
    assert "denied" not in " ".join(s4["classes"]), (
        f"post-reset send rendered a fake 'denied' bubble: {s4}"
    )
