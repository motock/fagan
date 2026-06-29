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


@pytest.fixture
def usage_state_path(tmp_path, monkeypatch):
    path = tmp_path / "usage_state.json"
    monkeypatch.setattr(d, "USAGE_STATE_PATH", path)
    return path


def test_usage_endpoint_when_no_state_file(client, usage_state_path):
    res = client.get("/api/usage")
    assert res.status_code == 200
    assert res.json()["available"] is False


def test_usage_endpoint_surfaces_gate_health(client, usage_state_path):
    usage_state_path.write_text(json.dumps({
        "session_pct": 91, "week_pct": 82, "paused": True,
        "measured_at": "2026-06-26T16:40:13+00:00",
        "gate_blind": False, "consecutive_parse_failures": 0,
    }))
    body = client.get("/api/usage").json()
    assert body["available"] is True
    assert body["session_pct"] == 91
    assert body["paused"] is True
    assert body["gate_blind"] is False


def test_usage_endpoint_reports_blind_gate(client, usage_state_path):
    usage_state_path.write_text(json.dumps({
        "session_pct": 50, "week_pct": 50, "paused": False,
        "measured_at": "2026-06-26T10:00:00+00:00",
        "gate_blind": True, "blind_since": "2026-06-26T10:30:00+00:00",
        "consecutive_parse_failures": 42,
    }))
    body = client.get("/api/usage").json()
    assert body["gate_blind"] is True
    assert body["blind_since"] == "2026-06-26T10:30:00+00:00"
    assert body["consecutive_parse_failures"] == 42


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


# ---------- /api/dispatch_health: escalated-vs-completed rate by acceptance --

def test_dispatch_health_empty_when_no_manifests(client, plan_dir):
    """No plans ingested -> totals all zero, rates 0.0 (not NaN)."""
    res = client.get("/api/dispatch_health")
    assert res.status_code == 200
    body = res.json()
    assert body["totals"] == {
        "dispatched": 0, "done": 0, "escalated": 0, "stories": 0,
        "escalation_rate": 0.0, "success_rate": 0.0,
    }
    assert body["per_plan"] == {}


def test_dispatch_health_stratifies_by_acceptance_presence(client, plan_dir):
    """The headline of Fix #1's measurement: stories with `acceptance` vs
    those without, each with their own escalation/success rates."""
    _write_manifest(plan_dir, "p1", {
        # 2 dispatched, 1 escalated, 1 done — all WITHOUT acceptance
        "S1": {"summary": "a", "status": "done", "backend": "local",
               "acceptance": []},
        "S2": {"summary": "b", "status": "interrupted", "backend": "local",
               "escalated": True, "acceptance": []},
        # 2 dispatched, both done — WITH acceptance (the Fix #1 path)
        "S3": {"summary": "c", "status": "done", "backend": "local",
               "acceptance": [{"path": "t.py", "source": "x"}]},
        "S4": {"summary": "d", "status": "done", "backend": "local",
               "acceptance": [{"path": "t.py", "source": "x"}]},
    })
    # A plan with one still-todo story should NOT inflate the dispatched
    # count — denominator is dispatched, not story_count.
    _write_manifest(plan_dir, "p2", {
        "S1": {"summary": "queued", "status": "todo", "acceptance": []},
    })

    body = client.get("/api/dispatch_health").json()

    # Headline: rates computed off dispatched (not story count).
    assert body["totals"]["stories"] == 5   # 4 in p1 + 1 in p2
    assert body["totals"]["dispatched"] == 4
    assert body["totals"]["done"] == 3
    assert body["totals"]["escalated"] == 1
    assert body["totals"]["escalation_rate"] == 0.25
    assert body["totals"]["success_rate"] == 0.75

    p1 = body["per_plan"]["p1"]
    assert p1["without_acceptance"] == {
        "stories": 2, "dispatched": 2, "done": 1, "escalated": 1,
        "escalation_rate": 0.5, "success_rate": 0.5,
    }
    assert p1["with_acceptance"] == {
        "stories": 2, "dispatched": 2, "done": 2, "escalated": 0,
        "escalation_rate": 0.0, "success_rate": 1.0,
    }
    # p2's still-todo story stays out of the dispatched count.
    assert body["per_plan"]["p2"]["without_acceptance"]["dispatched"] == 0
    assert body["per_plan"]["p2"]["without_acceptance"]["escalation_rate"] == 0.0


def test_dispatch_health_counts_backend_set_as_dispatched_even_if_todo(
    client, plan_dir,
):
    """Stories whose backend field is set but status is still 'todo' (e.g.
    escalated via the local-first auto mode but not yet re-tried) should
    still count as dispatched, otherwise escalation events vanish from the
    rate until the next tick runs."""
    _write_manifest(plan_dir, "p", {
        "S1": {"summary": "x", "status": "todo", "backend": "claude",
               "escalated": True, "acceptance": []},
    })

    body = client.get("/api/dispatch_health").json()
    s = body["per_plan"]["p"]["without_acceptance"]
    assert s["dispatched"] == 1
    assert s["escalated"] == 1
    assert s["escalation_rate"] == 1.0


