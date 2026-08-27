"""Node-eval harness tests for wiring the Comms landing suggestion chips
(`.comms-chip` buttons inside `#comms-chips`, added by the prior "Build the
Comms landing hero copy and suggestion-chip markup" story) to send their
message through the same path a typed message uses.

Scope of the change under test (static/app/comms.js ONLY): a new wiring
block, inserted between the existing `commsInput` keydown block and the
final `export { ... }` line, that does:

    const commsChips = document.querySelectorAll('.comms-chip');
    commsChips.forEach((chip) => {
      chip.addEventListener('click', () => {
        const message = chip.dataset.commsChipMessage || chip.textContent;
        sendCommsMessage(message);
      });
    });

The harness pattern (SHIM/_run_app_js) is copied verbatim from
tests/unit/test_dashboard_comms_send.py so this file stands alone, per that
file's own precedent. It is EXTENDED (in this file only) to:
  - mock `document.querySelectorAll` (the original shim's `document` mock
    has no such method - it was never needed until this story) so it can
    return a configurable list of fake `.comms-chip` elements, and
  - track every selector `document.querySelectorAll` is called with, so a
    test can assert the wiring code actually queried for '.comms-chip'
    even in the zero-chips case where nothing else would be observable.

Since `sendCommsMessage` is a module-scoped binding inside comms.js (not a
mutable global property), it cannot be spied on directly from outside the
module in this harness - reassigning `globalThis.sendCommsMessage` would
not affect the internal call the click handler makes. So, exactly like the
existing sendCommsMessage tests in test_dashboard_comms_send.py, these
tests observe sendCommsMessage's effect via a recording `fetch` mock: the
POST /api/chat body's `message` field is what sendCommsMessage was called
with (fetch is invoked synchronously as part of calling sendCommsMessage,
before the first `await` suspends it, so no async flush is needed).

These tests are RED until the implementation lands: today comms.js's
top-level code never calls `document.querySelectorAll('.comms-chip')` at
all, so clicking a mock chip is a pure no-op (no handler was ever
registered) and the querySelectorAll-selector tracking assertions fail.

NOTE for whoever implements against these tests: tests/unit/
test_comms_landing_redesign.py::test_comms_js_untouched asserts a
byte-exact SHA-256 hash of static/app/comms.js captured before this story.
That assertion will legitimately start failing once comms.js is edited -
its own docstring already anticipates this ("chip wiring is a dependent
follow-up story"). Per CLAUDE.md's "Protect existing tests" step, do not
edit that test yourself; surface it for explicit approval instead.
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

        // --- chip mocks (this story's own addition to the shared shim) ---
        // Each fake chip exposes `dataset` (mirroring the real DOM's
        // camelCasing of data-comms-chip-message -> dataset.commsChipMessage)
        // and a real addEventListener that records the registered click
        // handler(s) so a test can simulate a click by calling `.click()`
        // without a real DOM/browser.
        function __makeChip(message, textContent) {
            return {
                dataset: message === undefined ? {} : { commsChipMessage: message },
                textContent: textContent !== undefined ? textContent : (message || ""),
                _clickHandlers: [],
                addEventListener: function (type, handler) {
                    if (type === "click") this._clickHandlers.push(handler);
                },
                click: function () {
                    this._clickHandlers.forEach(function (h) { h(); });
                },
            };
        }
        globalThis.__makeChip = __makeChip;
        // Default: no chips. Tests override globalThis.__chips via
        // extra_setup (run after this shim, before app.js is imported).
        globalThis.__chips = globalThis.__chips || [];
        globalThis.__querySelectorAllCalls = [];

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
            querySelectorAll: function (selector) {
                globalThis.__querySelectorAllCalls.push(selector);
                if (selector === ".comms-chip") return globalThis.__chips;
                return [];
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

_SHIM_FETCH_DEFAULT = "globalThis.fetch = () => new Promise(() => {});"

_SHIM_TAIL = """
        process.on("unhandledRejection", () => {});
        globalThis.setInterval = () => 0;
        globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
"""

# Records every fetch call's url/opts so a test can inspect what
# sendCommsMessage actually sent, without needing to spy on the
# module-scoped sendCommsMessage binding directly (see module docstring).
_FETCH_RECORDER = (
    "(url, opts) => { globalThis.__fetchCalls = globalThis.__fetchCalls || []; "
    "globalThis.__fetchCalls.push({ url: url, opts: opts }); "
    "return Promise.resolve({ ok: true, status: 200, "
    "json: () => Promise.resolve({ reply: 'ok', tool_calls: [], turns: 1 }) }); }"
)


def _run_app_js(expr, fetch_impl=None, extra_setup=""):
    """Evaluate a JS expression after static/app.js (which imports
    static/app/comms.js) has been loaded under the shim above.

    Mirrors test_dashboard_comms_send.py's `_run_app_js`: app.js's top-level
    `refresh()` call fires its own fetches during module load against the
    always-pending `_SHIM_FETCH_DEFAULT`, and only after import completes is
    the test's `fetch_impl` swapped in (prepended to `expr`) - so a
    recording fetch_impl only ever observes fetches the test itself
    triggers (e.g. by clicking a chip), not app.js's own bootstrap fetches.
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


