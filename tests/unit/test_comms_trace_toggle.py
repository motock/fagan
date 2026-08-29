"""Tests for the Comms TRACE toggle (hide tool-trace chips/details).

Story: add a `#comms-trace-toggle` button to the Comms actions row that
flips a `trace-off` class on <body>, hiding every `.trace-chip` and
`.trace-detail` via CSS only. The preference persists in localStorage under
the `commsShowTrace` key. The chips themselves stay in the DOM --
`renderToolTraceHtml` / `appendCommsMessage` / `sendCommsMessage` must NOT
be modified; visibility is purely CSS.

Files touched by the implementation (not by this test file):
  - static/index.html      new button inside .comms-actions, before #comms-export
  - static/app/comms.js    showTrace state + readStoredShowTrace +
                           applyTraceVisibility + click wiring
  - static/style.css       two `body.trace-off ...` display:none rules

These tests are RED until the implementation lands. Static assertions are
membership/substring based (static/ files are cumulative artifacts other
stories edit); behavior assertions reuse the shared Node-eval harness from
tests/unit/_app_js.py, loading static/app/comms.js directly as an ES module
against a stub DOM.
"""
import json
import os

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INDEX_HTML = os.path.join(REPO_ROOT, "static", "index.html")
COMMS_JS = os.path.join(REPO_ROOT, "static", "app", "comms.js")
STYLE_CSS = os.path.join(REPO_ROOT, "static", "style.css")


def _index_html_source():
    with open(INDEX_HTML, encoding="utf-8") as fh:
        return fh.read()


def _comms_js_source():
    with open(COMMS_JS, encoding="utf-8") as fh:
        return fh.read()


def _style_css_source():
    with open(STYLE_CSS, encoding="utf-8") as fh:
        return fh.read()


# === 1. static/index.html: the toggle button ==============================

_TOGGLE_BUTTON_EXACT = (
    '<button id="comms-trace-toggle" class="comms-icon-btn" type="button" '
    'aria-pressed="true" aria-label="Toggle tool trace" '
    'title="Toggle tool trace">TRACE</button>'
)


def test_index_html_has_trace_toggle_button():
    """index.html must contain the #comms-trace-toggle button with the
    exact markup the brief specifies (comms-icon-btn styling, type=button,
    initial aria-pressed="true", accessible name/title, TRACE label)."""
    src = _index_html_source()
    assert _TOGGLE_BUTTON_EXACT in src, (
        "index.html must contain the #comms-trace-toggle button verbatim: "
        f"{_TOGGLE_BUTTON_EXACT!r}"
    )


def test_index_html_trace_toggle_sits_inside_comms_actions_before_export():
    """The new button must live inside the existing .comms-actions div and
    be placed BEFORE #comms-export (brief-specified ordering)."""
    src = _index_html_source()
    idx_actions = src.index('class="comms-actions"')
    idx_toggle = src.index('id="comms-trace-toggle"')
    idx_export = src.index('id="comms-export"')
    idx_reset = src.index('id="comms-reset"')
    assert idx_actions < idx_toggle < idx_export < idx_reset, (
        "#comms-trace-toggle must be inside .comms-actions, before "
        "#comms-export, which precedes #comms-reset"
    )


def test_index_html_keeps_export_and_reset_buttons():
    """The pre-existing #comms-export and #comms-reset buttons must remain
    untouched by this story."""
    src = _index_html_source()
    assert (
        '<button id="comms-export" class="comms-icon-btn" type="button" '
        'aria-label="Export conversation" '
        'title="Export conversation">EXPORT</button>'
    ) in src, "#comms-export button must remain unchanged"
    assert (
        '<button id="comms-reset" class="comms-icon-btn" type="button" '
        'aria-label="Reset conversation" '
        'title="Reset conversation">RESET</button>'
    ) in src, "#comms-reset button must remain unchanged"


# === 2. static/app/comms.js: state, helper, apply, wiring =================


def _function_body(src, header):
    """Return the source from `header` up to the next top-level function /
    export boundary (same extraction approach as
    test_dashboard_comms_send.py)."""
    start = src.index(header)
    candidates = [
        src.find("\nfunction ", start + 1),
        src.find("\nasync function ", start + 1),
        src.find("\nexport ", start + 1),
        src.find("\nconst ", start + 1),
        src.find("\nlet ", start + 1),
    ]
    candidates = [c for c in candidates if c != -1]
    end = min(candidates) if candidates else len(src)
    return src[start:end]


