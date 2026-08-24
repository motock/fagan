"""Node-eval harness tests for preserving the `.filter-search` input's
value/focus/cursor-position across the capture/restore cycle in static/app.js.

This story extends the EXISTING narrow save/restore mechanism
(`capturePlanDetailState`/`restorePlanDetailState`) which today only preserves
scroll position and a focused FILTER CHIP. It adds, on the SAME snapshot
object, three new optional fields (`searchValue`, `searchSelectionStart`,
`searchSelectionEnd`) that capture the text a user is actively typing into the
`.filter-search` input when a poll tick re-renders the section out from under
them, and restores them onto the freshly-rendered `<input>` node.

The harness pattern mirrors tests/unit/test_dashboard_notifications_ui.py and
tests/unit/test_dashboard.py: it builds a minimal DOM shim, evals static/app.js
under `node -e`, and JSON-stringifies the result of a test expression. The
shared shim's generic `fakeEl` is NOT sufficient here because it has no real
mutable `.value` and no `.selectionStart`/`.selectionEnd`/`.setSelectionRange()`
on the search input — so this file builds a slightly richer fake specifically
for the search input (mirroring the existing shim's style), while still
standing alone (no import of the other test files).

These tests are RED until the implementation lands: `capturePlanDetailState`
must add `searchValue`/`searchSelectionStart`/`searchSelectionEnd` to its
returned snapshot when the focused element is the `.filter-search` input, and
`restorePlanDetailState` must re-apply value/focus/selection onto the new
search input — all WITHOUT removing or renaming the existing `scrollTop`/
`focusKey` fields or changing the chip-focus behavior.
"""
import json
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")


def _app_js_source():
    """Read static/app.js source for static-source assertions."""
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


# === Harness =================================================================
#
# A richer DOM shim than the bare `fakeEl` in test_dashboard_notifications_ui.py:
# `document.activeElement` is a mutable global the tests point at whichever
# element is "focused", and the search-input fake is a real object (not a
# spread copy of fakeEl) with a mutable `.value` and stub `.focus()`/
# `.setSelectionRange()` methods whose call arguments we record. The section
# fake supports `querySelector(".filter-search")`, `contains()`, `scrollTop`,
# and `dataset` — everything the two functions under test touch.

_SHIM = r"""
const noop = () => {};

// A richer fake specifically for the .filter-search input: a real mutable
// `.value` plus `.selectionStart`/`.selectionEnd` and stub
// `.focus()`/`.setSelectionRange()` that record their call args so tests can
// assert them. Mirrors the existing shim's plain-object style but adds the
// input-specific surface the generic fakeEl lacks.
function makeSearchInput() {
  return {
    value: "",
    selectionStart: 0,
    selectionEnd: 0,
    classList: { add: noop, remove: noop, toggle: noop, contains: () => true },
    addEventListener: noop,
    setAttribute: noop,
    appendChild: noop,
    querySelectorAll: () => [],
    querySelector: () => null,
    dataset: {},
    _focusCalls: 0,
    _setRangeCalls: [],
    focus() { this._focusCalls += 1; },
    setSelectionRange(start, end) { this._setRangeCalls.push([start, end]); },
  };
}

// A fake section element: stores scrollTop, supports contains() and a
// querySelector that returns whichever child we've installed under a class
// key (default: the search input, or null when tests want it absent).
function makeSection(opts) {
  opts = opts || {};
  const searchInput = opts.searchInput === undefined ? makeSearchInput() : opts.searchInput;
  const section = {
    scrollTop: opts.scrollTop || 0,
    dataset: opts.dataset || {},
    _children: {},
    _containsList: opts.containsList || null,
    contains(node) {
      if (this._containsList) return this._containsList.indexOf(node) !== -1;
      return node != null && node !== globalThis.document.body;
    },
    querySelector(sel) {
      if (sel === ".filter-search") return this._children[".filter-search"] || null;
      return null;
    },
    querySelectorAll(sel) { return []; },
  };
  if (searchInput !== null) section._children[".filter-search"] = searchInput;
  return section;
}

const fakeEl = {
  innerHTML: "",
  classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
  addEventListener: noop,
  setAttribute: noop,
  appendChild: noop,
  querySelectorAll: () => [],
  querySelector: () => null,
  dataset: {},
};

globalThis.document = {
  addEventListener: noop,
  documentElement: { dataset: {} },
  body: { _isBody: true },
  // activeElement is a plain mutable reference the tests reassign to point at
  // whichever fake element is "focused". Defaults to body (matches a browser
  // with nothing focused).
  activeElement: null,
  getElementById: () => ({ ...fakeEl, dataset: {}, addEventListener: noop }),
  createElement: () => ({ ...fakeEl, classList: { add: noop, remove: noop, contains: () => false } }),
};
globalThis.document.activeElement = globalThis.document.body;

globalThis.window = {
  location: { hash: "" },
  addEventListener: noop,
};
globalThis.localStorage = { getItem: () => null, setItem: noop };
globalThis.fetch = () => new Promise(() => {});
process.on("unhandledRejection", () => {});
globalThis.setInterval = () => 0;
globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
// CSS.escape is used by the existing chip-restore path; stub it so app.js loads.
globalThis.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&") };
"""