def _set_chips(*chips):
    """Build the extra_setup JS that overrides globalThis.__chips before
    app.js is imported. Each entry is a (message, text) tuple; message may
    be None to simulate a chip with no data-comms-chip-message attribute at
    all (dataset.commsChipMessage is undefined, matching real DOM
    behavior for a missing data- attribute)."""
    parts = []
    for message, text in chips:
        msg_js = "undefined" if message is None else json.dumps(message)
        text_js = "undefined" if text is None else json.dumps(text)
        parts.append(f"__makeChip({msg_js}, {text_js})")
    return "globalThis.__chips = [" + ", ".join(parts) + "];"


def _click_and_capture(index):
    """JS expr: click globalThis.__chips[index], then return
    { count, message, url } describing the most recent fetch call (or
    count: 0 / message: null / url: null if no fetch happened)."""
    return (
        f"globalThis.__chips[{index}].click(); "
        "(function () { "
        "  var calls = globalThis.__fetchCalls || []; "
        "  if (calls.length === 0) return { count: 0, message: null, url: null }; "
        "  var last = calls[calls.length - 1]; "
        "  return { count: calls.length, message: JSON.parse(last.opts.body).message, url: last.url }; "
        "})()"
    )


def _comms_js_source():
    with open(COMMS_JS, encoding="utf-8") as fh:
        return fh.read()


# === happy path: clicking a chip sends its own message ====================

def test_click_on_chip_sends_its_own_data_message():
    """Clicking the one .comms-chip button must POST /api/chat with
    message == that chip's data-comms-chip-message text."""
    result = _run_app_js(
        _click_and_capture(0),
        fetch_impl=_FETCH_RECORDER,
        extra_setup=_set_chips(("what's blocked right now?", "what's blocked right now?")),
    )
    assert result["count"] == 1, f"expected exactly one fetch call, got {result!r}"
    assert result["url"] == "/api/chat", f"must POST to /api/chat, got {result!r}"
    assert result["message"] == "what's blocked right now?", (
        f"sendCommsMessage must be called with the chip's own message, got {result!r}"
    )


def test_click_on_second_chip_sends_its_own_distinct_message():
    """A second, differently-labeled chip must send its own message, not
    the first chip's - proving each chip's listener is wired to its own
    element rather than a single shared handler."""
    result = _run_app_js(
        _click_and_capture(1),
        fetch_impl=_FETCH_RECORDER,
        extra_setup=_set_chips(
            ("draft a plan for CSV export", "draft a plan for CSV export"),
            ("approve the merge for W1-04", "approve the merge for W1-04"),
        ),
    )
    assert result["count"] == 1
    assert result["message"] == "approve the merge for W1-04", (
        f"clicking chip[1] must send chip[1]'s own message, got {result!r}"
    )


def test_clicking_non_last_chip_sends_its_own_message_not_last_chips():
    """Classic closure-over-loop-variable regression guard: with three
    chips registered, clicking the FIRST (non-last) chip must send the
    first chip's message, not the last chip's - a bug that would occur if
    the implementation captured a shared/mutable reference across the
    forEach iterations instead of each callback's own `chip`."""
    result = _run_app_js(
        _click_and_capture(0),
        fetch_impl=_FETCH_RECORDER,
        extra_setup=_set_chips(
            ("first chip message", "first chip message"),
            ("second chip message", "second chip message"),
            ("third and last chip message", "third and last chip message"),
        ),
    )
    assert result["count"] == 1
    assert result["message"] == "first chip message", (
        f"clicking the first chip must send the first chip's message, not "
        f"the last chip's, got {result!r}"
    )


# === boundary: missing / empty data-comms-chip-message falls back to text =

def test_click_on_chip_missing_data_attribute_falls_back_to_text_content():
    """A chip rendered with no data-comms-chip-message attribute at all
    (dataset.commsChipMessage is undefined) must fall back to the chip's
    visible textContent - the defensive `|| chip.textContent` path."""
    result = _run_app_js(
        _click_and_capture(0),
        fetch_impl=_FETCH_RECORDER,
        extra_setup=_set_chips((None, "fallback visible text")),
    )
    assert result["count"] == 1
    assert result["message"] == "fallback visible text", (
        f"missing data-comms-chip-message must fall back to textContent, got {result!r}"
    )