def test_comms_js_defines_read_stored_show_trace():
    """The localStorage-reading helper must be defined in comms.js."""
    src = _comms_js_source()
    assert "function readStoredShowTrace" in src, (
        "comms.js must define readStoredShowTrace()"
    )


def test_comms_js_initializes_show_trace_at_module_level():
    """showTrace must be module-level state seeded from the stored
    preference (brief-specified line)."""
    src = _comms_js_source()
    assert "let showTrace = readStoredShowTrace();" in src, (
        "comms.js must declare module-level `let showTrace = "
        "readStoredShowTrace();`"
    )


def test_comms_js_defines_apply_trace_visibility():
    """applyTraceVisibility() must be defined as a function."""
    src = _comms_js_source()
    assert "function applyTraceVisibility" in src, (
        "comms.js must define applyTraceVisibility()"
    )


def test_comms_js_toggles_trace_off_on_document_body():
    """applyTraceVisibility must toggle a `trace-off` class on
    document.body (CSS-only visibility mechanism)."""
    src = _comms_js_source()
    assert "document.body.classList" in src, (
        "comms.js must toggle the class on document.body.classList"
    )
    assert "trace-off" in src, "comms.js must reference the trace-off class"


def test_comms_js_uses_comms_show_trace_storage_key():
    """The preference must be read from and written to localStorage under
    the `commsShowTrace` key."""
    src = _comms_js_source()
    assert "commsShowTrace" in src, (
        "comms.js must use the commsShowTrace localStorage key"
    )
    assert "localStorage.getItem" in src, (
        "readStoredShowTrace must read via localStorage.getItem"
    )
    assert "localStorage.setItem" in src, (
        "the click handler must persist via localStorage.setItem"
    )


def test_comms_js_syncs_aria_pressed_to_string_show_trace():
    """applyTraceVisibility must sync #comms-trace-toggle's aria-pressed to
    String(showTrace) (brief-specified expression)."""
    src = _comms_js_source()
    assert "aria-pressed" in src, (
        "comms.js must keep #comms-trace-toggle's aria-pressed in sync"
    )
    assert "String(showTrace)" in src, (
        "aria-pressed must be set to String(showTrace)"
    )


def test_comms_js_wires_click_listener_on_trace_toggle():
    """The click listener must be attached to #comms-trace-toggle and
    applyTraceVisibility must do the aria-pressed sync itself."""
    src = _comms_js_source()
    assert "comms-trace-toggle" in src, (
        "comms.js must reference #comms-trace-toggle"
    )
    body = _function_body(src, "function applyTraceVisibility")
    assert "aria-pressed" in body, (
        "applyTraceVisibility must sync #comms-trace-toggle's aria-pressed "
        "state (the element lookup may live at module level, but the "
        "setAttribute must happen inside applyTraceVisibility)"
    )


def test_comms_js_calls_apply_trace_visibility_more_than_once():
    """applyTraceVisibility() must be invoked at least twice: once from the
    click handler and once at module load (initial state)."""
    src = _comms_js_source()
    assert src.count("applyTraceVisibility()") >= 2, (
        "applyTraceVisibility() must be called from the click listener AND "
        "once at module load"
    )


def test_comms_js_trace_state_kept_out_of_render_tool_trace_html():
    """renderToolTraceHtml must NOT be modified by this story: the chips
    stay in the DOM and visibility is CSS-only, so its body must not
    reference any of the new trace-toggle identifiers."""
    src = _comms_js_source()
    body = _function_body(src, "function renderToolTraceHtml")
    for forbidden in ("trace-off", "showTrace", "commsShowTrace",
                      "comms-trace-toggle"):
        assert forbidden not in body, (
            f"renderToolTraceHtml must stay untouched by the trace toggle "
            f"(found {forbidden!r} in its body)"
        )


def test_comms_js_chip_functions_still_defined():
    """The pre-existing Comms functions must survive this story unchanged
    in name (guards against accidental removal/refactor)."""
    src = _comms_js_source()
    assert "function appendCommsMessage" in src
    assert "function renderToolTraceHtml" in src
    assert "function sendCommsMessage" in src


# === 3. static/style.css: the two body.trace-off rules ====================