# ---------- last_activity derivation on /api/plans/{name} stories ----------

def _write_journal(plan_dir, plan_name, story_key, entries):
    """Write a checkpoint journal file. `entries` is a list of dicts each
    with a 'ts' (ISO timestamp) and a 'step' short id, in chronological
    order.  Only the final entry's timestamp matters for last_activity."""
    path = plan_dir / f"{plan_name}.{story_key}.journal.json"
    path.write_text(json.dumps(entries))


def test_story_last_activity_uses_interrupted_at_when_only_that_is_set(client, plan_dir):
    """(1) interrupted_at only -> last_activity == interrupted_at."""
    interrupted_at = "2026-06-25T12:00:00+00:00"
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "interrupted story",
            "status": "interrupted",
            "interrupted_at": interrupted_at,
            "dependencies": [],
        },
    })
    body = client.get("/api/plans/demo").json()
    assert body["stories"]["S1"]["interrupted_at"] == interrupted_at  # preserved
    assert body["stories"]["S1"]["last_activity"] == interrupted_at


def test_story_last_activity_picks_later_of_interrupted_at_and_last_commit(client, plan_dir):
    """(2) both present -> last_activity == the later of the two."""
    earlier = "2026-06-25T10:00:00+00:00"
    later = "2026-06-25T12:00:00+00:00"
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "interrupted with commit history",
            "status": "interrupted",
            "interrupted_at": earlier,
            "last_commit": later,
            "dependencies": [],
        },
        # Reverse case: last_commit is *earlier* than interrupted_at.
        "S2": {
            "summary": "commit older than interrupt",
            "status": "interrupted",
            "interrupted_at": later,
            "last_commit": earlier,
            "dependencies": [],
        },
    })
    body = client.get("/api/plans/demo").json()
    assert body["stories"]["S1"]["last_activity"] == later
    assert body["stories"]["S2"]["last_activity"] == later


def test_story_last_activity_omitted_when_no_signal(client, plan_dir):
    """(3) neither interrupted_at nor last_commit (and no journal) ->
    last_activity is None / absent so the UI doesn't render an age label."""
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "todo story",
            "status": "todo",
            "dependencies": [],
        },
    })
    body = client.get("/api/plans/demo").json()
    story = body["stories"]["S1"]
    # Must exist but signal the absence clearly; UI gates on truthiness.
    assert story.get("last_activity") in (None, "")


def test_story_last_activity_uses_journal_final_timestamp_when_latest(client, plan_dir):
    """(4) journal present -> its final entry's timestamp is considered and
    wins when it's the latest signal available."""
    interrupted_at = "2026-06-25T10:00:00+00:00"
    last_commit = "2026-06-25T11:00:00+00:00"
    journal_final_ts = "2026-06-25T13:30:00+00:00"
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "journal beats both",
            "status": "interrupted",
            "interrupted_at": interrupted_at,
            "last_commit": last_commit,
            "dependencies": [],
        },
    })
    _write_journal(plan_dir, "demo", "S1", [
        {"step": "a", "ts": "2026-06-25T09:00:00+00:00", "summary": "early"},
        {"step": "b", "ts": "2026-06-25T13:30:00+00:00", "summary": "final"},
    ])

    body = client.get("/api/plans/demo").json()
    assert body["stories"]["S1"]["last_activity"] == journal_final_ts


def test_story_last_activity_does_not_mutate_manifest(client, plan_dir):
    """The dashboard is read-only and must NOT modify the manifest on disk
    when deriving last_activity — re-read after the request and assert
    nothing new was written."""
    interrupted_at = "2026-06-25T12:00:00+00:00"
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "no mutating writes please",
            "status": "interrupted",
            "interrupted_at": interrupted_at,
            "dependencies": [],
        },
    })
    before = json.loads((plan_dir / "demo.manifest.json").read_text())
    client.get("/api/plans/demo")
    after = json.loads((plan_dir / "demo.manifest.json").read_text())
    assert before == after


def test_story_last_activity_ignores_missing_or_empty_journal(client, plan_dir):
    """Boundary: a journal file that exists but has no parseable entries
    must not produce a last_activity; missing journal is the same."""
    interrupted_at = "2026-06-25T12:00:00+00:00"
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "broken journal",
            "status": "interrupted",
            "interrupted_at": interrupted_at,
            "dependencies": [],
        },
    })
    # Empty list -> parser should fall back to manifest signals.
    (plan_dir / "demo.S1.journal.json").write_text("[]")
    body = client.get("/api/plans/demo").json()
    assert body["stories"]["S1"]["last_activity"] == interrupted_at
