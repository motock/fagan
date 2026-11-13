"""Store-seam tests for GET /api/plans/{plan_name}/metrics.

Story: remove the last direct on-disk-layout composition from
app/dashboard.py. ``get_plan_metrics`` must stop building the notifications
sidecar path itself (``PLAN_DIR / f"{plan_name}.notifications.jsonl"``) and
obtain the notification history from the same ``_service`` seam every other
dashboard route uses.

The seam contract pinned here: ``PipelineService`` (pipeline/service.py)
exposes ONE narrow accessor

    get_notification_history(plan_name: str) -> tuple[list[dict], int]

returning the plan's notification records and the malformed-line count, read
from the record store, and the dashboard endpoint calls it and feeds the
records to ``story_metrics.compute_story_metrics`` unchanged.

The canonical PLAN_DIR binding is ``pipeline.server.PLAN_DIR``: both
``app.dashboard`` and ``pipeline.store`` read the name through a LiveRef
that re-resolves ``pipeline.server.<name>`` on every access, so patching
``pipeline.server.PLAN_DIR`` must be followed by the endpoint no matter
which module it (indirectly) reads the binding from. A frozen module-load
copy (e.g. ``from pipeline.server import PLAN_DIR`` inside a new module)
would keep reading the real ``~/.claude/plans`` and fail the
canonical-binding test below.

The endpoint's JSON response shape must stay byte-identical: exactly the
keys ``plan``, ``stories``, ``rollup``, ``malformed_lines`` with the same
values the local composition produced.
"""
import inspect
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import dashboard
from pipeline import server as pipeline_server
from pipeline import story_metrics
from pipeline.service import PipelineService


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point the canonical PLAN_DIR binding (pipeline.server) at tmp_path.

    Only ``pipeline.server.PLAN_DIR`` is patched: that IS the canonical
    binding both the dashboard and the store resolve through LiveRef on
    every access.
    """
    monkeypatch.setattr(pipeline_server, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(dashboard.app)


def _write_manifest(plan_dir, name):
    (plan_dir / f"{name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": []})
    )


def _write_sidecar(plan_dir, name, lines):
    (plan_dir / f"{name}.notifications.jsonl").write_text(
        "".join(line + "\n" for line in lines)
    )


VALID_SIDECAR_LINES = [
    json.dumps(
        {"ts": "2026-01-01T00:00:00Z", "story_key": "S1",
         "event": "dispatch_failed", "dedup_key": "d1"}
    ),
    json.dumps(
        {"ts": "2026-01-01T00:00:01Z", "story_key": "S1",
         "event": "tests_failed", "dedup_key": "d2"}
    ),
    json.dumps(
        {"ts": "2026-01-01T00:00:02Z", "story_key": "S2",
         "event": "story_merged", "dedup_key": "d3"}
    ),
    json.dumps(
        {"ts": "2026-01-01T00:00:03Z", "story_key": "S2",
         "event": "escalated", "dedup_key": "d4"}
    ),
]


# ---------------------------------------------------------------------------
# The seam itself: PipelineService.get_notification_history
# ---------------------------------------------------------------------------

def test_service_accessor_returns_the_store_history(plan_dir):
    """The accessor returns (records, malformed) for the plan from the store.

    plan_dir patches ONLY pipeline.server.PLAN_DIR, so this passes only if
    the store the accessor delegates to resolves the canonical (LiveRef)
    binding at call time.
    """
    _write_manifest(plan_dir, "alpha")
    _write_sidecar(plan_dir, "alpha", VALID_SIDECAR_LINES)

    records, malformed = dashboard._service.get_notification_history("alpha")

    assert [record["story_key"] for record in records] == ["S1", "S1", "S2", "S2"]
    assert malformed == 0


def test_service_accessor_is_on_pipeline_service_class():
    """The narrow accessor lives on PipelineService itself, not an ad-hoc
    instance attribute, so every service consumer (and the dashboard's
    module-level ``_service``) sees it."""
    assert callable(PipelineService.get_notification_history)


# ---------------------------------------------------------------------------
# Endpoint behavior over the seam (response shape byte-identical)
# ---------------------------------------------------------------------------

def test_metrics_follows_canonical_plan_dir_binding(plan_dir, client):
    """With pipeline.server.PLAN_DIR patched to a tmp_path holding a valid
    <plan>.notifications.jsonl, the metrics are computed from THAT file."""
    _write_manifest(plan_dir, "alpha")
    _write_sidecar(plan_dir, "alpha", VALID_SIDECAR_LINES)

    response = client.get("/api/plans/alpha/metrics")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["plan"] == "alpha"
    assert len(body["stories"]) == 2
    by_key = {story["story_key"]: story for story in body["stories"]}
    assert by_key["S1"]["dispatch_failures"] == 1
    assert by_key["S1"]["rework_cycles"] == 1
    assert by_key["S1"]["escalations"] == 0
    assert by_key["S1"]["merged"] is False
    assert by_key["S1"]["cost"] == 3
    assert by_key["S2"]["merged"] is True
    assert by_key["S2"]["merged_ts"] == "2026-01-01T00:00:02Z"
    assert by_key["S2"]["escalations"] == 1
    assert by_key["S2"]["cost"] == 2

    rollup = body["rollup"]
    assert rollup["stories_total"] == 2
    assert rollup["stories_merged"] == 1
    assert rollup["total_rework_cycles"] == 1
    assert rollup["total_escalations"] == 1
    assert rollup["total_dispatch_failures"] == 1


def test_metrics_response_shape_exact_keys(plan_dir, client):
    """The JSON response keeps exactly the keys plan/stories/rollup/
    malformed_lines (byte-identical shape to the pre-refactor endpoint)."""
    _write_manifest(plan_dir, "alpha")
    _write_sidecar(plan_dir, "alpha", VALID_SIDECAR_LINES)

    body = client.get("/api/plans/alpha/metrics").json()
    assert sorted(body.keys()) == ["malformed_lines", "plan", "rollup", "stories"]
    assert body["plan"] == "alpha"
    assert isinstance(body["stories"], list)
    assert isinstance(body["rollup"], dict)
    assert isinstance(body["malformed_lines"], int)


def test_metrics_missing_manifest_returns_404_with_detail(plan_dir, client):
    """NEGATIVE: a plan with no manifest still 404s with the existing
    detail message."""
    response = client.get("/api/plans/ghost/metrics")
    assert response.status_code == 404
    assert response.json()["detail"] == "No manifest for plan 'ghost'"


def test_metrics_single_malformed_line_counted_not_raised(plan_dir, client):
    """NEGATIVE: one malformed JSON line is counted in malformed_lines
    rather than raising; the valid lines still produce metrics."""
    _write_manifest(plan_dir, "alpha")
    _write_sidecar(plan_dir, "alpha", [
        json.dumps(
            {"ts": "t1", "story_key": "S1",
             "event": "dispatch_failed", "dedup_key": "d1"}
        ),
        "{not valid json",
    ])

    response = client.get("/api/plans/alpha/metrics")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["malformed_lines"] == 1
    assert len(body["stories"]) == 1
    assert body["stories"][0]["story_key"] == "S1"
    assert body["stories"][0]["dispatch_failures"] == 1


# ---------------------------------------------------------------------------
# Boundary cases: empty / missing sidecar, blank + non-dict lines
# ---------------------------------------------------------------------------

def test_metrics_empty_sidecar_file_yields_zeroed_metrics(plan_dir, client):
    """Boundary: a zero-byte sidecar is not an error — zero stories, zero
    malformed lines."""
    _write_manifest(plan_dir, "alpha")
    _write_sidecar(plan_dir, "alpha", [])

    response = client.get("/api/plans/alpha/metrics")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stories"] == []
    assert body["malformed_lines"] == 0
    assert body["rollup"]["stories_total"] == 0
    assert body["rollup"]["stories_merged"] == 0


def test_metrics_missing_sidecar_file_yields_zeroed_metrics(plan_dir, client):
    """Boundary: manifest present but no sidecar file at all — zero
    metrics, never a 500."""
    _write_manifest(plan_dir, "alpha")

    response = client.get("/api/plans/alpha/metrics")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stories"] == []
    assert body["malformed_lines"] == 0
    assert body["rollup"]["stories_total"] == 0


def test_metrics_blank_and_nondict_lines_counted_as_malformed(plan_dir, client):
    """Boundary: blank lines are skipped; a line parsing to non-dict JSON
    counts as malformed exactly like unparseable text does."""
    _write_manifest(plan_dir, "alpha")
    _write_sidecar(plan_dir, "alpha", [
        "",
        json.dumps(
            {"ts": "t1", "story_key": "S1",
             "event": "dispatch_failed", "dedup_key": "d1"}
        ),
        "[]",
        "not json at all",
    ])

    response = client.get("/api/plans/alpha/metrics")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["malformed_lines"] == 2
    assert len(body["stories"]) == 1
    assert body["stories"][0]["story_key"] == "S1"


# ---------------------------------------------------------------------------
# The dashboard must delegate to the seam, not compose the path locally
# ---------------------------------------------------------------------------

def test_metrics_endpoint_delegates_to_service_accessor(plan_dir, client,
                                                        monkeypatch):
    """The endpoint obtains the history by calling
    ``_service.get_notification_history`` — proven by spying on the
    service instance the routes actually use."""
    _write_manifest(plan_dir, "alpha")
    _write_sidecar(plan_dir, "alpha", VALID_SIDECAR_LINES)

    calls = []
    original = dashboard._service.get_notification_history

    def spy(plan_name, *args, **kwargs):
        calls.append(plan_name)
        return original(plan_name)

    monkeypatch.setattr(dashboard._service, "get_notification_history", spy)

    response = client.get("/api/plans/alpha/metrics")
    assert response.status_code == 200, response.text
    assert calls == ["alpha"]


def test_metrics_endpoint_uses_accessor_return_value(plan_dir, client,
                                                     monkeypatch):
    """The endpoint feeds the ACCESSOR'S return value to the metrics
    computation: redirecting the accessor to a different history must change
    the computed metrics accordingly."""
    _write_manifest(plan_dir, "alpha")
    _write_sidecar(plan_dir, "alpha", VALID_SIDECAR_LINES)  # stories S1, S2

    def spy(plan_name, *args, **kwargs):
        return (
            [{"ts": "t9", "story_key": "RX",
              "event": "dispatch_failed", "dedup_key": "z"}],
            0,
        )

    monkeypatch.setattr(dashboard._service, "get_notification_history", spy)

    response = client.get("/api/plans/alpha/metrics")
    assert response.status_code == 200, response.text
    by_key = {story["story_key"]: story for story in response.json()["stories"]}
    assert "RX" in by_key
    assert "S1" not in by_key
    assert "S2" not in by_key


# ---------------------------------------------------------------------------
# Mechanical regression guards on the dashboard source
# ---------------------------------------------------------------------------

def test_dashboard_source_has_no_local_sidecar_composition():
    """REGRESSION: no ``PLAN_DIR / f"`` composition anywhere in
    app/dashboard.py (equivalent to
    ``grep -c 'PLAN_DIR / f"' app/dashboard.py`` returning 0)."""
    assert 'PLAN_DIR / f"' not in inspect.getsource(dashboard)


def test_dashboard_get_plan_metrics_source_calls_accessor():
    """The metrics route itself references the seam accessor."""
    assert "get_notification_history" in inspect.getsource(
        dashboard.get_plan_metrics
    )


def test_plan_dir_global_removed_with_ui_state_path():
    """The PLAN_DIR module global is gone: its only use was the UI state file,
    which now goes through the host state (c3-3)."""
    assert not hasattr(dashboard, "PLAN_DIR")
    assert not re.search(r"^PLAN_DIR\s*=", inspect.getsource(dashboard), re.MULTILINE)


def test_story_metrics_public_surface_unchanged():
    """DO-NOT guard: story_metrics is untouched — the three functions the
    endpoint relies on still exist and are callable."""
    assert callable(story_metrics.load_notification_records)
    assert callable(story_metrics.compute_story_metrics)
    assert callable(story_metrics.compute_plan_rollup)