def test_css_trace_off_hides_trace_chips():
    """body.trace-off must hide .trace-chip with !important (the expanded
    chip's `display: block` rule would otherwise win)."""
    src = _style_css_source()
    assert "body.trace-off .trace-chip { display: none !important; }" in src, (
        "style.css must contain "
        "`body.trace-off .trace-chip { display: none !important; }`"
    )


def test_css_trace_off_hides_trace_details():
    """body.trace-off must hide .trace-detail with !important."""
    src = _style_css_source()
    assert (
        "body.trace-off .trace-detail { display: none !important; }"
    ) in src, (
        "style.css must contain "
        "`body.trace-off .trace-detail { display: none !important; }`"
    )


def test_css_trace_off_rules_come_after_expanded_rule():
    """The new rules must be appended after the existing
    `.trace-chip.expanded + .trace-detail` rule so they are graded as an
    addition to the trace-chip section, not a rewrite of it."""
    src = _style_css_source()
    idx_expanded = src.index(".trace-chip.expanded + .trace-detail")
    idx_chip_off = src.index("body.trace-off .trace-chip")
    idx_detail_off = src.index("body.trace-off .trace-detail")
    assert idx_expanded < idx_chip_off < idx_detail_off, (
        "the body.trace-off rules must be appended after the existing "
        ".trace-chip.expanded + .trace-detail rule"
    )


def test_css_keeps_existing_expanded_rule():
    """The pre-existing expanded-detail rule must remain (existing stories'
    CSS assertions depend on it)."""
    src = _style_css_source()
    assert ".trace-chip.expanded + .trace-detail { display: block; }" in src


# === 4-6. behavior tests via the Node-eval harness ========================
#
# static/app/comms.js is loaded directly as an ES module (it has top-level
# imports, so the shared harness takes its dynamic-import path) against a
# stub DOM. The stub records:
#   - globalThis.__body.toggles      every document.body classList mutation
#   - globalThis.__traceToggle.handlers  click listeners on #comms-trace-toggle
#   - globalThis.__traceToggle.attrs     setAttribute calls on the toggle
#   - globalThis.__storage.setItemCalls localStorage.setItem calls

_SHIM_TEMPLATE = r"""
        const noop = () => {};
        function __makeClassList(rec) {
            const set = new Set();
            return {
                add: (...cs) => { cs.forEach((c) => set.add(c)); },
                remove: (...cs) => { cs.forEach((c) => set.delete(c)); },
                toggle: (c, force) => {
                    const next = force === undefined ? !set.has(c) : !!force;
                    if (next) { set.add(c); } else { set.delete(c); }
                    rec.push({ cls: c, present: set.has(c) });
                    return next;
                },
                contains: (c) => set.has(c),
                __snapshot: () => Array.from(set).sort(),
            };
        }
        globalThis.__body = { toggles: [] };
        const bodyEl = {
            dataset: {},
            classList: __makeClassList(globalThis.__body.toggles),
        };
        globalThis.__traceToggle = { handlers: [], attrs: [] };
        const traceToggleEl = {
            tagName: "BUTTON",
            id: "comms-trace-toggle",
            dataset: {},
            classList: __makeClassList([]),
            addEventListener: function (type, fn) {
                if (type === "click") globalThis.__traceToggle.handlers.push(fn);
            },
            setAttribute: function (k, v) {
                globalThis.__traceToggle.attrs.push([k, String(v)]);
            },
            getAttribute: () => null,
        };
        const genericEl = () => ({
            tagName: "DIV",
            innerHTML: "",
            textContent: "",
            className: "",
            style: {},
            dataset: {},
            children: [],
            classList: __makeClassList([]),
            addEventListener: noop,
            setAttribute: noop,
            appendChild: (c) => c,
            querySelectorAll: () => [],
        });
        globalThis.__storage = { setItemCalls: [] };
        globalThis.localStorage = __STORAGE_JS__;
        globalThis.document = {
            body: bodyEl,
            documentElement: { dataset: {} },
            addEventListener: noop,
            querySelectorAll: () => [],
            getElementById: (id) => {
                if (id === "comms-trace-toggle") {
                    return __TOGGLE_PRESENT__ ? traceToggleEl : null;
                }
                return genericEl();
            },
            createElement: (tag) => genericEl(),
        };
        globalThis.window = { location: { hash: "" }, addEventListener: noop };
        globalThis.fetch = () => new Promise(() => {});
        process.on("unhandledRejection", () => {});
        globalThis.setInterval = () => 0;
        globalThis.setTimeout = () => 0;
"""

