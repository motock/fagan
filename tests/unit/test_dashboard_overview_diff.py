"""Node-eval harness tests for the keyed-diff refactor of renderOverview's
compact plan-row list in static/app.js.

This file mirrors the harness pattern in tests/unit/test_dashboard_planlist_diff.py
(itself mirroring tests/unit/test_dashboard.py): it builds a minimal (but
richer) DOM shim, evals static/app.js under `node -e`, and JSON-stringifies
the result of a test expression. The shim is copied + extended here (rather
than imported) so this file stands alone and does not touch the other test
files.

EXTENSION TO THE SHIM (this file only): `document.createElement` tags every
created element with a unique incrementing `__testId` number, and
`appendChild` / `innerHTML=""` actually maintain a `children` array on each
node, and `innerHTML` parses simple open/close tag pairs so
`querySelector`/`querySelectorAll` can locate `.overview-plan-meta` and
`.overview-plan-name`. This lets a test detect whether an
`.overview-plan-row` was REUSED across two `_diffOverviewPlanRows` calls
(same `__testId`) or RECREATED (new `__testId`).

These tests are RED until the implementation lands:
  - a new `_diffOverviewPlanRows(listEl, plans)` function must exist that
    performs a keyed diff by plan name, mirroring renderPlanList's
    per-plan add/update/remove-by-name pattern,
  - each per-plan `<li class="overview-plan-row">` row must carry a
    `data-plan` attribute (already present) used as the diff key,
  - `_diffOverviewPlanRows` must be added to module.exports.
"""
import json
import os

from tests.unit import _dom_shims
from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")
# renderOverview/_diffOverviewPlanRows were relocated out of static/app.js
# into this dedicated render module (server-app-file-split plan); the
# static-source assertions below follow them here.
OVERVIEW_JS = os.path.join(REPO_ROOT, "static", "app", "render", "overview.js")


# A self-contained, richer DOM shim. Built once as a Python string and
# embedded in every node invocation. Implements just enough of the DOM for
# _diffOverviewPlanRows: createElement with unique __testId, appendChild
# maintaining a children array, innerHTML="" clearing children, innerHTML
# parsing simple open/close tag pairs into child nodes, and
# querySelector/querySelectorAll matching a single class selector.
_SHIM = _dom_shims.OVERVIEW_DIFF_SHIM


def _run_app_js(expr):
    """Evaluate a JS expression inside an environment where static/app.js
    has been loaded. Returns the JSON-serialized result."""
    proc = _shared_run_app_js(expr, shim=_SHIM)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _app_js_source():
    """Read static/app.js source for static-source assertions."""
    with open(APP_JS, encoding="utf-8") as fh:
        return fh.read()


def _plan(name, done=0, total=0, paused=False):
    """Build a plan dict shaped like /api/plans entries."""
    return {
        "name": name,
        "story_count": total,
        "status_counts": {"done": done},
        "paused": paused,
    }


# === Static-source assertions ===============================================

def test_diff_overview_plan_rows_function_exists():
    with open(OVERVIEW_JS, encoding="utf-8") as fh:
        js = fh.read()
    assert "function _diffOverviewPlanRows(listEl, plans)" in js


def test_diff_overview_plan_rows_is_exported():
    expr = (
        "(() => {"
        " const out = (typeof module !== 'undefined' && module.exports) ? module.exports : {};"
        " return typeof out._diffOverviewPlanRows;"
        " })()"
    )
    assert _run_app_js(expr) == "function"


def test_render_overview_no_longer_builds_planlisthtml_join_string():
    """renderOverview must no longer build a joined planListHtml string
    assigned via innerHTML for the plan-row list; it delegates to
    _diffOverviewPlanRows instead. (Now in static/app/render/overview.js.)"""
    with open(OVERVIEW_JS, encoding="utf-8") as fh:
        js = fh.read()
    assert "const planListHtml" not in js
    assert "_diffOverviewPlanRows(" in js


# === Harness helper: build a fresh <ul>, call the diff twice =================

def _collect_rows(list_el_expr="listEl"):
    return (
        "(() => {"
        f" const rows = [];"
        f" for (const c of ({list_el_expr}).__children) {{"
        "   rows.push({"
        "     plan: c.dataset ? c.dataset['plan'] : undefined,"
        "     className: c.className || '',"
        "     testId: c.__testId,"
        "     meta: (() => { const m = c.querySelector('.overview-plan-meta'); return m ? (m.__text || m.textContent || '') : ''; })(),"
        "   });"
        " }"
        " return rows;"
        " })()"
    )


def _diff_twice(plans_a, plans_b):
    """Create one <ul> listEl, call _diffOverviewPlanRows twice against it
    (module-level diff state is NOT reset between calls - that's the point),
    and return { first: [...], second: [...] } describing its children."""
    expr = (
        "(() => {"
        " const listEl = document.createElement('ul');"
        f" _diffOverviewPlanRows(listEl, {json.dumps(plans_a)});"
        f" const first = {_collect_rows()};"
        f" _diffOverviewPlanRows(listEl, {json.dumps(plans_b)});"
        f" const second = {_collect_rows()};"
        " return { first, second };"
        " })()"
    )
    return _run_app_js(expr)


def _by_plan(rows):
    return {r["plan"]: r for r in rows if r["plan"]}


