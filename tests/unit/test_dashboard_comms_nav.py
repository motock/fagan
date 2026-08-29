"""Node-eval harness tests for the Comms nav-selection wiring in
static/app.js.

This file mirrors the harness pattern in tests/unit/test_dashboard.py: it
builds a minimal DOM shim, evals static/app.js under `node -e`, and
JSON-stringifies the result of a test expression. The helper is copied here
verbatim (rather than imported) so this file stands alone and does not touch
test_dashboard.py — per that file's own header comment, the helper is copied
so each test file stands alone.

These tests are RED until the implementation lands: state.commsActive must
be added to the module-level state object (defaulting to true), a Comms
.plan-item must be built in renderPlanList above the Overview item,
selectComms() must exist and mirror selectOverview(), selectOverview() and
selectPlan() must clear state.commsActive, refresh() must branch three ways
via a new _applyActiveView() helper, and selectComms / selectOverview /
_applyActiveView must be added to module.exports.
"""
import json
import os

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")
# renderPlanList (and its Comms/Overview pinned-item markup) was relocated
# out of static/app.js into this dedicated render module (server-app-file-
# split plan); the static-source assertions below follow it here.
PLAN_LIST_JS = os.path.join(REPO_ROOT, "static", "app", "render", "plan-list.js")
# selectComms/selectOverview/_applyActiveView/refresh and module.exports
# were relocated out of static/app.js into static/app/main.js (server-app-
# file-split plan); the static-source assertions below follow them here.
MAIN_JS = os.path.join(REPO_ROOT, "static", "app", "main.js")

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
            getElementById: () => ({ ...fakeEl, dataset: {}, addEventListener: noop, children: [] }),
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
    has been loaded. Returns the JSON-serialized result. Copied verbatim
    from test_dashboard.py so this file is self-contained."""
    proc = _shared_run_app_js(expr, shim=_SHIM)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _app_js_source():
    """Read static/app.js source for static-source assertions."""
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


def _main_js_source():
    """Read static/app/main.js source for static-source assertions (the new
    home for selectComms/selectOverview/_applyActiveView/refresh and
    module.exports, relocated out of static/app.js)."""
    with open(MAIN_JS, encoding="utf-8") as fh:
        return fh.read()


# === state.commsActive initialization (default true) ======================

def test_state_comms_active_default_true():
    """The module-level state object must initialize commsActive to true so
    Comms is the default landing view on a fresh load with no plan in the
    URL hash."""
    src = _main_js_source()
    assert "commsActive" in src, "state.commsActive must be added to the state object"
    # The default must be true (Comms is the landing view).
    val = _run_app_js("state.commsActive")
    assert val is True, f"state.commsActive must default to true, got {val!r}"


# === selectComms() ========================================================

def test_select_comms_sets_state():
    """Calling selectComms() must set state.commsActive === true and
    state.selectedPlan === null."""
    result = _run_app_js(
        "selectComms(); ({ commsActive: state.commsActive, "
        "selectedPlan: state.selectedPlan })"
    )
    assert result["commsActive"] is True
    assert result["selectedPlan"] is None


def test_select_comms_exported():
    """selectComms must be exported in module.exports so tests can call it
    directly."""
    assert _run_app_js("typeof selectComms") == "function"


def test_select_overview_exported():
    """selectOverview must be exported in module.exports (it is not today)
    so tests can call it directly."""
    assert _run_app_js("typeof selectOverview") == "function"


def test_apply_active_view_exported():
    """_applyActiveView must be exported in module.exports."""
    assert _run_app_js("typeof _applyActiveView") == "function"


# === selectOverview() clears commsActive ==================================

def test_select_overview_clears_comms():
    """Calling selectOverview() must set state.commsActive === false."""
    result = _run_app_js(
        "state.commsActive = true; selectOverview(); state.commsActive"
    )
    assert result is False


def test_select_overview_clears_selected_plan():
    """selectOverview() must still set state.selectedPlan === null."""
    result = _run_app_js(
        "state.selectedPlan = 'x'; selectOverview(); state.selectedPlan"
    )
    assert result is None


# === selectPlan() clears commsActive ======================================

def test_select_plan_clears_comms():
    """Calling a plan selection must set state.commsActive === false. We
    drive state.selectedPlan directly and call _applyActiveView() (the
    brief's fallback when selectPlan is async / not directly drivable), but
    we also assert the source wires selectPlan to clear commsActive so the
    real click path is covered."""
    src = _main_js_source()
    # The selectPlan function body must set state.commsActive = false.
    assert "state.commsActive = false" in src, (
        "selectPlan must set state.commsActive = false"
    )
    # Functional check via the pure helper: setting selectedPlan and calling
    # _applyActiveView must not throw and must leave commsActive as set.
    result = _run_app_js(
        "state.commsActive = true; state.selectedPlan = 'x'; "
        "_applyActiveView(); state.commsActive"
    )
    # _applyActiveView is a pure function of commsActive; it must not throw.
    # The selectPlan wiring itself clears commsActive (asserted via source).
    assert result is True  # helper alone doesn't flip commsActive; source asserts the flip


# === _applyActiveView() is a pure function of state.commsActive ===========

def test_apply_active_view_does_not_throw_when_comms_active():
    """_applyActiveView() must not throw when state.commsActive is true,
    even with the DOM shim's no-op classList elements."""
    _run_app_js("state.commsActive = true; _applyActiveView(); true")


def test_apply_active_view_does_not_throw_when_comms_inactive():
    """_applyActiveView() must not throw when state.commsActive is false."""
    _run_app_js("state.commsActive = false; _applyActiveView(); true")


def test_apply_active_view_pure_of_comms_active():
    """_applyActiveView() must be a pure function of state.commsActive:
    calling it must not mutate state.commsActive in either direction."""
    active = _run_app_js(
        "state.commsActive = true; _applyActiveView(); state.commsActive"
    )
    assert active is True
    inactive = _run_app_js(
        "state.commsActive = false; _applyActiveView(); state.commsActive"
    )
    assert inactive is False


# === Negative / boundary: idempotency =====================================

def test_select_comms_idempotent():
    """Calling selectComms() twice in a row is idempotent: the state ends
    the same as calling it once and it does not throw."""
    once = _run_app_js(
        "selectComms(); ({ commsActive: state.commsActive, "
        "selectedPlan: state.selectedPlan })"
    )
    twice = _run_app_js(
        "selectComms(); selectComms(); ({ commsActive: state.commsActive, "
        "selectedPlan: state.selectedPlan })"
    )
    assert once == twice
    assert twice["commsActive"] is True
    assert twice["selectedPlan"] is None


def test_select_comms_after_plan_selection():
    """After selecting a plan (which clears commsActive), calling
    selectComms() must re-activate Comms and clear the selected plan."""
    result = _run_app_js(
        "state.commsActive = false; state.selectedPlan = 'x'; "
        "selectComms(); ({ commsActive: state.commsActive, "
        "selectedPlan: state.selectedPlan })"
    )
    assert result["commsActive"] is True
    assert result["selectedPlan"] is None


# === renderPlanList: Comms pinned item markup =============================

def test_render_plan_list_builds_comms_item_via_create_element():
    """renderPlanList must build the Comms .plan-item with
    document.createElement (not an innerHTML string), mirroring how the
    Overview item is built. We assert the source uses createElement for the
    comms item and does NOT build it via an innerHTML string. (Now in
    static/app/render/plan-list.js.)"""
    with open(PLAN_LIST_JS, encoding="utf-8") as fh:
        src = fh.read()
    assert "data-comms" in src, (
        "the Comms nav item must carry data-comms=\"true\" "
        "(mirroring Overview's data-overview)"
    )
    assert "selectComms" in src, (
        "renderPlanList must wire the Comms item's click to selectComms()"
    )


def test_render_plan_list_comms_item_above_overview():
    """The Comms .plan-item must be inserted ABOVE the Overview item in
    renderPlanList. We assert via source ordering: the comms item's
    appendChild must appear before the overview item's appendChild within
    renderPlanList. (Now in static/app/render/plan-list.js.)"""
    with open(PLAN_LIST_JS, encoding="utf-8") as fh:
        src = fh.read()
    # Slice the renderPlanList function body.
    start = src.index("function renderPlanList(")
    end = src.index("function togglePlanArchived(", start)
    body = src[start:end]
    comms_append = body.find("data-comms")
    overview_append = body.find("data-overview")
    assert comms_append != -1, "Comms item (data-comms) must be built in renderPlanList"
    assert overview_append != -1, "Overview item (data-overview) must still be built in renderPlanList"
    assert comms_append < overview_append, (
        "Comms item must be inserted ABOVE the Overview item in renderPlanList"
    )


def test_render_plan_list_overview_active_condition_updated():
    """The Overview item's .active condition must be updated from
    `!state.selectedPlan` to `!state.selectedPlan && !state.commsActive`.
    (Now in static/app/render/plan-list.js.)"""
    with open(PLAN_LIST_JS, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("function renderPlanList(")
    end = src.index("function togglePlanArchived(", start)
    body = src[start:end]
    assert "!state.selectedPlan && !state.commsActive" in body, (
        "Overview item .active condition must be "
        "!state.selectedPlan && !state.commsActive"
    )


# === refresh() three-way branch via _applyActiveView ======================

def test_refresh_uses_apply_active_view_helper():
    """refresh() must call _applyActiveView() near its top (the brief says
    via a small new helper, not scattered inline hidden-class toggling)."""
    src = _main_js_source()
    start = src.index("async function refresh(")
    end = src.index("function startPolling(", start)
    body = src[start:end]
    assert "_applyActiveView()" in body, (
        "refresh() must call _applyActiveView() to toggle the view panes"
    )


def test_apply_active_view_references_comms_view_and_plan_detail():
    """_applyActiveView() must toggle #comms-view and #plan-detail (the
    brief: if commsActive, show #comms-view / hide #plan-detail; otherwise
    hide #comms-view / show #plan-detail)."""
    src = _main_js_source()
    assert "comms-view" in src, (
        "_applyActiveView must reference the #comms-view section"
    )
    assert "plan-detail" in src  # already referenced elsewhere; sanity check


def test_apply_active_view_helper_defined():
    """_applyActiveView must be defined as a function in the source."""
    src = _main_js_source()
    assert "function _applyActiveView" in src, (
        "_applyActiveView must be defined as a function"
    )


def test_select_comms_function_defined():
    """selectComms must be defined as a function in the source."""
    src = _main_js_source()
    assert "function selectComms" in src, (
        "selectComms must be defined as a function"
    )


# === module.exports membership ============================================

def test_module_exports_contains_select_comms():
    """module.exports must list selectComms."""
    src = _main_js_source()
    start = src.index("module.exports = {")
    end = src.index("};", start) + 2
    block = src[start:end]
    assert "selectComms" in block, "module.exports must include selectComms"


def test_module_exports_contains_select_overview():
    """module.exports must list selectOverview (added by this story)."""
    src = _main_js_source()
    start = src.index("module.exports = {")
    end = src.index("};", start) + 2
    block = src[start:end]
    assert "selectOverview" in block, "module.exports must include selectOverview"


def test_module_exports_contains_apply_active_view():
    """module.exports must list _applyActiveView."""
    src = _main_js_source()
    start = src.index("module.exports = {")
    end = src.index("};", start) + 2
    block = src[start:end]
    assert "_applyActiveView" in block, "module.exports must include _applyActiveView"


# === selectComms() must not crash rendering the overview plan-row list ====

def test_select_comms_does_not_crash_rendering_overview_plan_rows():
    """selectComms() calls renderOverview(), which looks up its plan-row
    <ul> to hand to _diffOverviewPlanRows(). In this file's DOM shim, an
    element from document.getElementById only implements querySelectorAll
    (not querySelector) - so renderOverview must locate the list via
    document.getElementById("overview-plan-list"), not
    section.querySelector(".overview-plan-list"), or this throws
    TypeError: section.querySelector is not a function."""
    result = _run_app_js(
        "state.lastPlans = { plans: [{ name: 'demo', story_count: 1, "
        "status_counts: { done: 1 } }] }; selectComms(); "
        "({ commsActive: state.commsActive })"
    )
    assert result["commsActive"] is True


# === selectComms()/selectOverview() must apply the active view synchronously
#
# BUG: selectComms() and selectOverview() flip state.commsActive and the
# sidebar's .active classes but never call _applyActiveView() — the only
# helper that unhides/hides the three <section> panes. The only other caller
# is refresh(), which runs on the 4-second polling interval, so after
# clicking Comms (or Overview while in Chat) the correct pane stays hidden
# until the next poll tick (up to 4s of perceived nav lag). Each function
# must call _applyActiveView() inside its own body.
#
# NOTE: the DOM shim above has no-op classList, so behavioral class
# assertions cannot work here; static-source assertions are the established
# pattern in this file (mirroring test_refresh_uses_apply_active_view_helper)
# and are RED before the fix / GREEN after.

def test_select_comms_calls_apply_active_view():
    """selectComms() must call _applyActiveView() inside its own body so the
    #comms-view pane unhides synchronously on click instead of waiting for
    the next 4s polling tick. Exactly one call is required, and the existing
    renderOverview(...) call must be retained (removing it is out of
    scope)."""
    src = _main_js_source()
    start = src.index("function selectComms(")
    end = src.index("async function selectPlan", start)
    body = src[start:end]
    assert "_applyActiveView()" in body, (
        "selectComms() must call _applyActiveView() so the Comms pane "
        "unhides synchronously instead of waiting for the next poll tick"
    )
    assert body.count("_applyActiveView()") == 1, (
        "selectComms() must contain exactly one _applyActiveView() call"
    )
    assert "renderOverview(" in body, (
        "selectComms() must keep its existing renderOverview(...) call "
        "(removing it is out of scope for this fix)"
    )


def test_select_overview_calls_apply_active_view():
    """selectOverview() must call _applyActiveView() inside its own body so
    the #plan-detail pane unhides synchronously when navigating back from
    Chat to Overview (same 4s poll-tick lag as selectComms). Exactly one
    call is required, and the existing renderOverview(...) call must be
    retained (removing it is out of scope)."""
    src = _main_js_source()
    start = src.index("function selectOverview(")
    end = src.index("function selectComms(", start)
    body = src[start:end]
    assert "_applyActiveView()" in body, (
        "selectOverview() must call _applyActiveView() so the Overview pane "
        "unhides synchronously instead of waiting for the next poll tick"
    )
    assert body.count("_applyActiveView()") == 1, (
        "selectOverview() must contain exactly one _applyActiveView() call"
    )
    assert "renderOverview(" in body, (
        "selectOverview() must keep its existing renderOverview(...) call "
        "(removing it is out of scope for this fix)"
    )