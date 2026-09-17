"""Node-eval harness tests for the Comms typing indicator (CTI-1).

While a chat reply is pending, sendCommsMessage shows a typing indicator
in the Comms panel: a single `.comms-typing` element created lazily by the
private helper `_typingIndicatorEl()` and appended ONCE to the EXISTING
#comms-body element (never to #comms-thread, so the append-count pins in
test_dashboard_comms_send.py are unaffected). The element is memoized in
the module-level `_typingEl` cache and hidden again in sendCommsMessage's
finally block.

Harness pattern copied from tests/unit/test_dashboard_comms_send.py (which
copied it from test_dashboard_comms_nav.py / test_dashboard.py), but this
file loads static/app/comms.js DIRECTLY (app_js=COMMS_JS) the way
test_comms_trace_toggle.py does, and extends the shim IN THIS FILE ONLY:
  - #comms-body is a stable instrumented element whose appendChild calls
    are counted, so memoization (append exactly once) is observable.
  - document.createElement records classList add/remove calls per element,
    so the show ('hidden' removed) / hide ('hidden' added) wiring inside
    sendCommsMessage is observable on the created typing element.

`_typingIndicatorEl` is module-private (not in comms.js's export list), so
its behavior is driven through the exported `sendCommsMessage` — the same
approach test_comms_trace_toggle.py uses for its private helpers — plus
static-source assertions for the private helper's structure.
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
        // Counters for #comms-thread (mirrors test_dashboard_comms_send.py).
        globalThis.__commsThread = { appendChildCalls: 0, appendedClasses: [] };
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
            style: { display: "" },
        };
        globalThis.__onAir = { classListAdd: [], classListRemove: [] };
        const onAirEl = {
            ...fakeEl,
            dataset: {},
            classList: {
                add: function (c) { globalThis.__onAir.classListAdd.push(c); },
                remove: function (c) { globalThis.__onAir.classListRemove.push(c); },
                toggle: noop, contains: () => false,
            },
        };
        globalThis.__commsSend = { disabledSet: [] };
        const commsSendEl = {
            ...fakeEl,
            dataset: {},
            disabled: false,
            set disabled(v) { globalThis.__commsSend.disabledSet.push(v); },
            get disabled() { return false; },
        };
        const commsInputEl = { ...fakeEl, dataset: {}, value: "", addEventListener: noop };
        // CTI-1 extension: #comms-body is a STABLE instrumented element so
        // the typing indicator's append-once memoization is observable.
        globalThis.__commsBody = { appendChildCalls: 0, appendedClasses: [], appended: [] };
        const commsBodyEl = {
            ...fakeEl,
            dataset: {},
            children: [],
            style: { display: "" },
            scrollTo: noop,
            scrollTop: 0,
            scrollHeight: 0,
            appendChild: function (child) {
                globalThis.__commsBody.appendChildCalls += 1;
                if (child && typeof child.className === "string") {
                    globalThis.__commsBody.appendedClasses.push(child.className);
                }
                globalThis.__commsBody.appended.push(child);
                return child;
            },
        };
        globalThis.__commsBodyNull = false;
        globalThis.document = {
            addEventListener: noop,
            documentElement: { dataset: {} },
            body: { classList: { add: noop, remove: noop, toggle: noop, contains: () => false } },
            getElementById: function (id) {
                if (globalThis.__commsBodyNull && id === "comms-body") return null;
                if (id === "comms-body") return commsBodyEl;
                if (id === "comms-thread") return commsThreadEl;
                if (id === "comms-landing") return commsLandingEl;
                if (id === "on-air") return onAirEl;
                if (id === "comms-send") return commsSendEl;
                if (id === "comms-input") return commsInputEl;
                return { ...fakeEl, dataset: {}, addEventListener: noop, children: [] };
            },
            createElement: function (tag) {
                // CTI-1 extension: record classList mutations per created
                // element so show/hide of the typing indicator is observable.
                const el = {
                    tagName: tag,
                    innerHTML: "",
                    textContent: "",
                    className: "",
                    __attrs: [],
                    classList: {
                        __clsCalls: [],
                        add: function (c) { this.__clsCalls.push(["add", c]); },
                        remove: function (c) { this.__clsCalls.push(["remove", c]); },
                        toggle: function (c) { this.__clsCalls.push(["toggle", c]); },
                        contains: function (c) {
                            return this.__clsCalls.some(([k, v]) => k === "add" && v === c);
                        },
                    },
                    setAttribute: function (k, v) { el.__attrs.push([k, String(v)]); },
                    addEventListener: noop,
                    appendChild: function (child) { return child; },
                    querySelectorAll: () => [],
                    dataset: {},
                    style: { display: "" },
                };
                return el;
            },
        };
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
"""