# localStorage variants -----------------------------------------------------

_STORAGE_CLEAN = (
    "{ getItem: () => null, "
    "setItem: (k, v) => { globalThis.__storage.setItemCalls.push([k, String(v)]); } }"
)


def _storage_seeded(value):
    return (
        "(function () { const store = { commsShowTrace: " + json.dumps(value) + " };"
        " return { getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k)"
        " ? store[k] : null),"
        " setItem: (k, v) => { store[k] = String(v);"
        " globalThis.__storage.setItemCalls.push([k, String(v)]); } }; })()"
    )


_STORAGE_THROWING = (
    "{ getItem: () => { throw new Error('SecurityError: localStorage denied'); },"
    " setItem: () => { throw new Error('SecurityError: localStorage denied'); } }"
)


def _load_comms(expr, storage_js=_STORAGE_CLEAN, toggle_present=True):
    """Load static/app/comms.js in Node with the stub DOM and evaluate
    `expr`. Returns the JSON-decoded result; raises AssertionError with the
    node stderr if the module throws while loading (which is how the
    negative cases fail a non-defensive implementation)."""
    shim = _SHIM_TEMPLATE.replace("__STORAGE_JS__", storage_js)
    shim = shim.replace("__TOGGLE_PRESENT__", "true" if toggle_present else "false")
    proc = _shared_run_app_js(expr, app_js=COMMS_JS, shim=shim)
    if proc.returncode != 0:
        raise AssertionError(
            f"node failed while loading static/app/comms.js: {proc.stderr}"
        )
    return json.loads(proc.stdout)


def _state_after_load(storage_js=_STORAGE_CLEAN, toggle_present=True):
    """Load comms.js and return the initial toggle/body state."""
    return _load_comms(
        "({ handlers: globalThis.__traceToggle.handlers.length,"
        " attrs: globalThis.__traceToggle.attrs,"
        " bodyClasses: document.body.classList.__snapshot() })",
        storage_js=storage_js,
        toggle_present=toggle_present,
    )


def _click(n=1, storage_js=_STORAGE_CLEAN, toggle_present=True):
    """Load comms.js, invoke every registered click handler on
    #comms-trace-toggle `n` times, return the resulting state."""
    expr = (
        "(() => { const hs = globalThis.__traceToggle.handlers;"
        " for (let i = 0; i < " + str(n) + "; i++) {"
        " hs.forEach((h) => h()); }"
        " return { handlers: hs.length,"
        " attrs: globalThis.__traceToggle.attrs,"
        " setItemCalls: globalThis.__storage.setItemCalls,"
        " bodyClasses: document.body.classList.__snapshot() }; })()"
    )
    return _load_comms(expr, storage_js=storage_js, toggle_present=toggle_present)


def test_default_state_chips_visible_and_aria_pressed_true():
    """(4) With a fresh DOM and no stored preference, module load must NOT
    put `trace-off` on <body> (chips stay visible) and must sync the
    toggle's aria-pressed to 'true'."""
    state = _state_after_load()
    assert "trace-off" not in state["bodyClasses"], (
        f"default state must not add trace-off to body, got "
        f"{state['bodyClasses']!r}"
    )
    aria = [v for k, v in state["attrs"] if k == "aria-pressed"]
    assert aria, "applyTraceVisibility must set aria-pressed at module load"
    assert aria[-1] == "true", (
        f"default aria-pressed must be 'true', got {aria[-1]!r}"
    )


def test_click_toggle_hides_chips_and_persists_false():
    """(4) Clicking the toggle must add `trace-off` to <body>, set
    aria-pressed to 'false', and persist commsShowTrace='false'."""
    state = _click()
    assert "trace-off" in state["bodyClasses"], (
        f"clicking the toggle must add trace-off to body, got "
        f"{state['bodyClasses']!r}"
    )
    aria = [v for k, v in state["attrs"] if k == "aria-pressed"]
    assert aria and aria[-1] == "false", (
        f"after click aria-pressed must be 'false', got {aria!r}"
    )
    assert ["commsShowTrace", "false"] in state["setItemCalls"], (
        f"click must persist commsShowTrace='false', got "
        f"{state['setItemCalls']!r}"
    )


