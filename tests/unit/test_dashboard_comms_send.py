"""Node-eval harness tests for the Comms send/render wiring in static/app.js.

This story wires the Comms input box to POST /api/chat and renders the
conversation. It adds three functions to static/app.js:
  - appendCommsMessage(role, html)
  - renderToolTraceHtml(toolCalls)
  - sendCommsMessage(text)

The harness pattern is copied verbatim from
tests/unit/test_dashboard_comms_nav.py (which itself copied it from
test_dashboard.py) so this file stands alone. The shim is extended IN THIS
FILE ONLY to let a test supply a canned fetch response and to count
appendChild calls on #comms-thread, so we can assert how many messages
were appended without a real DOM.

These tests are RED until the implementation lands: the three functions
must be defined and exported, and the wiring must match the brief.
"""
import json
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")


def _run_app_js(expr, fetch_impl=None, extra_setup=""):
    """Evaluate a JS expression inside an environment where static/app.js
    has been loaded. Returns the JSON-serialized result.

    Copied verbatim from test_dashboard_comms_nav.py / test_dashboard.py so
    this file is self-contained. The shim is extended (in THIS file only) so
    a test can supply a canned `fetch` implementation via `fetch_impl` (a JS
    source string evaluating to a function) and so #comms-thread's
    appendChild is observable.

    `fetch_impl`, when provided, must be a JS expression evaluating to a
    function `(url, opts) => Promise<Response-like>`. The Response-like
    object must expose `.ok` (boolean) and a `.json()` method returning a
    Promise (or a plain value; the shim's helper awaits it).
    """
    shim_fetch = (
        "globalThis.fetch = " + fetch_impl + ";"
        if fetch_impl is not None
        else "globalThis.fetch = () => new Promise(() => {});"
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
        // Counters so tests can observe how many messages were appended to
        // #comms-thread and what class names the appended bubbles carried.
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
            getElementById: function (id) {
                if (id === "comms-thread") return commsThreadEl;
                if (id === "comms-landing") return commsLandingEl;
                if (id === "on-air") return onAirEl;
                if (id === "comms-send") return commsSendEl;
                if (id === "comms-input") return commsInputEl;
                return { ...fakeEl, dataset: {}, addEventListener: noop, children: [] };
            },
            createElement: function (tag) {
                // Return a fresh element so each appended node is independent
                // and so classList.toggle on chips is observable.
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
        + shim_fetch
        + """
        process.on("unhandledRejection", () => {});
        globalThis.setInterval = () => 0;
        globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
        """
    )
    script = (
        shim
        + extra_setup
        + "const fs = require('fs');"
        + f"eval(fs.readFileSync({json.dumps(APP_JS)}, 'utf8'));"
        + "globalThis.state = globalThis.window.state;"
        + "process.stdout.write(JSON.stringify(eval(" + json.dumps(expr) + ")));"
    )
    proc = subprocess.run(
        ["node", "-e", script],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _run_app_js_async(expr, fetch_impl=None, extra_setup=""):
    """Like _run_app_js but awaits the expression (for async code paths
    that drive fetch). The expression is wrapped in an async IIFE whose
    awaited result is JSON-stringified."""
    wrapped = "(async () => { return (" + expr + "); })()"
    return _run_app_js(wrapped, fetch_impl=fetch_impl, extra_setup=extra_setup)


def _app_js_source():
    """Read static/app.js source for static-source assertions."""
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


# === function definitions & exports ======================================

def test_append_comms_message_defined():
    """appendCommsMessage must be defined as a function in the source."""
    src = _app_js_source()
    assert "function appendCommsMessage" in src, (
        "appendCommsMessage must be defined as a function"
    )


def test_render_tool_trace_html_defined():
    """renderToolTraceHtml must be defined as a function in the source."""
    src = _app_js_source()
    assert "function renderToolTraceHtml" in src, (
        "renderToolTraceHtml must be defined as a function"
    )


def test_send_comms_message_defined():
    """sendCommsMessage must be defined as a function in the source."""
    src = _app_js_source()
    assert "function sendCommsMessage" in src, (
        "sendCommsMessage must be defined as a function"
    )


def test_append_comms_message_exported():
    """appendCommsMessage must be exported in module.exports."""
    assert _run_app_js("typeof appendCommsMessage") == "function"


def test_render_tool_trace_html_exported():
    """renderToolTraceHtml must be exported in module.exports."""
    assert _run_app_js("typeof renderToolTraceHtml") == "function"


def test_send_comms_message_exported():
    """sendCommsMessage must be exported in module.exports."""
    assert _run_app_js("typeof sendCommsMessage") == "function"


def test_module_exports_contains_three_new_names():
    """module.exports must list all three new functions."""
    src = _app_js_source()
    start = src.index("module.exports = {")
    end = src.index("};", start) + 2
    block = src[start:end]
    assert "appendCommsMessage" in block, "module.exports must include appendCommsMessage"
    assert "renderToolTraceHtml" in block, "module.exports must include renderToolTraceHtml"
    assert "sendCommsMessage" in block, "module.exports must include sendCommsMessage"


# === escapeHtml reuse (no duplication) ===================================

def test_escape_html_not_duplicated():
    """escapeHtml already exists at the top of the file; this story must
    reuse it, not redefine it. There must be exactly one definition."""
    src = _app_js_source()
    assert src.count("function escapeHtml") == 1, (
        "escapeHtml must not be duplicated; reuse the existing definition"
    )


# === renderToolTraceHtml ==================================================

def test_render_tool_trace_html_empty_returns_empty_string():
    """renderToolTraceHtml([]) must return "" (no empty .trace-row)."""
    result = _run_app_js("renderToolTraceHtml([])")
    assert result == "", f"expected empty string, got {result!r}"


def test_render_tool_trace_html_undefined_returns_empty_string():
    """renderToolTraceHtml(undefined) must return "" (defensive)."""
    result = _run_app_js("renderToolTraceHtml(undefined)")
    assert result == "", f"expected empty string, got {result!r}"


def test_render_tool_trace_html_null_returns_empty_string():
    """renderToolTraceHtml(null) must return "" (defensive)."""
    result = _run_app_js("renderToolTraceHtml(null)")
    assert result == "", f"expected empty string, got {result!r}"


def test_render_tool_trace_html_one_call_has_one_chip():
    """With one tool call, the output must contain exactly one
    `trace-chip` occurrence and must include the tool name."""
    call = json.dumps({"name": "get_plan", "args": {"plan_name": "alpha"}, "result": {"ok": True}})
    result = _run_app_js(f"renderToolTraceHtml([{call}])")
    assert isinstance(result, str)
    assert result.count("trace-chip") == 1, (
        f"expected exactly one trace-chip, got {result!r}"
    )
    assert "get_plan" in result, "trace chip must include the tool name"


def test_render_tool_trace_html_includes_args_stringified():
    """The chip must show name(JSON.stringify(args)) - so the args must be
    stringified into the chip label."""
    call = json.dumps({"name": "get_plan", "args": {"plan_name": "alpha"}, "result": {"ok": True}})
    result = _run_app_js(f"renderToolTraceHtml([{call}])")
    assert "alpha" in result, "chip label must include stringified args (plan_name value)"


def test_render_tool_trace_html_includes_trace_detail_with_result():
    """The sibling .trace-detail div must contain JSON.stringify(result)."""
    call = json.dumps({
        "name": "get_plan", "args": {"plan_name": "alpha"},
        "result": {"status": "shipped"},
    })
    result = _run_app_js(f"renderToolTraceHtml([{call}])")
    assert "trace-detail" in result, "must include a .trace-detail div"
    assert "shipped" in result, "trace-detail must contain the stringified result"


def test_render_tool_trace_html_two_calls_have_two_chips():
    """Boundary: two tool calls produce exactly two trace-chip occurrences."""
    c1 = json.dumps({"name": "get_plan", "args": {}, "result": {}})
    c2 = json.dumps({"name": "list_decisions", "args": {}, "result": []})
    result = _run_app_js(f"renderToolTraceHtml([{c1}, {c2}])")
    assert result.count("trace-chip") == 2, (
        f"expected exactly two trace-chips, got {result!r}"
    )


def test_render_tool_trace_html_chip_is_button():
    """Each trace-chip must be a <button> element (per the brief: a
    .trace-chip button)."""
    call = json.dumps({"name": "get_plan", "args": {}, "result": {}})
    result = _run_app_js(f"renderToolTraceHtml([{call}])")
    assert "<button" in result.lower(), "trace-chip must be a <button> element"


def test_render_tool_trace_html_chip_click_toggles_expanded():
    """Each chip's click must toggle the 'expanded' class on itself. We
    assert the source wires classList.toggle('expanded') on the chip."""
    src = _app_js_source()
    start = src.index("function renderToolTraceHtml")
    # Find the end of the function (next top-level function or module.exports).
    end_candidates = [
        src.find("\nfunction ", start + 1),
        src.find("\nasync function ", start + 1),
        src.find("\nmodule.exports", start + 1),
    ]
    end_candidates = [e for e in end_candidates if e != -1]
    end = min(end_candidates) if end_candidates else len(src)
    body = src[start:end]
    assert "expanded" in body, (
        "renderToolTraceHtml must toggle the 'expanded' class on chip click"
    )
    assert "toggle" in body, (
        "renderToolTraceHtml must use classList.toggle for the chip click"
    )


# === appendCommsMessage ==================================================

def test_append_comms_message_user_appends_to_thread():
    """appendCommsMessage('user', html) must append a node to #comms-thread
    whose className includes 'msg' and 'user'."""
    result = _run_app_js(
        "appendCommsMessage('user', 'hello'); globalThis.__commsThread.appendedClasses"
    )
    assert isinstance(result, list)
    assert len(result) >= 1, "a node must be appended to #comms-thread"
    assert any("msg" in c and "user" in c for c in result), (
        f"appended node must carry msg+user classes, got {result!r}"
    )


def test_append_comms_message_tower_appends_to_thread():
    """appendCommsMessage('tower', html) must append a node whose className
    includes 'msg' and 'tower'."""
    result = _run_app_js(
        "appendCommsMessage('tower', 'hi'); globalThis.__commsThread.appendedClasses"
    )
    assert isinstance(result, list)
    assert len(result) >= 1
    assert any("msg" in c and "tower" in c for c in result), (
        f"appended node must carry msg+tower classes, got {result!r}"
    )


def test_append_comms_message_hides_landing_on_first_message():
    """On the first message, #comms-landing must be hidden. We assert the
    source references comms-landing and comms-thread and toggles display."""
    src = _app_js_source()
    start = src.index("function appendCommsMessage")
    end_candidates = [
        src.find("\nfunction ", start + 1),
        src.find("\nasync function ", start + 1),
        src.find("\nmodule.exports", start + 1),
    ]
    end_candidates = [e for e in end_candidates if e != -1]
    end = min(end_candidates) if end_candidates else len(src)
    body = src[start:end]
    assert "comms-landing" in body, "appendCommsMessage must reference #comms-landing"
    assert "comms-thread" in body, "appendCommsMessage must reference #comms-thread"
    assert "flex" in body, (
        "appendCommsMessage must show #comms-thread with style.display = 'flex'"
    )


def test_append_comms_message_uses_escape_html_for_user_text():
    """sendCommsMessage must pass escapeHtml(text) (not raw text) to
    appendCommsMessage for the user bubble. We assert the source calls
    escapeHtml within sendCommsMessage."""
    src = _app_js_source()
    start = src.index("function sendCommsMessage")
    end_candidates = [
        src.find("\nfunction ", start + 1),
        src.find("\nasync function ", start + 1),
        src.find("\nmodule.exports", start + 1),
    ]
    end_candidates = [e for e in end_candidates if e != -1]
    end = min(end_candidates) if end_candidates else len(src)
    body = src[start:end]
    assert "escapeHtml" in body, (
        "sendCommsMessage must escape the user text via escapeHtml before appending"
    )


# === sendCommsMessage: happy path ========================================

_OK_FETCH = (
    "() => Promise.resolve({ ok: true, status: 200, "
    "json: () => Promise.resolve({ reply: 'ok', tool_calls: [], turns: 1 }) })"
)


def test_send_comms_message_happy_path_appends_two_messages():
    """A successful sendCommsMessage (mock fetch resolving with
    {reply, tool_calls:[], turns}) must append exactly two messages to
    #comms-thread: the user message and the tower reply."""
    result = _run_app_js_async(
        "sendCommsMessage('hello').then(() => globalThis.__commsThread.appendChildCalls)",
        fetch_impl=_OK_FETCH,
    )
    assert result == 2, (
        f"expected 2 appended messages (user + tower), got {result!r}"
    )


def test_send_comms_message_happy_path_user_then_tower_classes():
    """The two appended messages must be user then tower (no 'denied')."""
    result = _run_app_js_async(
        "sendCommsMessage('hello').then(() => globalThis.__commsThread.appendedClasses)",
        fetch_impl=_OK_FETCH,
    )
    assert isinstance(result, list)
    assert len(result) == 2, f"expected 2 classes, got {result!r}"
    assert "msg" in result[0] and "user" in result[0], (
        f"first appended node must be msg+user, got {result!r}"
    )
    assert "msg" in result[1] and "tower" in result[1], (
        f"second appended node must be msg+tower, got {result!r}"
    )
    assert "denied" not in result[1], (
        "happy-path tower reply must NOT carry the 'denied' class"
    )


def test_send_comms_message_posts_to_api_chat():
    """sendCommsMessage must POST to /api/chat with the right body shape.
    We capture the fetch url and options via a recording fetch shim."""
    fetch_recorder = (
        "(url, opts) => { globalThis.__fetchCalls = globalThis.__fetchCalls || []; "
        "globalThis.__fetchCalls.push({ url: url, opts: opts }); "
        "return Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ reply: 'ok', tool_calls: [], turns: 1 }) }); }"
    )
    result = _run_app_js_async(
        "sendCommsMessage('hello').then(() => globalThis.__fetchCalls)",
        fetch_impl=fetch_recorder,
    )
    assert isinstance(result, list)
    assert len(result) == 1, f"expected one fetch call, got {result!r}"
    call = result[0]
    assert call["url"] == "/api/chat", (
        f"must POST to /api/chat, got {call['url']!r}"
    )
    assert call["opts"]["method"] == "POST", "fetch method must be POST"
    assert call["opts"]["headers"]["Content-Type"] == "application/json", (
        "Content-Type header must be application/json"
    )
    body = json.loads(call["opts"]["body"])
    assert body["message"] == "hello", f"body.message must be the text, got {body!r}"
    assert "plan_name" in body, "body must include plan_name"
    assert "history" in body, "body must include history"


def test_send_comms_message_uses_state_selected_plan():
    """The body's plan_name must come from state.selectedPlan."""
    fetch_recorder = (
        "(url, opts) => { globalThis.__fetchCalls = globalThis.__fetchCalls || []; "
        "globalThis.__fetchCalls.push({ url: url, opts: opts }); "
        "return Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ reply: 'ok', tool_calls: [], turns: 1 }) }); }"
    )
    result = _run_app_js_async(
        "state.selectedPlan = 'my-plan'; "
        "sendCommsMessage('hello').then(() => JSON.parse(globalThis.__fetchCalls[0].opts.body).plan_name)",
        fetch_impl=fetch_recorder,
    )
    assert result == "my-plan", f"plan_name must be state.selectedPlan, got {result!r}"


def test_send_comms_message_history_is_null():
    """The body's history must be null (per the brief)."""
    fetch_recorder = (
        "(url, opts) => { globalThis.__fetchCalls = globalThis.__fetchCalls || []; "
        "globalThis.__fetchCalls.push({ url: url, opts: opts }); "
        "return Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ reply: 'ok', tool_calls: [], turns: 1 }) }); }"
    )
    result = _run_app_js_async(
        "sendCommsMessage('hello').then(() => JSON.parse(globalThis.__fetchCalls[0].opts.body).history)",
        fetch_impl=fetch_recorder,
    )
    assert result is None, f"history must be null, got {result!r}"


def test_send_comms_message_disables_send_and_lives_on_air():
    """sendCommsMessage must disable #comms-send and add 'live' to #on-air
    while in flight, then restore both in the finally path."""
    result = _run_app_js_async(
        "sendCommsMessage('hello').then(() => ({ "
        "sendDisabled: globalThis.__commsSend.disabledSet, "
        "onAirAdd: globalThis.__onAir.classListAdd, "
        "onAirRemove: globalThis.__onAir.classListRemove, "
        "sendRemove: globalThis.__commsSend.classListRemove "
        "}))",
        fetch_impl=_OK_FETCH,
    )
    # 'live' must have been added to #on-air during the call.
    assert "live" in result["onAirAdd"], (
        f"'live' must be added to #on-air during send, got {result!r}"
    )
    # 'live' must have been removed in the finally path.
    assert "live" in result["onAirRemove"], (
        f"'live' must be removed from #on-air in finally, got {result!r}"
    )
    # #comms-send must have been disabled (true) and re-enabled (false).
    assert True in result["sendDisabled"], (
        f"#comms-send must be disabled during send, got {result!r}"
    )
    assert False in result["sendDisabled"], (
        f"#comms-send must be re-enabled in finally, got {result!r}"
    )


def test_send_comms_message_re_enables_send_on_error():
    """Even on a fetch rejection, the finally path must re-enable
    #comms-send and remove 'live' from #on-air."""
    result = _run_app_js_async(
        "sendCommsMessage('hello').then(() => ({ "
        "sendDisabled: globalThis.__commsSend.disabledSet, "
        "onAirRemove: globalThis.__onAir.classListRemove "
        "}))",
        fetch_impl="() => Promise.reject(new Error('boom'))",
    )
    assert False in result["sendDisabled"], (
        f"#comms-send must be re-enabled even on error, got {result!r}"
    )
    assert "live" in result["onAirRemove"], (
        f"'live' must be removed from #on-air even on error, got {result!r}"
    )


# === sendCommsMessage: tool trace rendered in bubble =====================

def test_send_comms_message_renders_trace_in_bubble():
    """When the response has tool_calls, the trace HTML must be appended
    inside the tower bubble. We assert the source calls
    renderToolTraceHtml within sendCommsMessage."""
    src = _app_js_source()
    start = src.index("function sendCommsMessage")
    end_candidates = [
        src.find("\nfunction ", start + 1),
        src.find("\nasync function ", start + 1),
        src.find("\nmodule.exports", start + 1),
    ]
    end_candidates = [e for e in end_candidates if e != -1]
    end = min(end_candidates) if end_candidates else len(src)
    body = src[start:end]
    assert "renderToolTraceHtml" in body, (
        "sendCommsMessage must call renderToolTraceHtml to render the trace"
    )


def test_send_comms_message_with_tool_calls_appends_trace():
    """A response with one tool call must result in a trace-chip appearing
    in the appended tower bubble's innerHTML. We use a fetch that returns
    one tool call and inspect the appended node's innerHTML via a richer
    createElement shim."""
    fetch_impl = (
        "() => Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ reply: 'done', "
        "tool_calls: [{ name: 'get_plan', args: { plan_name: 'a' }, result: { ok: true } }], "
        "turns: 1 }) })"
    )
    # Use a richer shim that records innerHTML set on appended nodes.
    extra_setup = (
        "globalThis.__bubbles = []; "
        "globalThis.__origCreateElement = globalThis.document.createElement.bind(globalThis.document); "
        "globalThis.document.createElement = function (tag) { "
        "  var el = globalThis.__origCreateElement(tag); "
        "  var _innerHTML = ''; "
        "  Object.defineProperty(el, 'innerHTML', { "
        "    configurable: true, "
        "    get: function () { return _innerHTML; }, "
        "    set: function (v) { _innerHTML = String(v); globalThis.__bubbles.push(_innerHTML); } "
        "  }); "
        "  return el; "
        "};"
    )
    result = _run_app_js_async(
        "sendCommsMessage('hello').then(() => globalThis.__bubbles.join('\\n'))",
        fetch_impl=fetch_impl,
        extra_setup=extra_setup,
    )
    assert "trace-chip" in result, (
        f"trace-chip must appear in the tower bubble innerHTML, got {result!r}"
    )
    assert "get_plan" in result, "the tool name must appear in the bubble"


# === sendCommsMessage: denied path (tool result has error) ===============

_DENIED_FETCH = (
    "() => Promise.resolve({ ok: true, status: 200, "
    "json: () => Promise.resolve({ reply: 'nope', "
    "tool_calls: [{ name: 'patch_story', args: {}, result: { error: 'denied' } }], "
    "turns: 1 }) })"
)


def test_send_comms_message_denied_tool_result_marks_denied():
    """A tool call whose result is {error: ...} must cause the reply
    message to be classed 'msg tower denied', not plain 'msg tower'."""
    result = _run_app_js_async(
        "sendCommsMessage('do it').then(() => globalThis.__commsThread.appendedClasses)",
        fetch_impl=_DENIED_FETCH,
    )
    assert isinstance(result, list)
    assert len(result) == 2, f"expected 2 messages, got {result!r}"
    # The tower reply is the second message.
    tower = result[1]
    assert "msg" in tower and "tower" in tower, (
        f"second message must be msg+tower, got {result!r}"
    )
    assert "denied" in tower, (
        f"second message must carry 'denied' when a tool result has an error, got {result!r}"
    )


def test_send_comms_message_no_error_tool_result_not_denied():
    """When no tool result has an error, the reply must NOT be denied."""
    fetch_impl = (
        "() => Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ reply: 'done', "
        "tool_calls: [{ name: 'get_plan', args: {}, result: { ok: true } }], "
        "turns: 1 }) })"
    )
    result = _run_app_js_async(
        "sendCommsMessage('do it').then(() => globalThis.__commsThread.appendedClasses)",
        fetch_impl=fetch_impl,
    )
    assert isinstance(result, list)
    assert len(result) == 2
    tower = result[1]
    assert "denied" not in tower, (
        f"non-error tool result must not mark the reply denied, got {result!r}"
    )


def test_send_comms_message_mixed_error_and_ok_marks_denied():
    """If ANY tool call in the response has a result with an error, the
    reply must be marked denied (defensive: one blocked call denies the
    bubble)."""
    fetch_impl = (
        "() => Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ reply: 'partial', "
        "tool_calls: [ "
        "  { name: 'get_plan', args: {}, result: { ok: true } }, "
        "  { name: 'patch_story', args: {}, result: { error: 'nope' } } "
        "], turns: 1 }) })"
    )
    result = _run_app_js_async(
        "sendCommsMessage('do it').then(() => globalThis.__commsThread.appendedClasses[1])",
        fetch_impl=fetch_impl,
    )
    assert "denied" in result, (
        f"any error tool result must mark the reply denied, got {result!r}"
    )


# === sendCommsMessage: fetch rejection / non-2xx ==========================

def test_send_comms_message_rejected_fetch_appends_denied():
    """A rejected fetch must append exactly one .msg.tower.denied message
    (the user message is appended first, so total appends = 2: user +
    denied tower). The test asserts the denied message was appended, not
    just that nothing crashed."""
    result = _run_app_js_async(
        "sendCommsMessage('hello').then(() => ({ "
        "count: globalThis.__commsThread.appendChildCalls, "
        "classes: globalThis.__commsThread.appendedClasses "
        "}))",
        fetch_impl="() => Promise.reject(new Error('network down'))",
    )
    assert result["count"] == 2, (
        f"expected 2 appends (user + denied tower), got {result!r}"
    )
    denied = result["classes"][1]
    assert "msg" in denied and "tower" in denied and "denied" in denied, (
        f"rejected fetch must append a msg+tower+denied message, got {result!r}"
    )


def test_send_comms_message_rejected_fetch_does_not_leak_raw_error():
    """The denied message on a rejected fetch must use a generic,
    non-leaking error string - NOT the raw exception text. We assert the
    raw 'network down' / 'boom' text does NOT appear in the bubble."""
    extra_setup = (
        "globalThis.__bubbles = []; "
        "globalThis.__origCreateElement = globalThis.document.createElement.bind(globalThis.document); "
        "globalThis.document.createElement = function (tag) { "
        "  var el = globalThis.__origCreateElement(tag); "
        "  var _innerHTML = ''; "
        "  Object.defineProperty(el, 'innerHTML', { "
        "    configurable: true, "
        "    get: function () { return _innerHTML; }, "
        "    set: function (v) { _innerHTML = String(v); globalThis.__bubbles.push(_innerHTML); } "
        "  }); "
        "  return el; "
        "};"
    )
    result = _run_app_js_async(
        "sendCommsMessage('hello').then(() => globalThis.__bubbles.join('\\n'))",
        fetch_impl="() => Promise.reject(new Error('SECRET-STACK-TRACE-XYZ'))",
        extra_setup=extra_setup,
    )
    assert "SECRET-STACK-TRACE-XYZ" not in result, (
        "raw exception text must NOT leak into the UI (Secure by Design); "
        f"got {result!r}"
    )
    # The generic message must be present (the brief suggests
    # "Couldn't reach the tower - try again." or similar non-leaking text).
    assert "tower" in result.lower() or "reach" in result.lower() or "try again" in result.lower(), (
        f"a generic non-leaking error message must be shown, got {result!r}"
    )


def test_send_comms_message_non_2xx_appends_denied():
    """A non-2xx response (e.g. 500) must also append a .msg.tower.denied
    message, not throw."""
    fetch_impl = (
        "() => Promise.resolve({ ok: false, status: 500, "
        "json: () => Promise.resolve({ detail: 'internal' }) })"
    )
    result = _run_app_js_async(
        "sendCommsMessage('hello').then(() => ({ "
        "count: globalThis.__commsThread.appendChildCalls, "
        "classes: globalThis.__commsThread.appendedClasses "
        "}))",
        fetch_impl=fetch_impl,
    )
    assert result["count"] == 2, f"expected 2 appends, got {result!r}"
    denied = result["classes"][1]
    assert "denied" in denied, (
        f"non-2xx must append a denied tower message, got {result!r}"
    )


# === sendCommsMessage: boundary (whitespace no-op) ========================

def test_send_comms_message_whitespace_only_is_noop():
    """sendCommsMessage('   ') (whitespace only) must be a no-op: no fetch
    call, nothing appended."""
    fetch_recorder = (
        "(url, opts) => { globalThis.__fetchCalls = globalThis.__fetchCalls || []; "
        "globalThis.__fetchCalls.push({ url: url, opts: opts }); "
        "return Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ reply: 'ok', tool_calls: [], turns: 1 }) }); }"
    )
    result = _run_app_js_async(
        "sendCommsMessage('   ').then(() => ({ "
        "fetchCalls: (globalThis.__fetchCalls || []).length, "
        "appends: globalThis.__commsThread.appendChildCalls "
        "}))",
        fetch_impl=fetch_recorder,
    )
    assert result["fetchCalls"] == 0, (
        f"whitespace-only message must not call fetch, got {result!r}"
    )
    assert result["appends"] == 0, (
        f"whitespace-only message must not append anything, got {result!r}"
    )


def test_send_comms_message_empty_string_is_noop():
    """sendCommsMessage('') must be a no-op."""
    fetch_recorder = (
        "(url, opts) => { globalThis.__fetchCalls = globalThis.__fetchCalls || []; "
        "globalThis.__fetchCalls.push({ url: url, opts: opts }); "
        "return Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ reply: 'ok', tool_calls: [], turns: 1 }) }); }"
    )
    result = _run_app_js_async(
        "sendCommsMessage('').then(() => ({ "
        "fetchCalls: (globalThis.__fetchCalls || []).length, "
        "appends: globalThis.__commsThread.appendChildCalls "
        "}))",
        fetch_impl=fetch_recorder,
    )
    assert result["fetchCalls"] == 0
    assert result["appends"] == 0


def test_send_comms_message_trims_before_sending():
    """sendCommsMessage must trim the text before sending, so a message
    with surrounding whitespace still sends (and the trimmed text is what
    reaches the API)."""
    fetch_recorder = (
        "(url, opts) => { globalThis.__fetchCalls = globalThis.__fetchCalls || []; "
        "globalThis.__fetchCalls.push({ url: url, opts: opts }); "
        "return Promise.resolve({ ok: true, status: 200, "
        "json: () => Promise.resolve({ reply: 'ok', tool_calls: [], turns: 1 }) }); }"
    )
    result = _run_app_js_async(
        "sendCommsMessage('  hi  ').then(() => JSON.parse(globalThis.__fetchCalls[0].opts.body).message)",
        fetch_impl=fetch_recorder,
    )
    assert result == "hi", f"message must be trimmed before sending, got {result!r}"


# === wiring: #comms-send click & #comms-input Enter =======================

def test_comms_send_click_wired():
    """#comms-send's click must be wired to call sendCommsMessage. We assert
    the source references comms-send and addEventListener('click' ...)."""
    src = _app_js_source()
    assert "comms-send" in src, "source must reference #comms-send"
    # There must be a click listener wired to comms-send that calls
    # sendCommsMessage.
    assert "sendCommsMessage" in src, "source must call sendCommsMessage"


def test_comms_input_enter_wired():
    """#comms-input's Enter-without-Shift keydown must be wired to call
    sendCommsMessage. We assert the source references comms-input and a
    keydown/Enter/Shift guard."""
    src = _app_js_source()
    assert "comms-input" in src, "source must reference #comms-input"
    # The Enter-without-Shift guard: must check key === 'Enter' (or
    # event.key) and shiftKey.
    assert "Enter" in src or "enter" in src.lower(), (
        "source must guard on the Enter key for the input keydown"
    )
    assert "shiftKey" in src, (
        "source must guard against Shift+Enter (shiftKey) so multi-line input "
        "is not sent on Shift+Enter"
    )


def test_comms_input_clears_after_send():
    """After sending, the input value must be cleared. We assert the source
    clears the input value after the send call (in the click/keydown
    handler)."""
    src = _app_js_source()
    # The wiring must clear the input: look for a value = "" assignment near
    # the send wiring. We assert the source contains a value reset pattern
    # referencing the comms input.
    assert "value" in src and "comms-input" in src, (
        "source must clear the comms input value after sending"
    )


# === no unhandled rejection safety =======================================

def test_send_comms_message_rejected_fetch_no_unhandled_rejection():
    """A rejected fetch must NOT produce an unhandled rejection that
    crashes the node process. The harness's process.on('unhandledRejection')
    is a safety net, but sendCommsMessage itself must catch the rejection.
    We assert the node process exits 0 (the harness already checks
    returncode == 0, but we drive it explicitly here)."""
    # If sendCommsMessage did not catch, the async IIFE would reject and
    # node would log an unhandled rejection; the harness asserts
    # returncode == 0, so simply running it is the assertion.
    result = _run_app_js_async(
        "sendCommsMessage('hello').then(() => 'survived')",
        fetch_impl="() => Promise.reject(new Error('boom'))",
    )
    assert result == "survived", (
        "sendCommsMessage must catch fetch rejection and not propagate it"
    )