_OK_FETCH = (
    "() => Promise.resolve({ ok: true, status: 200, "
    "json: () => Promise.resolve({ reply: 'ok', tool_calls: [], turns: 1 }) })"
)

_REDUCED_MOTION_SETUP = (
    "globalThis.window.matchMedia = function (q) { return { matches: true }; };"
)


def _run_comms_js(expr, fetch_impl=None, extra_setup=""):
    """Load static/app/comms.js in Node with the stub DOM and evaluate expr.

    Copied from test_dashboard_comms_send.py's _run_app_js, changed to load
    COMMS_JS directly. The shim always loads comms.js against the default
    never-resolving fetch stub, then swaps in the test's fetch_impl right
    before the expr runs (prepended to expr), so module-load fetch traffic
    is never observed by a test's fetch_impl.
    """
    shim_fetch_swap = (
        "globalThis.fetch = " + fetch_impl + ";"
        if fetch_impl is not None
        else ""
    )
    built_shim = _SHIM + _SHIM_FETCH_DEFAULT + _SHIM_TAIL + extra_setup
    expr = shim_fetch_swap + expr
    proc = _shared_run_app_js(expr, app_js=COMMS_JS, shim=built_shim)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _comms_js_source():
    with open(COMMS_JS, encoding="utf-8") as fh:
        return fh.read()


def _typing_fn_source():
    """The source of _typingIndicatorEl (up to the next top-level function),
    for containment assertions."""
    src = _comms_js_source()
    start = src.index("function _typingIndicatorEl")
    end = src.index("function _commsMessageInnerHtml", start)
    return src[start:end]


# === static-source structure =============================================

def test_comms_js_defines_typing_indicator_el_once():
    src = _comms_js_source()
    assert src.count("function _typingIndicatorEl") == 1, (
        "_typingIndicatorEl must be defined exactly once in comms.js"
    )
    assert src.count("let _typingEl = null;") == 1, (
        "the _typingEl memo cache must be declared exactly once"
    )


def test_typing_indicator_appended_to_comms_body_not_thread():
    fn_src = _typing_fn_source()
    assert "getElementById('comms-body')" in fn_src, (
        "_typingIndicatorEl must anchor the indicator to #comms-body"
    )
    assert "body.appendChild(el)" in fn_src, (
        "_typingIndicatorEl must append the indicator to #comms-body"
    )
    assert "comms-thread" not in fn_src, (
        "the typing indicator must never be appended to #comms-thread"
    )
    assert "aria-hidden" in fn_src, "the indicator must be aria-hidden"


def test_send_comms_message_shows_and_hides_typing_indicator():
    src = _comms_js_source()
    assert "const typingEl = _typingIndicatorEl();" in src, (
        "sendCommsMessage must request the typing indicator"
    )
    assert "if (typingEl && typingEl.classList) typingEl.classList.remove('hidden');" in src, (
        "sendCommsMessage must un-hide the indicator while the reply is pending"
    )
    assert "if (_typingEl && _typingEl.classList) _typingEl.classList.add('hidden');" in src, (
        "sendCommsMessage's finally block must re-hide the indicator"
    )


# === behavior: show/hide + memoization via sendCommsMessage ==============

def test_send_shows_then_hides_typing_indicator_once():
    """One happy-path send: the indicator is appended to #comms-body once,
    un-hidden at send start, and re-hidden by the finally block."""
    result = _run_comms_js(
        "sendCommsMessage('hello').then(() => ({"
        " bodyAppends: globalThis.__commsBody.appendChildCalls,"
        " bodyClasses: globalThis.__commsBody.appendedClasses,"
        " clsCalls: (globalThis.__commsBody.appended[0]"
        "   && globalThis.__commsBody.appended[0].classList.__clsCalls) || [],"
        " attrs: (globalThis.__commsBody.appended[0]"
        "   && globalThis.__commsBody.appended[0].__attrs) || [] }))",
        fetch_impl=_OK_FETCH,
    )
    assert result["bodyAppends"] == 1, (
        f"expected exactly 1 appendChild on #comms-body, got {result!r}"
    )
    assert result["bodyClasses"] == ["comms-typing hidden"], (
        f"the appended indicator must start hidden, got {result!r}"
    )
    assert ["remove", "hidden"] in result["clsCalls"], (
        f"the indicator must be shown while pending, got {result!r}"
    )
    assert ["add", "hidden"] in result["clsCalls"], (
        f"the indicator must be hidden again after the reply, got {result!r}"
    )
    assert ["aria-hidden", "true"] in result["attrs"], (
        "the indicator must carry aria-hidden=true, got {result!r}"
    )


