"""Tests for removing the manual "auto-refresh" checkbox and letting polling
always run (the dashboard's tab-hidden auto-pause behavior is preserved).

Scope (two files: static/index.html, static/app.js):
  - static/index.html must no longer contain the `<label class="auto-refresh">`
    block / the `id="auto-refresh"` checkbox. The `#refresh-indicator` and
    `#last-updated` spans must remain.
  - static/app.js must no longer reference `auto-refresh` anywhere, must no
    longer attach a "change" listener to it, and `syncPollingWithVisibility`
    must no longer be gated by an `auto-refresh` checkbox early-return. The
    existing pause-on-hidden / resume-on-visible behavior must still work
    WITHOUT any `auto-refresh` element existing in the shimmed document.

The node-eval harness (`_run_app_js`) is copied verbatim from
tests/unit/test_dashboard_comms_send.py so this file stands alone.

These tests are RED until the implementation lands.
"""
import json
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")
INDEX_HTML = os.path.join(REPO_ROOT, "static", "index.html")


def _run_app_js(expr, fetch_impl=None, extra_setup=""):
    """Evaluate a JS expression inside an environment where static/app.js
    has been loaded. Returns the JSON-serialized result.

    Copied verbatim from test_dashboard_comms_send.py so this file is
    self-contained. The shim is extended (in THIS file only) so a test can
    supply a canned `fetch` implementation via `fetch_impl` (a JS source
    string evaluating to a function) and so #comms-thread's appendChild is
    observable.

    `fetch_impl`, when provided, must be a JS expression evaluating to a
    function `(url, opts) => Promise<Response-like>`. The Response-like
    object must expose `.ok` (boolean) and a `.json()` method returning a
    Promise (or a plain value; the shim's helper awaits it).
    """
    # app.js calls refresh() once at top-level module load (pre-existing,
    # unrelated dashboard bootstrap behavior outside this story's scope).
    # That call must not be observed by a test's fetch_impl - only fetch()
    # calls made *after* app.js has finished loading (i.e. by the code the
    # test is actually driving) should hit it. So the shim always loads
    # app.js against the default never-resolving stub, then swaps in the
    # test's fetch_impl right after.
    shim_fetch_default = "globalThis.fetch = () => new Promise(() => {});"
    shim_fetch_swap = (
        "globalThis.fetch = " + fetch_impl + ";"
        if fetch_impl is not None
        else ""
    )
    shim = (
        """
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
        globalThis.__commsThread = { appendChildCalls: 0, appendedClasses: [] };
        globalThis.__commsLanding = { styleCalls: [] };
        globalThis.__onAir = { classListAdd: [], classListRemove: [] };
        globalThis.__commsSend = { disabledSet: [], classListAdd: [], classListRemove: [] };
        const commsThreadEl = {
            ...fakeEl,
            dataset: {},
            addEventListener: noop,
            children: [],
            style: { display: "" },
            appendChild: function (child) {
                globalThis.__commsThread.appendChildCalls += 1;
                if (child && typeof child.className === "string") {
                    globalThis.__commsThread.appendedClasses.push(child.className);
                }
                return child;
            },
        };
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
            hidden: false,
            getElementById: function (id) {
                if (id === "comms-thread") return commsThreadEl;
                if (id === "comms-landing") return commsLandingEl;
                if (id === "on-air") return onAirEl;
                if (id === "comms-send") return commsSendEl;
                if (id === "comms-input") return commsInputEl;
                // NOTE: deliberately NO special-case for "auto-refresh" -
                // the implementation must not depend on that element
                // existing anymore. Return a generic fake element so any
                // stray lookup does not crash, but tests assert behavior
                // is correct regardless of this fallback.
                return { ...fakeEl, dataset: {}, addEventListener: noop, children: [] };
            },
            createElement: function (tag) {
                return {
                    tagName: tag,
                    innerHTML: "",
                    textContent: "",
                    className: "",
                    classList: {
                        add: noop, remove: noop,
                        toggle: function (c) { this._toggled = c; },
                        contains: () => false,
                    },
                    addEventListener: noop,
                    setAttribute: noop,
                    appendChild: function (child) { return child; },
                    querySelectorAll: () => [],
                    dataset: {},
                    style: { display: "" },
                };
            },
        };
        globalThis.window = {
            location: { hash: "" },
            addEventListener: noop,
        };
        globalThis.localStorage = { getItem: () => null, setItem: noop };
        """
        + shim_fetch_default
        + """
        process.on("unhandledRejection", () => {});
        globalThis.setInterval = () => 1;
        globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
        globalThis.clearInterval = (h) => { /* no-op */ };
        """
    )
    script = (
        shim
        + extra_setup
        + "const fs = require('fs');"
        + f"eval(fs.readFileSync({json.dumps(APP_JS)}, 'utf8'));"
        + "globalThis.state = globalThis.window.state;"
        + shim_fetch_swap
        + "(async () => { const __result = await eval(" + json.dumps(expr) + "); "
        + "process.stdout.write(JSON.stringify(__result === undefined ? null : __result)); })();"
    )
    proc = subprocess.run(
        ["node", "-e", script],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _app_js_source():
    """Read static/app.js source for static-source assertions."""
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


def _index_html_source():
    """Read static/index.html source for static-source assertions."""
    with open(INDEX_HTML, encoding="utf-8") as fh:
        return fh.read()


# === static/index.html: checkbox removed, neighbors kept ==================

def test_index_html_no_auto_refresh_id():
    """The `id="auto-refresh"` checkbox must be removed from index.html."""
    html = _index_html_source()
    assert 'id="auto-refresh"' not in html, (
        'index.html must not contain id="auto-refresh" anymore'
    )


def test_index_html_no_auto_refresh_label_class():
    """The `<label class="auto-refresh">` block must be removed."""
    html = _index_html_source()
    assert "auto-refresh" not in html, (
        "index.html must not contain the string 'auto-refresh' at all "
        "(neither the label class nor the checkbox id)"
    )


def test_index_html_refresh_indicator_kept():
    """The #refresh-indicator span must remain next to where the checkbox was."""
    html = _index_html_source()
    assert 'id="refresh-indicator"' in html, (
        "#refresh-indicator span must be preserved"
    )


def test_index_html_last_updated_kept():
    """The #last-updated span must remain next to where the checkbox was."""
    html = _index_html_source()
    assert 'id="last-updated"' in html, (
        "#last-updated span must be preserved"
    )


# === static/app.js: full removal of auto-refresh references ===============

def test_app_js_no_auto_refresh_string():
    """No reference to `auto-refresh` may remain anywhere in app.js."""
    src = _app_js_source()
    assert "auto-refresh" not in src, (
        "app.js must not contain the string 'auto-refresh' anywhere - "
        "the checkbox, its change listener, and the early-return gate in "
        "syncPollingWithVisibility must all be removed"
    )


def test_app_js_no_auto_refresh_change_listener():
    """The `document.getElementById("auto-refresh").addEventListener("change"...)`
    block must be removed entirely. (Covered by the no-string test above, but
    asserted explicitly so a partial edit that only renames the variable is
    still caught.)"""
    src = _app_js_source()
    assert 'getElementById("auto-refresh")' not in src, (
        "the auto-refresh change-listener lookup must be removed"
    )


def test_app_js_sync_polling_visibility_defined_and_exported():
    """syncPollingWithVisibility must still exist and be exported (the
    tab-hidden auto-pause behavior is preserved, not deleted)."""
    assert _run_app_js("typeof syncPollingWithVisibility") == "function"


def test_app_js_start_stop_polling_still_exported():
    """startPolling / stopPolling must still exist (always-on polling uses them)."""
    assert _run_app_js("typeof startPolling") == "function"
    assert _run_app_js("typeof stopPolling") == "function"


# === syncPollingWithVisibility: no checkbox gate, behavior preserved ======

def test_sync_polling_pauses_when_hidden_without_checkbox():
    """With document.hidden === true and a truthy pollHandle, calling
    syncPollingWithVisibility must stop polling (pollHandle becomes falsy)
    EVEN THOUGH no auto-refresh element exists in the shimmed document.

    This is the core regression: the old early-return
    `const auto = document.getElementById("auto-refresh"); if (!auto || !auto.checked) return;`
    would have aborted before stopPolling() ran. After the fix there is no
    gate, so the pause path executes regardless of any checkbox.
    """
    # Start polling so pollHandle is truthy, then hide the tab and sync.
    result = _run_app_js(
        "startPolling();"
        "const before = !!state.pollHandle;"
        "document.hidden = true;"
        "syncPollingWithVisibility();"
        "const after = !!state.pollHandle;"
        "({ before: before, after: after });"
    )
    # _run_app_js JSON-parses stdout, so result is the parsed object.
    assert result["before"] is True, "pollHandle should be truthy before sync"
    assert result["after"] is False, (
        "pollHandle must be falsy after syncPollingWithVisibility with "
        "document.hidden === true (pause path ran with no checkbox gate)"
    )


def test_sync_polling_resumes_when_visible_without_checkbox():
    """Negative regression: with document.hidden === false and pollHandle
    already null, calling syncPollingWithVisibility must resume polling
    (pollHandle becomes truthy again) with no checkbox gate in front of the
    resume path.

    The old code's early-return `if (!auto || !auto.checked) return;` would
    have aborted before startPolling() ran when no checkbox existed. After
    the fix the resume path executes.
    """
    result = _run_app_js(
        "stopPolling();"
        "const before = !!state.pollHandle;"
        "document.hidden = false;"
        "syncPollingWithVisibility();"
        "const after = !!state.pollHandle;"
        "({ before: before, after: after });"
    )
    assert result["before"] is False, "pollHandle should be falsy before sync"
    assert result["after"] is True, (
        "pollHandle must be truthy after syncPollingWithVisibility with "
        "document.hidden === false (resume path ran with no checkbox gate)"
    )


def test_sync_polling_no_early_return_when_hidden_default_state():
    """Boundary: calling syncPollingWithVisibility with document.hidden === true
    when polling has NOT been started (pollHandle already null) must be a
    no-op that leaves pollHandle falsy - it must not throw and must not
    accidentally start polling. Crucially it must not early-return due to a
    missing checkbox: the pause branch is reached and is a no-op on an
    already-stopped handle."""
    result = _run_app_js(
        "stopPolling();"
        "document.hidden = true;"
        "syncPollingWithVisibility();"
        "!!state.pollHandle;"
    )
    assert result is False, (
        "pollHandle must remain falsy when hidden and polling already stopped"
    )


def test_sync_polling_visible_with_active_handle_is_noop():
    """Boundary: with document.hidden === false and pollHandle already truthy,
    calling syncPollingWithVisibility must NOT restart polling (the resume
    branch is `else if (!state.pollHandle)`, so an active handle is left
    alone). Confirms the resume path is not over-eager."""
    result = _run_app_js(
        "startPolling();"
        "const handleBefore = state.pollHandle;"
        "document.hidden = false;"
        "syncPollingWithVisibility();"
        "({ same: state.pollHandle === handleBefore, truthy: !!state.pollHandle });"
    )
    assert result["truthy"] is True
    assert result["same"] is True, (
        "an already-active pollHandle must not be replaced when visible"
    )


# === always-on polling on load ============================================

def test_polling_started_on_load():
    """Polling must be running after app.js loads (always-on, no checkbox to
    enable). state.pollHandle must be truthy immediately after load."""
    assert _run_app_js("!!state.pollHandle") is True, (
        "polling must be started unconditionally on load"
    )