def _run_app_js(expr):
    """Evaluate a JS expression inside an environment where static/app.js has
    been loaded against the richer DOM shim above. Returns the JSON-serialized
    result."""
    script = (
        _SHIM
        + "const fs = require('fs');"
        + f"eval(fs.readFileSync({json.dumps(APP_JS)}, 'utf8'));"
        + "globalThis.state = globalThis.window.state;"
        + "process.stdout.write(JSON.stringify(" + expr + "));"
    )
    proc = subprocess.run(
        ["node", "-e", script],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


# === Static source assertions (trivially checkable, no node needed) ==========

def test_source_exports_both_functions():
    """Both functions must remain exported (the harness reaches them via
    module.exports). A rename or accidental drop would break every test below."""
    src = _app_js_source()
    assert "capturePlanDetailState" in src
    assert "restorePlanDetailState" in src
    # They must still be listed in the module.exports block.
    assert "capturePlanDetailState, restorePlanDetailState" in src


def test_source_existing_fields_not_removed():
    """The existing `scrollTop` and `focusKey` fields must NOT be removed or
    renamed — the story says only ADD to the snapshot object. Assert both
    names still appear in the source."""
    src = _app_js_source()
    assert "scrollTop" in src
    assert "focusKey" in src


def test_source_new_fields_present():
    """The three new snapshot fields must appear by name in the source (the
    implementer must add them)."""
    src = _app_js_source()
    assert "searchValue" in src
    assert "searchSelectionStart" in src
    assert "searchSelectionEnd" in src


def test_source_uses_setSelectionRange_and_filter_search_selector():
    """The restore path must query `.filter-search` and call
    `.setSelectionRange(...)`; assert both tokens appear in the source."""
    src = _app_js_source()
    assert ".filter-search" in src
    assert "setSelectionRange" in src


# === capturePlanDetailState ==================================================

def test_capture_includes_searchValue_when_search_input_focused():
    """When the currently-focused element (document.activeElement) IS the
    `.filter-search` input and its `.value` is "foo", the snapshot must include
    `searchValue: "foo"` plus the selection start/end."""
    expr = (
        "(() => {"
        " const section = makeSection();"
        " const searchInput = section.querySelector('.filter-search');"
        " searchInput.value = 'foo';"
        " searchInput.selectionStart = 2;"
        " searchInput.selectionEnd = 3;"
        " globalThis.document.activeElement = searchInput;"
        " const snap = capturePlanDetailState(section);"
        " return {"
        "   searchValue: snap.searchValue,"
        "   searchSelectionStart: snap.searchSelectionStart,"
        "   searchSelectionEnd: snap.searchSelectionEnd,"
        "   scrollTop: snap.scrollTop,"
        "   focusKey: snap.focusKey,"
        " };"
        "})()"
    )
    result = _run_app_js(expr)
    assert result["searchValue"] == "foo"
    assert result["searchSelectionStart"] == 2
    assert result["searchSelectionEnd"] == 3
    # Existing fields must still be present and intact (no regression).
    assert "scrollTop" in result
    assert result["focusKey"] is None


def test_capture_no_searchValue_when_search_input_not_focused():
    """When the search input is NOT the focused element (e.g. a filter chip is
    focused, or nothing is focused), the snapshot must NOT carry a
    `searchValue` — matching today's existing behavior. We assert the key is
    absent (undefined serializes to absence in JSON)."""
    expr = (
        "(() => {"
        " const section = makeSection();"
        " const searchInput = section.querySelector('.filter-search');"
        " searchInput.value = 'foo';"
        " globalThis.document.activeElement = globalThis.document.body;"
        " const snap = capturePlanDetailState(section);"
        " return {"
        "   hasSearchValue: ('searchValue' in snap),"
        "   searchValue: snap.searchValue,"
        "   scrollTop: snap.scrollTop,"
        "   focusKey: snap.focusKey,"
        " };"
        "})()"
    )
    result = _run_app_js(expr)
    # We require the key be ABSENT (undefined). JSON.stringify drops undefined
    # keys, so 'searchValue' in snap being false means it was never set.
    assert result["hasSearchValue"] is False
    assert result.get("searchValue") is None  # JSON-serialized undefined -> null
    # Existing fields intact.
    assert "scrollTop" in result
    assert result["focusKey"] is None


def test_capture_no_searchValue_when_chip_focused():
    """Boundary: when a FILTER CHIP (not the search input) is focused, the
    existing chip-focus path must still produce a `focusKey` and must NOT
    populate `searchValue` — the chip-focus behavior must not regress."""
    expr = (
        "(() => {"
        " const section = makeSection();"
        " const chip = {"
        "   dataset: { dim: 'status', value: 'done' },"
        "   classList: { contains: () => true },"
        "   focus: noop,"
        " };"
        " globalThis.document.activeElement = chip;"
        " section._containsList = [chip];"
        " const snap = capturePlanDetailState(section);"
        " return {"
        "   hasSearchValue: ('searchValue' in snap),"
        "   focusKey: snap.focusKey,"
        " };"
        "})()"
    )
    result = _run_app_js(expr)
    assert result["hasSearchValue"] is False
    assert result["focusKey"] == "status\u0000done"


def test_capture_null_section_returns_existing_fields():
    """Boundary: a null section must still return the existing
    `{scrollTop: 0, focusKey: null}` shape and must not throw — and must not
    invent a searchValue."""
    expr = (
        "(() => {"
        " const snap = capturePlanDetailState(null);"
        " return {"
        "   scrollTop: snap.scrollTop,"
        "   focusKey: snap.focusKey,"
        "   hasSearchValue: ('searchValue' in snap),"
        " };"
        "})()"
    )
    result = _run_app_js(expr)
    assert result["scrollTop"] == 0
    assert result["focusKey"] is None
    assert result["hasSearchValue"] is False


# === restorePlanDetailState ==================================================

def test_restore_sets_value_and_focuses_search_input():
    """Given a snapshot with `searchValue: "foo"`, restore must find the NEW
    `.filter-search` input in the freshly-rendered section, set its `.value`
    to "foo", call `.focus()`, and restore the cursor via
    `.setSelectionRange(start, end)`."""
    expr = (
        "(() => {"
        " const section = makeSection();"
        " const newSearch = section.querySelector('.filter-search');"
        " const snap = {"
        "   scrollTop: 0, focusKey: null,"
        "   searchValue: 'foo',"
        "   searchSelectionStart: 2, searchSelectionEnd: 3,"
        " };"
        " restorePlanDetailState(section, snap);"
        " return {"
        "   value: newSearch.value,"
        "   focusCalls: newSearch._focusCalls,"
        "   setRangeCalls: newSearch._setRangeCalls,"
        " };"
        "})()"
    )
    result = _run_app_js(expr)
    assert result["value"] == "foo"
    assert result["focusCalls"] == 1
    assert result["setRangeCalls"] == [[2, 3]]


def test_restore_selection_defaults_when_omitted():
    """Boundary: a snapshot with `searchValue` but missing/undefined selection
    start/end must not throw and must still set the value + focus. We pass
    only searchValue; selection fields are absent (undefined)."""
    expr = (
        "(() => {"
        " const section = makeSection();"
        " const newSearch = section.querySelector('.filter-search');"
        " const snap = { scrollTop: 0, focusKey: null, searchValue: 'foo' };"
        " let threw = false;"
        " try { restorePlanDetailState(section, snap); } catch (e) { threw = true; }"
        " return {"
        "   threw, value: newSearch.value, focusCalls: newSearch._focusCalls,"
        " };"
        "})()"
    )
    result = _run_app_js(expr)
    assert result["threw"] is False
    assert result["value"] == "foo"
    assert result["focusCalls"] == 1


def test_restore_without_searchValue_does_not_touch_search_input():
    """Negative: a snapshot with NO `searchValue` (the existing chip-focus-only
    case) must NOT touch the search input at all and must not throw — this must
    match today's existing chip-restore behavior exactly."""
    expr = (
        "(() => {"
        " const section = makeSection();"
        " const newSearch = section.querySelector('.filter-search');"
        " newSearch.value = 'untouched';"
        " const snap = { scrollTop: 5, focusKey: null };"
        " let threw = false;"
        " try { restorePlanDetailState(section, snap); } catch (e) { threw = true; }"
        " return {"
        "   threw, value: newSearch.value, focusCalls: newSearch._focusCalls,"
        "   setRangeCalls: newSearch._setRangeCalls,"
        " };"
        "})()"
    )
    result = _run_app_js(expr)
    assert result["threw"] is False
    assert result["value"] == "untouched"
    assert result["focusCalls"] == 0
    assert result["setRangeCalls"] == []


def test_restore_chip_focus_still_works_without_searchValue():
    """Negative/regression: the existing chip-focus restore path must still
    function when there is no searchValue — i.e. a snapshot with a focusKey
    pointing at a chip must still focus that chip and must not throw, and must
    not touch the search input."""
    expr = (
        "(() => {"
        " const section = makeSection();"
        " const newSearch = section.querySelector('.filter-search');"
        " newSearch.value = 'untouched';"
        " const chip = {"
        "   classList: { contains: () => true },"
        "   _focusCalls: 0,"
        "   focus() { this._focusCalls += 1; },"
        " };"
        " section.querySelector = (sel) => {"
        "   if (sel === '.filter-search') return newSearch;"
        "   if (sel && sel.indexOf('.filter-chip') === 0) return chip;"
        "   return null;"
        " };"
        " const snap = { scrollTop: 0, focusKey: 'status\\u0000done' };"
        " let threw = false;"
        " try { restorePlanDetailState(section, snap); } catch (e) { threw = true; }"
        " return {"
        "   threw, chipFocusCalls: chip._focusCalls,"
        "   searchValue: newSearch.value, searchFocusCalls: newSearch._focusCalls,"
        " };"
        "})()"
    )
    result = _run_app_js(expr)
    assert result["threw"] is False
    assert result["chipFocusCalls"] == 1
    assert result["searchValue"] == "untouched"
    assert result["searchFocusCalls"] == 0


def test_restore_does_not_throw_when_no_search_input_present():
    """Negative: when the new section has NO `.filter-search` element at all
    (e.g. filters produced zero results and the search box isn't rendered),
    restore with a searchValue must NOT throw — the existing try/catch around
    the call site must be respected, and the function itself must guard a
    missing input."""
    expr = (
        "(() => {"
        " const section = makeSection({ searchInput: null });"
        " const snap = {"
        "   scrollTop: 0, focusKey: null,"
        "   searchValue: 'foo',"
        "   searchSelectionStart: 0, searchSelectionEnd: 0,"
        " };"
        " let threw = false;"
        " let err = null;"
        " try { restorePlanDetailState(section, snap); } catch (e) { threw = true; err = String(e); }"
        " return { threw, err, scrollTop: section.scrollTop };"
        "})()"
    )
    result = _run_app_js(expr)
    assert result["threw"] is False
    # scrollTop must still have been restored (the search-restore failure must
    # not skip the scroll restore that already happened).
    assert result["scrollTop"] == 0


def test_restore_null_section_or_snapshot_does_not_throw():
    """Boundary: null section or null snapshot must not throw (existing guard)."""
    expr = (
        "(() => {"
        " let threw = false;"
        " try { restorePlanDetailState(null, { searchValue: 'foo' }); } catch (e) { threw = true; }"
        " try { restorePlanDetailState(makeSection(), null); } catch (e) { threw = true; }"
        " return { threw };"
        "})()"
    )
    result = _run_app_js(expr)
    assert result["threw"] is False


def test_restore_empty_searchValue_string_still_focuses():
    """Boundary: an empty-string searchValue (user typed then cleared, but the
    box is still focused) is a truthy-presence case for the restore path — the
    snapshot carries searchValue: '' which means the box WAS focused, so we
    must still set value to '' and focus. (Distinguishes 'focused with empty
    text' from 'not focused at all' where the key is absent.)"""
    expr = (
        "(() => {"
        " const section = makeSection();"
        " const newSearch = section.querySelector('.filter-search');"
        " newSearch.value = 'stale';"
        " const snap = {"
        "   scrollTop: 0, focusKey: null,"
        "   searchValue: '',"
        "   searchSelectionStart: 0, searchSelectionEnd: 0,"
        " };"
        " let threw = false;"
        " try { restorePlanDetailState(section, snap); } catch (e) { threw = true; }"
        " return { threw, value: newSearch.value, focusCalls: newSearch._focusCalls };"
        "})()"
    )
    result = _run_app_js(expr)
    assert result["threw"] is False
    assert result["value"] == ""
    assert result["focusCalls"] == 1


# === Round-trip: capture then restore onto a fresh section ====================

def test_roundtrip_capture_then_restore_preserves_value_focus_selection():
    """End-to-end within the harness: capture from a section whose search
    input is focused with value 'foo' and a selection, then restore that
    snapshot onto a FRESH section (simulating a re-render producing a new
    `<input>` node). The new input must end up with value 'foo', focused, and
    the same selection range."""
    expr = (
        "(() => {"
        " const oldSection = makeSection();"
        " const oldSearch = oldSection.querySelector('.filter-search');"
        " oldSearch.value = 'foo';"
        " oldSearch.selectionStart = 1;"
        " oldSearch.selectionEnd = 3;"
        " globalThis.document.activeElement = oldSearch;"
        " const snap = capturePlanDetailState(oldSection);"
        " const newSection = makeSection();"
        " const newSearch = newSection.querySelector('.filter-search');"
        " restorePlanDetailState(newSection, snap);"
        " return {"
        "   searchValue: snap.searchValue,"
        "   newValue: newSearch.value,"
        "   focusCalls: newSearch._focusCalls,"
        "   setRangeCalls: newSearch._setRangeCalls,"
        "   scrollTop: newSection.scrollTop,"
        " };"
        "})()"
    )
    result = _run_app_js(expr)
    assert result["searchValue"] == "foo"
    assert result["newValue"] == "foo"
    assert result["focusCalls"] == 1
    assert result["setRangeCalls"] == [[1, 3]]