def test_second_click_restores_chips_and_persists_true():
    """Clicking twice must toggle back to visible and persist 'true'."""
    state = _click(n=2)
    assert "trace-off" not in state["bodyClasses"], (
        f"second click must remove trace-off from body, got "
        f"{state['bodyClasses']!r}"
    )
    aria = [v for k, v in state["attrs"] if k == "aria-pressed"]
    assert aria and aria[-1] == "true", (
        f"after second click aria-pressed must be 'true', got {aria!r}"
    )
    assert ["commsShowTrace", "true"] in state["setItemCalls"], (
        f"second click must persist commsShowTrace='true', got "
        f"{state['setItemCalls']!r}"
    )


def test_stored_false_starts_hidden():
    """A stored commsShowTrace of exactly 'false' must start with chips
    hidden (trace-off on body, aria-pressed 'false')."""
    state = _state_after_load(storage_js=_storage_seeded("false"))
    assert "trace-off" in state["bodyClasses"], (
        f"stored 'false' must start with trace-off on body, got "
        f"{state['bodyClasses']!r}"
    )
    aria = [v for k, v in state["attrs"] if k == "aria-pressed"]
    assert aria and aria[-1] == "false", (
        f"stored 'false' must start with aria-pressed 'false', got {aria!r}"
    )


def test_stored_true_starts_visible():
    """A stored commsShowTrace of 'true' must start with chips visible."""
    state = _state_after_load(storage_js=_storage_seeded("true"))
    assert "trace-off" not in state["bodyClasses"]
    aria = [v for k, v in state["attrs"] if k == "aria-pressed"]
    assert aria and aria[-1] == "true"


def test_stored_garbage_value_starts_visible():
    """Only the exact string 'false' hides the chips; any other stored
    value (e.g. '0') must fall back to visible."""
    state = _state_after_load(storage_js=_storage_seeded("0"))
    assert "trace-off" not in state["bodyClasses"], (
        f"stored garbage {state['bodyClasses']!r} must default to visible"
    )
    aria = [v for k, v in state["attrs"] if k == "aria-pressed"]
    assert aria and aria[-1] == "true"


def test_localstorage_getitem_throwing_still_initializes_visible():
    """(5) If localStorage.getItem throws, module load must not propagate
    the exception: chips stay visible and aria-pressed is 'true'."""
    state = _state_after_load(storage_js=_STORAGE_THROWING)
    assert "trace-off" not in state["bodyClasses"], (
        "a throwing localStorage must fall back to showing chips"
    )
    aria = [v for k, v in state["attrs"] if k == "aria-pressed"]
    assert aria and aria[-1] == "true", (
        f"throwing localStorage must default aria-pressed to 'true', got "
        f"{aria!r}"
    )


def test_localstorage_setitem_throwing_click_still_toggles():
    """(5) If localStorage.setItem throws on click, the try/catch around
    the persist call must swallow it: the visibility still flips and
    aria-pressed still syncs."""
    state = _click(storage_js=_STORAGE_THROWING)
    assert "trace-off" in state["bodyClasses"], (
        "a throwing setItem must not prevent the click from hiding chips"
    )
    aria = [v for k, v in state["attrs"] if k == "aria-pressed"]
    assert aria and aria[-1] == "false", (
        f"a throwing setItem must not prevent the aria-pressed sync, got "
        f"{aria!r}"
    )


def test_missing_trace_toggle_button_wiring_does_not_throw():
    """(6) With no #comms-trace-toggle in the DOM, module load must not
    throw (the wiring guards the element lookup); the body class state is
    still applied."""
    state = _state_after_load(toggle_present=False)
    assert "trace-off" not in state["bodyClasses"], (
        "without a toggle button the default state must still leave chips "
        "visible"
    )


def test_missing_trace_toggle_click_is_noop():
    """(6) With no #comms-trace-toggle in the DOM there is no handler to
    invoke, so nothing throws and nothing changes."""
    state = _click(toggle_present=False)
    assert state["handlers"] == 0
    assert "trace-off" not in state["bodyClasses"]


def test_render_tool_trace_html_still_emits_trace_chips():
    """The chips must stay in the DOM: renderToolTraceHtml keeps emitting
    .trace-chip markup (visibility is CSS-only, so the existing
    trace-chip assertions in test_dashboard_comms_send.py keep passing)."""
    html = _load_comms(
        "renderToolTraceHtml([{ name: 'read_file', args: { path: 'a.py' } }])"
    )
    assert isinstance(html, str) and "trace-chip" in html, (
        f"renderToolTraceHtml must still emit .trace-chip markup, got "
        f"{html!r}"
    )