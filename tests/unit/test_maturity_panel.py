"""Source-level tests for the maturity panel (a3-maturity-metrics).

The dashboard frontend has a node-based harness for app.js behavior, but this
story's contract is intentionally narrow: the renderer module must exist,
export a render function, reference the two maturity endpoints as literal
strings, and be registered from the shared entry point. These are membership
assertions on the source — deliberately NOT full-file equality or hashes, so
sibling stories can keep editing the shared files without breaking us.
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MATURITY_JS = REPO_ROOT / "static" / "app" / "render" / "maturity.js"
MAIN_JS = REPO_ROOT / "static" / "app" / "main.js"
API_JS = REPO_ROOT / "static" / "app" / "api.js"
PLAN_DETAIL_JS = REPO_ROOT / "static" / "app" / "render" / "plan-detail.js"


def _read(path: pathlib.Path) -> str:
    assert path.exists(), f"expected file to exist: {path}"
    return path.read_text(encoding="utf-8")


# ---------- static/app/render/maturity.js ----------

def test_maturity_module_exists_and_exports_render_function():
    src = _read(MATURITY_JS)
    assert "export" in src, "maturity.js must export its renderer"
    assert re.search(r"export\s+(async\s+)?function\s+renderMaturity", src), (
        "maturity.js must export a renderMaturity* function"
    )


def test_maturity_module_references_both_endpoint_paths():
    src = _read(MATURITY_JS)
    assert "/api/guard-liveness" in src
    assert "/api/plans/" in src
    assert "/metrics" in src


def test_maturity_module_handles_negative_cases():
    src = _read(MATURITY_JS)
    # 404/500 -> collapsed error row, not a blank panel.
    assert "maturity-error-row" in src
    # empty stories list -> muted empty message.
    assert "no notification data for this plan" in src
    # dataset_found false -> muted line, not an error.
    assert "failure-mode dataset not found" in src
    assert "dataset_found" in src
    # null cost_per_merged_story renders "-".
    assert 'costPerMerged === null' in src or "cost_per_merged_story" in src


def test_maturity_module_sorts_stories_by_rework_descending():
    src = _read(MATURITY_JS)
    assert "rework_cycles" in src
    assert re.search(r"sort\(", src), "stories must be sorted client-side"


def test_maturity_module_renders_recurrence_alerts_with_warning_style():
    src = _read(MATURITY_JS)
    assert "recurrence_alerts" in src
    assert "maturity-alert" in src, "alerts need a visually distinct class"


def test_maturity_module_uses_green_summary_when_no_missing_or_uncollected():
    src = _read(MATURITY_JS)
    assert "maturity-summary-ok" in src
    assert "maturity-summary-warn" in src


# ---------- registration in shared artifacts ----------

def test_main_js_references_the_maturity_module():
    src = _read(MAIN_JS)
    assert "render/maturity.js" in src, (
        "main.js must reference the new maturity module"
    )


def test_api_js_exposes_maturity_fetch_helpers():
    src = _read(API_JS)
    assert "fetchPlanMetrics" in src
    assert "fetchGuardLiveness" in src
    assert "/api/guard-liveness" in src
    assert "/metrics" in src


def test_plan_detail_js_mounts_the_maturity_panel():
    src = _read(PLAN_DETAIL_JS)
    assert "maturity-panel" in src
    assert "renderMaturityPanel" in src


# ---------- cumulative-artifact guard: no full-file equality ----------

def test_shared_files_are_not_replaced_by_the_maturity_story():
    """Negative guard: the shared artifacts must keep their other exports —
    the maturity story adds to them, it does not replace them."""
    main_src = _read(MAIN_JS)
    assert "renderDecisions" in main_src
    assert "renderNotifications" in main_src
    api_src = _read(API_JS)
    assert "fetchJson" in api_src and "postJson" in api_src