def test_click_on_chip_with_empty_string_data_attribute_falls_back_to_text_content():
    """Boundary: an empty-string data-comms-chip-message (falsy, but not
    undefined) must also fall back to textContent, since
    `"" || chip.textContent` evaluates the fallback."""
    result = _run_app_js(
        _click_and_capture(0),
        fetch_impl=_FETCH_RECORDER,
        extra_setup=_set_chips(("", "shown chip label")),
    )
    assert result["count"] == 1
    assert result["message"] == "shown chip label", (
        f"empty-string data-comms-chip-message must fall back to textContent, got {result!r}"
    )


# === zero chips: no error, forEach is a no-op ==============================

def test_zero_comms_chip_elements_does_not_throw_and_queries_selector():
    """If document.querySelectorAll('.comms-chip') returns an empty
    array/NodeList, the forEach must be a no-op: no error thrown (module
    load must succeed - `_run_app_js` raises if the node process exits
    non-zero), and the wiring code must still have actually invoked
    querySelectorAll('.comms-chip') so this isn't vacuously true of
    unrelated code."""
    result = _run_app_js(
        "globalThis.__querySelectorAllCalls.indexOf('.comms-chip') !== -1",
        extra_setup=_set_chips(),
    )
    assert result is True, (
        "comms.js must call document.querySelectorAll('.comms-chip') even "
        "when there are zero chips in the DOM"
    )


def test_zero_comms_chip_elements_no_fetch_call_occurs():
    """With zero chips, nothing can be clicked, so no fetch call should
    ever occur as a side effect of the (no-op) wiring code itself."""
    result = _run_app_js(
        "(globalThis.__fetchCalls || []).length",
        fetch_impl=_FETCH_RECORDER,
        extra_setup=_set_chips(),
    )
    assert result == 0, f"expected no fetch calls with zero chips, got {result!r}"


# === independence from #comms-send / #comms-input ==========================

def test_click_on_chip_does_not_read_stale_comms_input_value():
    """Clicking a chip must send the chip's OWN message, not whatever is
    sitting in #comms-input - and must not ALSO trigger a second send via
    #comms-send/#comms-input's own listeners (which would produce a second
    fetch call, since the sentinel input value below is non-empty and would
    not be swallowed by sendCommsMessage's blank-string no-op guard)."""
    result = _run_app_js(
        _click_and_capture(0),
        fetch_impl=_FETCH_RECORDER,
        extra_setup=(
            _set_chips(("the chip's own message", "the chip's own message"))
            + "globalThis.document.getElementById('comms-input').value = 'SHOULD-NOT-BE-SENT';"
        ),
    )
    assert result["count"] == 1, (
        f"expected exactly one fetch call (the chip's own send only, not "
        f"also comms-send/comms-input's), got {result!r}"
    )
    assert result["message"] == "the chip's own message", (
        f"chip click must not read #comms-input's value, got {result!r}"
    )


# === source-level regression guards: existing wiring untouched ============

def test_existing_comms_send_and_input_wiring_still_referenced():
    """Regression guard: adding the chip wiring block must not remove or
    break the pre-existing #comms-send / #comms-input wiring it sits
    between."""
    src = _comms_js_source()
    assert "comms-send" in src, "existing #comms-send reference must remain"
    assert "comms-input" in src, "existing #comms-input reference must remain"
    assert src.count("sendCommsMessage") >= 4, (
        "expected sendCommsMessage referenced by its own definition, the "
        "existing comms-send click wiring, the existing comms-input "
        "keydown wiring, and the export line (at least 4 occurrences); "
        f"got {src.count('sendCommsMessage')} - existing wiring may have "
        "been removed"
    )


def test_export_line_retains_original_three_names_and_is_singular():
    """This story adds no new exported name (chip wiring is a plain
    top-level side effect, not a function other modules need to import),
    so the export line must be untouched: still exactly one export
    statement, still listing the three pre-existing names. This does not
    assert the export line's exact full text/order, since a sibling story
    may have already added `updateCommsSubtitle` to it - only that this
    story neither removes the original names nor introduces a second
    export statement."""
    src = _comms_js_source()
    assert src.count("export {") == 1, (
        "expected exactly one export statement in comms.js; chip wiring "
        "must not introduce a new export"
    )
    start = src.index("export {")
    end = src.index("}", start)
    export_block = src[start:end]
    for name in ("renderToolTraceHtml", "appendCommsMessage", "sendCommsMessage"):
        assert name in export_block, (
            f"{name} must remain in the export list; this story adds no "
            "new export and must not remove existing ones"
        )


# === source-level sanity checks tying failures back to the brief ==========

def test_source_references_comms_chip_class_selector():
    src = _comms_js_source()
    assert ".comms-chip" in src, (
        "comms.js must select the chip buttons via the '.comms-chip' class"
    )


def test_source_references_comms_chip_message_dataset_key():
    src = _comms_js_source()
    assert "commsChipMessage" in src, (
        "comms.js must read chip.dataset.commsChipMessage (the DOM's "
        "camelCased form of data-comms-chip-message)"
    )