# === Identical plans: no row recreated ======================================

def test_identical_plans_second_call_does_not_recreate_rows():
    plans = [_plan("alpha", done=1, total=3), _plan("beta", done=2, total=5)]
    res = _diff_twice(plans, plans)
    first, second = _by_plan(res["first"]), _by_plan(res["second"])
    assert set(first) == set(second) == {"alpha", "beta"}
    for name in ("alpha", "beta"):
        assert first[name]["testId"] == second[name]["testId"], (
            f"row {name} was recreated across identical calls"
        )


def test_identical_plans_second_call_preserves_meta():
    plans = [_plan("alpha", done=1, total=3)]
    res = _diff_twice(plans, plans)
    second = _by_plan(res["second"])
    assert "1/3" in second["alpha"]["meta"]


# === Changed done/total count: same node, updated text ======================

def test_changed_done_count_reuses_node_and_updates_meta():
    a = [_plan("alpha", done=1, total=3)]
    b = [_plan("alpha", done=2, total=3)]
    res = _diff_twice(a, b)
    first, second = _by_plan(res["first"]), _by_plan(res["second"])
    assert first["alpha"]["testId"] == second["alpha"]["testId"], (
        "row alpha was recreated when only its done count changed"
    )
    assert "1/3" in first["alpha"]["meta"]
    assert "2/3" in second["alpha"]["meta"]
    assert "1/3" not in second["alpha"]["meta"]


def test_changed_total_count_reuses_node_and_updates_meta():
    a = [_plan("alpha", done=1, total=3)]
    b = [_plan("alpha", done=1, total=4)]
    res = _diff_twice(a, b)
    first, second = _by_plan(res["first"]), _by_plan(res["second"])
    assert first["alpha"]["testId"] == second["alpha"]["testId"]
    assert "1/3" in first["alpha"]["meta"]
    assert "1/4" in second["alpha"]["meta"]


def test_changed_paused_state_reuses_node_and_updates_meta():
    a = [_plan("alpha", done=1, total=3, paused=False)]
    b = [_plan("alpha", done=1, total=3, paused=True)]
    res = _diff_twice(a, b)
    first, second = _by_plan(res["first"]), _by_plan(res["second"])
    assert first["alpha"]["testId"] == second["alpha"]["testId"]
    assert "paused" not in first["alpha"]["meta"].lower()
    assert "paused" in second["alpha"]["meta"].lower()


# === New plan added: new row appears, existing rows unchanged ===============

def test_new_plan_added_creates_new_row_preserves_existing():
    a = [_plan("alpha", done=1, total=3)]
    b = [_plan("alpha", done=1, total=3), _plan("beta", done=0, total=2)]
    res = _diff_twice(a, b)
    first, second = _by_plan(res["first"]), _by_plan(res["second"])
    assert set(first) == {"alpha"}
    assert set(second) == {"alpha", "beta"}
    assert first["alpha"]["testId"] == second["alpha"]["testId"], (
        "existing row alpha was recreated when a new plan was added"
    )
    assert "0/2" in second["beta"]["meta"]


# === Plan removed: row gone, remaining rows unchanged ========================

def test_plan_removed_drops_row_preserves_remaining():
    a = [_plan("alpha", done=1, total=3), _plan("beta", done=0, total=2)]
    b = [_plan("alpha", done=1, total=3)]
    res = _diff_twice(a, b)
    first, second = _by_plan(res["first"]), _by_plan(res["second"])
    assert set(first) == {"alpha", "beta"}
    assert set(second) == {"alpha"}
    assert "beta" not in second
    assert first["alpha"]["testId"] == second["alpha"]["testId"], (
        "remaining row alpha was recreated when a plan was removed"
    )


# === Negative / boundary =====================================================

def test_empty_plans_list_does_not_throw_and_renders_empty_state():
    """_diffOverviewPlanRows(listEl, []) on a fresh list does not throw and
    renders the 'No plans yet.' empty state."""
    expr = (
        "(() => {"
        " let ok = true, err = null, rows = [];"
        " try {"
        "  const listEl = document.createElement('ul');"
        "  _diffOverviewPlanRows(listEl, []);"
        f"  rows = {_collect_rows()};"
        " } catch (e) { ok = false; err = String(e); }"
        " return { ok, err, rows };"
        " })()"
    )
    res = _run_app_js(expr)
    assert res["ok"] is True, f"_diffOverviewPlanRows(listEl, []) threw: {res.get('err')}"
    assert len(res["rows"]) == 1
    assert res["rows"][0]["className"] == "overview-empty"


def test_populated_then_empty_removes_all_rows_and_restores_empty_state():
    """A list that previously had rows, diffed against [], removes all of
    them without throwing and the empty-state <li> is added back."""
    plans = [_plan("alpha", done=1, total=3), _plan("beta", done=0, total=2)]
    res = _diff_twice(plans, [])
    first, second = res["first"], res["second"]
    assert len(first) == 2
    assert len(second) == 1
    assert second[0]["className"] == "overview-empty"


def test_zero_done_zero_total_meta_rendered():
    plans = [_plan("zero", done=0, total=0)]
    res = _diff_twice(plans, plans)
    second = _by_plan(res["second"])
    assert "0/0" in second["zero"]["meta"]
