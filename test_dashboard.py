"""Tests for the read-only monitoring dashboard's API (dashboard.py).

Exercises the FastAPI endpoints against fixture plan files on disk - the
same manifest/notifications/decisions files pipeline_mcp_server.py writes.
The dashboard never writes to PLAN_DIR itself, so these only assert reads.
"""
import json

import pytest
from fastapi.testclient import TestClient

import dashboard as d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(d.app)


def _write_manifest(plan_dir, name, stories, epics=None, paused=False):
    manifest = {"epics": epics or {}, "stories": stories}
    if paused:
        manifest["paused"] = True
    (plan_dir / f"{name}.manifest.json").write_text(json.dumps(manifest))


def test_health_ok(client, plan_dir):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_list_plans_empty_when_no_manifests(client, plan_dir):
    res = client.get("/api/plans")
    assert res.status_code == 200
    assert res.json() == {"plans": []}


def test_list_plans_ignores_non_manifest_json_files(client, plan_dir):
    # A saved-but-not-ingested plan (plain <name>.json) has no manifest yet
    # and must not show up as if it had lifecycle state.
    (plan_dir / "draft-plan.json").write_text(json.dumps({"epics": []}))
    res = client.get("/api/plans")
    assert res.json() == {"plans": []}


def test_list_plans_returns_summary_with_status_counts(client, plan_dir):
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "one", "status": "done", "dependencies": []},
        "S2": {"summary": "two", "status": "in_progress", "dependencies": []},
        "S3": {"summary": "three", "status": "in_progress", "dependencies": []},
    })

    res = client.get("/api/plans")
    assert res.status_code == 200
    plans = res.json()["plans"]
    assert len(plans) == 1
    assert plans[0]["name"] == "demo"
    assert plans[0]["story_count"] == 3
    assert plans[0]["status_counts"] == {"done": 1, "in_progress": 2}
    assert plans[0]["paused"] is False


def test_list_plans_reports_paused_flag(client, plan_dir):
    _write_manifest(plan_dir, "frozen", {"S1": {"summary": "x", "status": "todo"}}, paused=True)
    res = client.get("/api/plans")
    assert res.json()["plans"][0]["paused"] is True


def test_get_plan_404_when_no_manifest(client, plan_dir):
    res = client.get("/api/plans/does-not-exist")
    assert res.status_code == 404


def test_get_plan_returns_stories_epics_notifications_decisions(client, plan_dir):
    _write_manifest(
        plan_dir, "demo",
        stories={"S1": {"summary": "one", "status": "done", "persona": "software-engineer"}},
        epics={"Epic A": "PIPE-1"},
    )
    (plan_dir / "demo.notifications.log").write_text(
        "2026-06-25T00:00:00+00:00 first note\n2026-06-25T00:01:00+00:00 second note\n"
    )
    decisions = [{
        "story_key": "S1", "question": "use library X or Y?",
        "options": ["X", "Y"], "ruling": "use X", "tier": "routine",
        "risk": "low", "rationale": "simpler", "notify_user": False,
        "decided_by": "overlord", "decided_at": "2026-06-25T00:02:00+00:00",
    }]
    (plan_dir / "demo.decisions.json").write_text(json.dumps(decisions))

    res = client.get("/api/plans/demo")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "demo"
    assert body["epics"] == {"Epic A": "PIPE-1"}
    assert body["stories"]["S1"]["status"] == "done"
    assert body["notifications"] == [
        "2026-06-25T00:00:00+00:00 first note",
        "2026-06-25T00:01:00+00:00 second note",
    ]
    assert body["decisions"] == decisions


def test_get_plan_handles_missing_notifications_and_decisions(client, plan_dir):
    _write_manifest(plan_dir, "bare", {"S1": {"summary": "x", "status": "todo"}})
    res = client.get("/api/plans/bare")
    assert res.status_code == 200
    body = res.json()
    assert body["notifications"] == []
    assert body["decisions"] == []


def test_get_plan_tails_notifications_to_limit(client, plan_dir):
    _write_manifest(plan_dir, "chatty", {"S1": {"summary": "x", "status": "todo"}})
    lines = [f"2026-06-25T00:00:00+00:00 note {i}" for i in range(150)]
    (plan_dir / "chatty.notifications.log").write_text("\n".join(lines) + "\n")

    res = client.get("/api/plans/chatty")
    notifications = res.json()["notifications"]
    assert len(notifications) == 100
    assert notifications[-1] == "2026-06-25T00:00:00+00:00 note 149"
    assert notifications[0] == "2026-06-25T00:00:00+00:00 note 50"
