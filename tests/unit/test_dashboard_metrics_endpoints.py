"""Tests for two new read-only dashboard endpoints, neither of which exists
on this branch yet - this file is intentionally RED until a later dispatch
implements them in app/dashboard.py (and app/dashboard_helpers.py, if the
existing split puts helper logic there).

  * GET /api/plans/{plan_name}/metrics - loads <plan_name>.notifications.jsonl
    and returns pipeline.story_metrics.load_notification_records +
    compute_story_metrics + compute_plan_rollup as
    {"plan": ..., "stories": [...], "rollup": {...}, "malformed_lines": int}.
  * GET /api/guard-liveness - runs pipeline.guard_liveness.check_guard_liveness
    over docs/failure_modes.json with collected_test_files=None (existence-only,
    no subprocess) and returns the report dict plus {"dataset_found": bool}.

Implementation contract this file pins (so the exact names below are load-
bearing, not incidental):

  * The guard-liveness endpoint must read its dataset path from a
    module-level ``app.dashboard.FAILURE_MODES_DATASET_PATH`` Path constant,
    read fresh at request time (never cached) - mirroring the existing
    ``STATIC_DIR = Path(__file__).parent.parent / "static"`` precedent in
    app/dashboard.py - so tests can monkeypatch it to a synthetic fixture
    instead of the live docs/failure_modes.json (the stub-the-config-source
    rule: never assert the live dataset's real numbers).
  * The metrics endpoint must resolve <plan_name>.notifications.jsonl through
    whichever PLAN_DIR-derived seam the notifications sink itself uses
    (pipeline.persistence / the Store seam), not a second, independently
    constructed path. This suite's ``plan_dir`` fixture patches PLAN_DIR on
    app.dashboard, pipeline.server, AND pipeline.persistence to the same tmp
    directory precisely so the fixture file is visible no matter which of
    those seams the implementation reads through.

Both endpoints must return 200 with a degraded/empty shape rather than 500
when their backing file is absent, empty, or malformed - see the individual
tests below for the exact degraded shape.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from pipeline import guard_liveness, story_metrics

# tests/unit/test_dashboard_metrics_endpoints.py -> tests/unit -> tests -> repo root.
REPO_ROOT_FOR_TEST = Path(__file__).resolve().parents[2]
# A file known to exist under tests/unit/ in this repo, used as a stable
# "this guard file exists" fixture value (existence is checked by a
# recursive basename walk of <repo_root>/tests, so the directory doesn't
# matter, only the basename).
_EXISTING_GUARD_FILE = "test_dashboard_api.py"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    from pipeline import persistence as _pers
    from pipeline import server as _srv

    monkeypatch.setattr(d, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(_srv, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(_pers, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def dataset_path(tmp_path, monkeypatch):
    """Point app.dashboard.FAILURE_MODES_DATASET_PATH at a tmp file instead
    of the real docs/failure_modes.json. ``raising=False`` because the
    attribute does not exist yet on this branch - creating it here still
    lets every test past this fixture exercise the endpoint's real 404/
    degraded-state behavior instead of erroring inside fixture setup."""
    path = tmp_path / "failure_modes.json"
    monkeypatch.setattr(d, "FAILURE_MODES_DATASET_PATH", path, raising=False)
    return path


def _write_manifest(plan_dir, name, stories):
    (plan_dir / f"{name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories})
    )


def _write_notification_lines(plan_dir, plan_name, lines):
    """Write raw text lines to <plan_name>.notifications.jsonl verbatim, one
    per line - callers pass already-serialized JSON or intentionally-broken
    text so malformed-line handling can be exercised precisely."""
    path = plan_dir / f"{plan_name}.notifications.jsonl"
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def _record(story_key, event, ts, dedup_key, **extra):
    rec = {
        "ts": ts,
        "message": event or "note",
        "story_key": story_key,
        "severity": "info",
        "event": event,
        "dedup_key": dedup_key,
    }
    rec.update(extra)
    return rec


def _write_dataset(path, data):
    path.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# GET /api/plans/{plan_name}/metrics - happy path
# ---------------------------------------------------------------------------


def test_metrics_endpoint_happy_path_computes_story_and_rollup_metrics(plan_dir, client):
    plan = "cost-demo"
    _write_manifest(
        plan_dir, plan,
        {"S1": {"summary": "a", "status": "done"}, "S2": {"summary": "b", "status": "done"}},
    )
    lines = [
        json.dumps(_record("S1", "dispatched", "t0", "d0")),
        json.dumps(_record("S1", "dispatch_failed", "t1", "d1")),
        json.dumps(_record("S1", "tests_failed", "t2", "d2")),
        json.dumps(_record("S1", "story_merged", "t3", "d3")),
        json.dumps(_record("S2", "escalated", "u0", "e0")),
        json.dumps(_record("S2", "story_merged", "u1", "e1")),
    ]
    _write_notification_lines(plan_dir, plan, lines)

    # Independently derive the expected payload from the same pure functions
    # the endpoint must call, reading the same file - this documents the
    # wiring contract without re-deriving story_metrics' own arithmetic
    # (which has its own dedicated unit tests in test_story_metrics.py).
    path = plan_dir / f"{plan}.notifications.jsonl"
    expected_records, expected_malformed = story_metrics.load_notification_records(path)
    expected_stories = list(story_metrics.compute_story_metrics(expected_records).values())
    expected_rollup = story_metrics.compute_plan_rollup(expected_stories)

    res = client.get(f"/api/plans/{plan}/metrics")
    assert res.status_code == 200
    body = res.json()

    assert set(body.keys()) == {"plan", "stories", "rollup", "malformed_lines"}
    assert body["plan"] == plan
    assert body["malformed_lines"] == expected_malformed == 0
    assert body["stories"] == expected_stories
    assert body["rollup"] == expected_rollup

    # Concrete spot-checks documenting the expected per-story/rollup shape.
    s1 = next(s for s in body["stories"] if s["story_key"] == "S1")
    assert s1["dispatch_failures"] == 1
    assert s1["rework_cycles"] == 1
    assert s1["escalations"] == 0
    assert s1["merged"] is True
    assert s1["cost"] == 3

    s2 = next(s for s in body["stories"] if s["story_key"] == "S2")
    assert s2["dispatch_failures"] == 0
    assert s2["escalations"] == 1
    assert s2["merged"] is True
    assert s2["cost"] == 2

    assert body["rollup"]["stories_total"] == 2
    assert body["rollup"]["stories_merged"] == 2
    assert body["rollup"]["total_cost"] == 5
    assert body["rollup"]["cost_per_merged_story"] == 2.5


def test_metrics_endpoint_single_notification_record_boundary(plan_dir, client):
    plan = "solo-plan"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "todo"}})
    _write_notification_lines(
        plan_dir, plan, [json.dumps(_record("S1", "story_merged", "t0", "d0"))]
    )

    res = client.get(f"/api/plans/{plan}/metrics")
    assert res.status_code == 200
    body = res.json()
    assert len(body["stories"]) == 1
    assert body["stories"][0]["story_key"] == "S1"
    assert body["stories"][0]["merged"] is True
    assert body["stories"][0]["cost"] == 1
    assert body["rollup"]["stories_total"] == 1
    assert body["rollup"]["stories_merged"] == 1
    assert body["rollup"]["cost_per_merged_story"] == 1.0


# ---------------------------------------------------------------------------
# GET /api/plans/{plan_name}/metrics - missing/empty sidecar (negative)
# ---------------------------------------------------------------------------


def test_metrics_endpoint_missing_sidecar_returns_zeroed_rollup(plan_dir, client):
    """A plan that has never emitted a notification: 200, not an error."""
    plan = "silent-plan"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "todo"}})
    # Intentionally do NOT create <plan>.notifications.jsonl.

    expected_rollup = story_metrics.compute_plan_rollup([])

    res = client.get(f"/api/plans/{plan}/metrics")
    assert res.status_code == 200, "missing sidecar must not 500"
    body = res.json()
    assert body["plan"] == plan
    assert body["stories"] == []
    assert body["malformed_lines"] == 0
    assert body["rollup"] == expected_rollup
    assert body["rollup"]["stories_merged"] == 0
    assert body["rollup"]["cost_per_merged_story"] is None


def test_metrics_endpoint_empty_sidecar_file_returns_zeroed_rollup(plan_dir, client):
    """Boundary: the file exists but has zero lines (distinct from a wholly
    missing file, both must degrade to the same zeroed shape)."""
    plan = "blank-plan"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "todo"}})
    (plan_dir / f"{plan}.notifications.jsonl").write_text("")

    res = client.get(f"/api/plans/{plan}/metrics")
    assert res.status_code == 200, "empty sidecar must not 500"
    body = res.json()
    assert body["stories"] == []
    assert body["malformed_lines"] == 0
    assert body["rollup"]["stories_total"] == 0
    assert body["rollup"]["cost_per_merged_story"] is None


def test_metrics_endpoint_surfaces_malformed_line_count(plan_dir, client):
    """Malformed lines are skipped for the story/rollup computation but the
    count must still surface as ``malformed_lines`` in the response."""
    plan = "mixed-plan"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "todo"}})
    lines = [
        json.dumps(_record("S1", "dispatch_failed", "t0", "d0")),
        "{not valid json at all",
        json.dumps(_record("S1", "story_merged", "t1", "d1")),
        "[1, 2, 3]",  # valid JSON but not an object -> also malformed
    ]
    _write_notification_lines(plan_dir, plan, lines)

    res = client.get(f"/api/plans/{plan}/metrics")
    assert res.status_code == 200
    body = res.json()
    assert body["malformed_lines"] == 2
    assert len(body["stories"]) == 1
    assert body["stories"][0]["story_key"] == "S1"
    assert body["stories"][0]["dispatch_failures"] == 1
    assert body["stories"][0]["merged"] is True


# ---------------------------------------------------------------------------
# GET /api/plans/{plan_name}/metrics - unknown plan (negative)
# ---------------------------------------------------------------------------


def test_metrics_endpoint_consolidates_split_groups_for_same_story_key(plan_dir, client):
    """A story's correlation_id is minted on first dispatch and can be absent
    from earlier notification records; pipeline.story_metrics.compute_story_metrics
    groups per-record (correlation_id when present, else story_key), so the
    same story can surface as two raw groups - one keyed by correlation_id
    (carrying later events, e.g. story_merged) and one keyed by the bare
    story_key (carrying earlier events). The maturity table's grain is one
    row per story, so the endpoint must consolidate these into a single row
    instead of showing the same story twice with conflicting merged flags."""
    plan = "split-story-plan"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "done"}})
    lines = [
        json.dumps(_record("S1", "dispatch_failed", "t0", "d0")),
        json.dumps(_record("S1", "story_merged", "t1", "d1", correlation_id="c-1")),
    ]
    _write_notification_lines(plan_dir, plan, lines)

    res = client.get(f"/api/plans/{plan}/metrics")
    assert res.status_code == 200
    body = res.json()

    assert len(body["stories"]) == 1, "same story_key must collapse to one row"
    story = body["stories"][0]
    assert story["story_key"] == "S1"
    assert story["merged"] is True
    assert story["dispatch_failures"] == 1
    assert story["cost"] == 2  # 1 baseline + 1 dispatch failure, not double-counted
    assert body["rollup"]["stories_total"] == 1
    assert body["rollup"]["stories_merged"] == 1
    assert body["rollup"]["cost_per_merged_story"] == 2.0


def test_metrics_endpoint_consolidation_sums_counters_across_split_groups(plan_dir, client):
    plan = "split-counters-plan"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "in_progress"}})
    lines = [
        json.dumps(_record("S1", "dispatch_failed", "t0", "d0")),
        json.dumps(_record("S1", "tests_failed", "t1", "d1", correlation_id="c-1")),
        json.dumps(_record("S1", "escalated", "t2", "d2", correlation_id="c-1")),
    ]
    _write_notification_lines(plan_dir, plan, lines)

    res = client.get(f"/api/plans/{plan}/metrics")
    assert res.status_code == 200
    body = res.json()

    assert len(body["stories"]) == 1
    story = body["stories"][0]
    assert story["dispatch_failures"] == 1
    assert story["rework_cycles"] == 1
    assert story["escalations"] == 1
    assert story["merged"] is False
    assert story["cost"] == 4  # 1 + 1 + 1 + 1


def test_metrics_endpoint_drops_uncorrelated_group_from_stories_list(plan_dir, client):
    """A notification with neither story_key nor correlation_id (e.g. a
    plan-level notice like "MCP servers touched, restart needed") is real
    data but is not a story - it must not render as a fake "?" row in the
    per-story table."""
    plan = "plan-level-notice"
    _write_manifest(plan_dir, plan, {"S1": {"summary": "x", "status": "done"}})
    lines = [
        json.dumps(_record(None, "mcp_restart_needed", "t0", "d0")),
        json.dumps(_record("S1", "story_merged", "t1", "d1")),
    ]
    _write_notification_lines(plan_dir, plan, lines)

    res = client.get(f"/api/plans/{plan}/metrics")
    assert res.status_code == 200
    body = res.json()

    assert [s["story_key"] for s in body["stories"]] == ["S1"]
    assert body["rollup"]["stories_total"] == 1


def test_metrics_endpoint_404_for_unknown_plan(plan_dir, client):
    """Consistent with GET /api/plans/{plan_name}'s existing not-found
    behavior: a 404 naming the missing plan, not a 200 with empty data."""
    res = client.get("/api/plans/does-not-exist/metrics")
    assert res.status_code == 404
    body = res.json()
    assert "detail" in body
    assert "does-not-exist" in body["detail"]


# ---------------------------------------------------------------------------
# GET /api/guard-liveness - happy path
# ---------------------------------------------------------------------------


def test_guard_liveness_endpoint_happy_path_matches_pure_check(dataset_path, client):
    dataset = [
        {"mode": "T1", "status": "FIXED", "guard": f"`{_EXISTING_GUARD_FILE}`"},
        {
            "mode": "T2",
            "status": "FIXED (with follow-up)",
            "guard": "`this_guard_file_does_not_exist_zzz.py`",
        },
        {"mode": "T3", "status": "NOT fixed", "guard": "none identified"},
    ]
    _write_dataset(dataset_path, dataset)

    expected = guard_liveness.check_guard_liveness(
        dataset, REPO_ROOT_FOR_TEST, collected_test_files=None
    )

    res = client.get("/api/guard-liveness")
    assert res.status_code == 200
    body = res.json()

    assert body["dataset_found"] is True
    assert body["entries"] == expected["entries"]
    assert body["summary"] == expected["summary"]
    # collected_test_files=None must be used (existence-only): no
    # subprocess-derived recurrence framing leaks into the response.
    assert "recurrence_alerts" not in body
    assert "collection_ok" not in body

    entries_by_mode = {e["mode"]: e for e in body["entries"]}
    assert entries_by_mode["T1"]["missing"] == []
    assert entries_by_mode["T1"]["expected_live"] is True
    assert entries_by_mode["T2"]["missing"] == ["this_guard_file_does_not_exist_zzz.py"]
    assert entries_by_mode["T2"]["expected_live"] is True
    assert entries_by_mode["T3"]["guard_files"] == []
    assert body["summary"]["total"] == 3
    assert body["summary"]["missing_files"] == 1


def test_guard_liveness_endpoint_empty_dataset_array_boundary(dataset_path, client):
    """A present-but-empty dataset array is a legitimate (if unusual) state,
    distinct from a missing/unparseable file: dataset_found stays True."""
    _write_dataset(dataset_path, [])

    res = client.get("/api/guard-liveness")
    assert res.status_code == 200
    body = res.json()
    assert body["dataset_found"] is True
    assert body["entries"] == []
    assert body["summary"]["total"] == 0


# ---------------------------------------------------------------------------
# GET /api/guard-liveness - missing/unparseable dataset (negative)
# ---------------------------------------------------------------------------


def test_guard_liveness_endpoint_dataset_missing_returns_degraded_state(dataset_path, client):
    # dataset_path fixture points FAILURE_MODES_DATASET_PATH at a tmp file
    # that is never created.
    res = client.get("/api/guard-liveness")
    assert res.status_code == 200, "a missing dataset must not 500"
    body = res.json()
    assert body["dataset_found"] is False
    assert body["entries"] == []
    assert body["summary"] == {
        "total": 0,
        "with_guard": 0,
        "no_guard_expected": 0,
        "missing_files": 0,
        "uncollected_files": 0,
    }


def test_guard_liveness_endpoint_dataset_unparseable_returns_degraded_state(dataset_path, client):
    dataset_path.write_text("{ this is not valid json ]")

    res = client.get("/api/guard-liveness")
    assert res.status_code == 200, "unparseable JSON must not 500"
    body = res.json()
    assert body["dataset_found"] is False
    assert body["entries"] == []
    assert body["summary"]["total"] == 0


def test_guard_liveness_endpoint_dataset_wrong_json_type_returns_degraded_state(
    dataset_path, client
):
    """Valid JSON that parses to an object (not an array) is also
    unparseable as a failure-mode dataset."""
    dataset_path.write_text(json.dumps({"not": "a list"}))

    res = client.get("/api/guard-liveness")
    assert res.status_code == 200, "wrong dataset JSON type must not 500"
    body = res.json()
    assert body["dataset_found"] is False
    assert body["entries"] == []


# ---------------------------------------------------------------------------
# GET /api/guard-liveness - no subprocess, no caching
# ---------------------------------------------------------------------------


def test_guard_liveness_endpoint_never_spawns_a_subprocess(dataset_path, client, monkeypatch):
    """collected_test_files must be None (existence-only): the
    collected/uncollected signal is the CLI runner story's job, not the
    dashboard's, so no pytest --collect-only subprocess may ever be spawned
    from this request path."""
    _write_dataset(
        dataset_path, [{"mode": "T1", "status": "FIXED", "guard": f"`{_EXISTING_GUARD_FILE}`"}]
    )

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "GET /api/guard-liveness must never spawn a subprocess"
        )

    monkeypatch.setattr(subprocess, "run", _forbidden)

    res = client.get("/api/guard-liveness")
    assert res.status_code == 200


def test_guard_liveness_endpoint_reflects_dataset_changes_no_caching(dataset_path, client):
    _write_dataset(dataset_path, [{"mode": "T1", "status": "FIXED", "guard": "none identified"}])
    first = client.get("/api/guard-liveness").json()
    assert first["summary"]["total"] == 1

    _write_dataset(
        dataset_path,
        [
            {"mode": "T1", "status": "FIXED", "guard": "none identified"},
            {"mode": "T2", "status": "FIXED", "guard": "none identified"},
        ],
    )
    second = client.get("/api/guard-liveness").json()
    assert second["summary"]["total"] == 2, (
        "endpoint must re-read the dataset per request, not cache the first response"
    )