def test_typing_indicator_memoized_across_sends():
    """Two sequential sends reuse the SAME element: #comms-body sees exactly
    one appendChild total, while the cached element is shown/hidden once per
    send (2 removes + 2 adds of 'hidden' on that single element)."""
    result = _run_comms_js(
        "sendCommsMessage('first')"
        ".then(() => sendCommsMessage('second'))"
        ".then(() => ({"
        " bodyAppends: globalThis.__commsBody.appendChildCalls,"
        " appended: globalThis.__commsBody.appended.length,"
        " clsCalls: (globalThis.__commsBody.appended[0]"
        "   && globalThis.__commsBody.appended[0].classList.__clsCalls) || [] }))",
        fetch_impl=_OK_FETCH,
    )
    assert result["bodyAppends"] == 1 and result["appended"] == 1, (
        "the typing indicator must be memoized: exactly one element appended "
        f"to #comms-body across both sends, got {result!r}"
    )
    removes = [c for c in result["clsCalls"] if c[0] == "remove" and c[1] == "hidden"]
    adds = [c for c in result["clsCalls"] if c[0] == "add" and c[1] == "hidden"]
    assert len(removes) == 2 and len(adds) == 2, (
        "the SAME cached element must be shown+hidden once per send "
        f"(2 removes / 2 adds of 'hidden'), got {result!r}"
    )


def test_typing_indicator_null_body_does_not_throw():
    """With #comms-body missing, _typingIndicatorEl must bail out (null) and
    sendCommsMessage must still complete its normal happy path."""
    result = _run_comms_js(
        "sendCommsMessage('hello').then(() => ({"
        " bodyAppends: globalThis.__commsBody.appendChildCalls,"
        " threadAppends: globalThis.__commsThread.appendChildCalls }))",
        fetch_impl=_OK_FETCH,
        extra_setup="globalThis.__commsBodyNull = true;",
    )
    assert result["bodyAppends"] == 0, (
        f"no indicator may be appended when #comms-body is missing, got {result!r}"
    )
    assert result["threadAppends"] == 2, (
        f"the send itself must still append user+tower messages, got {result!r}"
    )


# === behavior: motion vs reduced-motion markup ===========================

def test_typing_indicator_dots_when_motion_allowed():
    result = _run_comms_js(
        "sendCommsMessage('hello').then(() => "
        "globalThis.__commsBody.appended[0].innerHTML)",
        fetch_impl=_OK_FETCH,
    )
    assert result.count('class="comms-typing-dot"') == 3, (
        f"expected three .comms-typing-dot spans, got {result!r}"
    )
    assert "comms-typing-static" not in result, (
        f"the animated variant must not carry the static label, got {result!r}"
    )


def test_typing_indicator_static_text_when_reduced_motion():
    """prefers-reduced-motion: the indicator renders the static 'tower is
    typing' label and NEVER a .comms-typing-dot element (no animation is
    ever attempted for a user who opted out)."""
    result = _run_comms_js(
        "sendCommsMessage('hello').then(() => "
        "globalThis.__commsBody.appended[0].innerHTML)",
        fetch_impl=_OK_FETCH,
        extra_setup=_REDUCED_MOTION_SETUP,
    )
    assert "tower is typing" in result, (
        f"reduced-motion variant must show the static label, got {result!r}"
    )
    assert "comms-typing-dot" not in result, (
        f"reduced-motion variant must not contain any dot span, got {result!r}"
    )


# === regression guard: the pinned thread counters are unaffected =========

def test_send_happy_path_still_appends_exactly_two_thread_messages():
    """Regression guard mirroring test_dashboard_comms_send.py's pins: with
    the typing indicator active, a successful send still appends EXACTLY two
    messages to #comms-thread (user then tower) — the indicator lives on
    #comms-body and is invisible to the thread counters."""
    result = _run_comms_js(
        "sendCommsMessage('hello').then(() => ({"
        " threadAppends: globalThis.__commsThread.appendChildCalls,"
        " classes: globalThis.__commsThread.appendedClasses }))",
        fetch_impl=_OK_FETCH,
    )
    assert result["threadAppends"] == 2, (
        f"expected exactly 2 appended thread messages, got {result!r}"
    )
    classes = result["classes"]
    assert len(classes) == 2, f"expected exactly 2 appended classes, got {result!r}"
    assert "msg" in classes[0] and "user" in classes[0], (
        f"first appended node must be msg+user, got {result!r}"
    )
    assert "msg" in classes[1] and "tower" in classes[1], (
        f"second appended node must be msg+tower, got {result!r}"
    )
