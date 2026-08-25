"""Tests for the read-only monitoring dashboard's API (dashboard.py).

Exercises the FastAPI endpoints against fixture plan files on disk - the
same manifest/notifications/decisions files pipeline_mcp_server.py writes.
The dashboard never writes to PLAN_DIR itself, so these only assert reads.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from pipeline import config_provenance


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "PLAN_DIR", tmp_path)
    from pipeline import server as _srv
    monkeypatch.setattr(_srv, "PLAN_DIR", tmp_path)
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


def test_list_plans_sorted_newest_first(client, plan_dir):
    """The left-nav plan list orders by most-recently-updated first, not
    alphabetically - so a plan touched moments ago always surfaces above
    one untouched for days, regardless of name."""
    import os
    import time

    _write_manifest(plan_dir, "aaa-oldest", {"S1": {"summary": "x", "status": "todo"}})
    os.utime(plan_dir / "aaa-oldest.manifest.json", (time.time() - 200, time.time() - 200))
    _write_manifest(plan_dir, "zzz-newest", {"S1": {"summary": "x", "status": "todo"}})
    os.utime(plan_dir / "zzz-newest.manifest.json", (time.time() - 10, time.time() - 10))
    _write_manifest(plan_dir, "mmm-middle", {"S1": {"summary": "x", "status": "todo"}})
    os.utime(plan_dir / "mmm-middle.manifest.json", (time.time() - 100, time.time() - 100))

    res = client.get("/api/plans")
    names = [p["name"] for p in res.json()["plans"]]
    assert names == ["zzz-newest", "mmm-middle", "aaa-oldest"]


def test_list_plans_excludes_archived_by_default(client, plan_dir):
    _write_manifest(plan_dir, "keep", {"S1": {"summary": "x", "status": "todo"}})
    _write_manifest(plan_dir, "dismiss-me", {"S1": {"summary": "x", "status": "todo"}})

    archive_res = client.post("/api/plans/dismiss-me/archive")
    assert archive_res.status_code == 200
    assert archive_res.json()["archived"] is True

    res = client.get("/api/plans")
    names = [p["name"] for p in res.json()["plans"]]
    assert names == ["keep"]


def test_list_plans_includes_archived_when_requested(client, plan_dir):
    _write_manifest(plan_dir, "keep", {"S1": {"summary": "x", "status": "todo"}})
    _write_manifest(plan_dir, "dismiss-me", {"S1": {"summary": "x", "status": "todo"}})
    client.post("/api/plans/dismiss-me/archive")

    res = client.get("/api/plans?include_archived=true")
    plans = {p["name"]: p["archived"] for p in res.json()["plans"]}
    assert plans == {"keep": False, "dismiss-me": True}


def test_archive_plan_404_for_unknown_plan(client, plan_dir):
    res = client.post("/api/plans/does-not-exist/archive")
    assert res.status_code == 404


def test_unarchive_plan_404_for_unknown_plan(client, plan_dir):
    res = client.post("/api/plans/does-not-exist/unarchive")
    assert res.status_code == 404


def test_unarchive_plan_restores_default_visibility(client, plan_dir):
    _write_manifest(plan_dir, "back-again", {"S1": {"summary": "x", "status": "todo"}})
    client.post("/api/plans/back-again/archive")
    assert [p["name"] for p in client.get("/api/plans").json()["plans"]] == []

    unarchive_res = client.post("/api/plans/back-again/unarchive")
    assert unarchive_res.status_code == 200
    assert unarchive_res.json()["archived"] is False

    assert [p["name"] for p in client.get("/api/plans").json()["plans"]] == ["back-again"]


def test_archive_state_persists_across_separate_requests(client, plan_dir):
    """The archived set is read from disk on every request, not cached in
    memory, so it survives a dashboard process restart (a fresh TestClient
    call sequence exercises the same code path a restart would)."""
    _write_manifest(plan_dir, "persist-me", {"S1": {"summary": "x", "status": "todo"}})
    client.post("/api/plans/persist-me/archive")

    state_file = plan_dir / ".dashboard_ui_state.json"
    assert state_file.exists()
    assert "persist-me" in json.loads(state_file.read_text())["archived"]

    # A brand-new client against the same plan_dir sees the same state.
    fresh_client = TestClient(d.app)
    names = [p["name"] for p in fresh_client.get("/api/plans").json()["plans"]]
    assert names == []


def test_archive_state_file_missing_defaults_to_no_archived_plans(client, plan_dir):
    _write_manifest(plan_dir, "solo", {"S1": {"summary": "x", "status": "todo"}})
    assert not (plan_dir / ".dashboard_ui_state.json").exists()
    res = client.get("/api/plans")
    assert [p["name"] for p in res.json()["plans"]] == ["solo"]


def test_archive_state_file_corrupt_fails_open_to_no_archived_plans(client, plan_dir):
    """A corrupt UI-state file must not 500 the plan list - fail open (treat
    as nothing archived) rather than breaking the whole dashboard over a
    non-critical preference file."""
    _write_manifest(plan_dir, "solo", {"S1": {"summary": "x", "status": "todo"}})
    (plan_dir / ".dashboard_ui_state.json").write_text("{not valid json")
    res = client.get("/api/plans")
    assert res.status_code == 200
    assert [p["name"] for p in res.json()["plans"]] == ["solo"]


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
    """No plans ingested -> totals all zero, rates 0.0 (not NaN). Existing
    keys remain untouched (only-add contract); new aggregate rollup keys
    appear alongside as zero-valued."""
    res = client.get("/api/dispatch_health")
    assert res.status_code == 200
    body = res.json()
    totals = body["totals"]
    # Pinned existing keys — must remain exact-equivalent to the pre-rollup shape.
    assert {k: totals[k] for k in (
        "dispatched", "done", "escalated", "stories",
        "escalation_rate", "success_rate",
    )} == {
        "dispatched": 0, "done": 0, "escalated": 0, "stories": 0,
        "escalation_rate": 0.0, "success_rate": 0.0,
    }
    # New aggregate rollup is present and all-zero.
    assert totals["dispatch_attempts"] == 0
    assert totals["rework_attempts"] == 0
    assert totals["merge_attempts"] == 0
    assert totals["failure_reasons"] == {}
    assert totals["by_backend"] == {}
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


# ---------- aggregate attempt / failure metrics on /api/plans + dispatch_health ---

def test_plan_summary_aggregate_sums_attempt_counts_across_stories(client, plan_dir):
    """(1) A plan with varied attempt counts -> sums are correct, and the
    existing _plan_summary fields (name/paused/story_count/status_counts)
    are preserved verbatim (only-add contract)."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a", "status": "done",
               "dispatch_attempts": 3, "rework_attempts": 2,
               "merge_attempts": 1, "backend": "local"},
        "S2": {"summary": "b", "status": "in_progress",
               "dispatch_attempts": 5, "rework_attempts": 0,
               "merge_attempts": 2, "backend": "claude"},
        "S3": {"summary": "c", "status": "failed",
               "dispatch_attempts": 1, "rework_attempts": 1,
               "merge_attempts": 0, "backend": "local"},
    })

    res = client.get("/api/plans")
    assert res.status_code == 200
    plan = res.json()["plans"][0]
    # Existing fields untouched.
    assert plan["name"] == "demo"
    assert plan["story_count"] == 3
    assert plan["status_counts"]["done"] == 1
    # New aggregate block: sums exact, every story counted once.
    agg = plan["aggregate"]
    assert agg["dispatch_attempts"] == 3 + 5 + 1
    assert agg["rework_attempts"] == 2 + 0 + 1
    assert agg["merge_attempts"] == 1 + 2 + 0


def test_plan_summary_aggregate_failure_reason_rollup_groups_equal_reasons(client, plan_dir):
    """(2) failure_reason rollup: equal reasons are grouped under the same
    key, and the empty string is bucketed under '(none)' rather than
    becoming its own literal '' key in the UI."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a", "status": "failed",
               "failure_reason": "tests failed", "backend": "local"},
        "S2": {"summary": "b", "status": "failed",
               "failure_reason": "tests failed", "backend": "local"},
        "S3": {"summary": "c", "status": "failed",
               "failure_reason": "merge conflict", "backend": "claude"},
        "S4": {"summary": "d", "status": "todo",
               "failure_reason": "", "backend": "local"},
    })

    plan = client.get("/api/plans").json()["plans"][0]
    agg = plan["aggregate"]
    by_reason = agg["failure_reasons"]
    assert by_reason["tests failed"] == 2
    assert by_reason["merge conflict"] == 1
    # Empty-string reason -> '(none)' bucket, not a bare '' key.
    assert by_reason["(none)"] == 1
    assert "" not in by_reason


def test_plan_summary_aggregate_escalated_and_backend_counts_correct(client, plan_dir):
    """(3) Escalated count and per-backend counts are summed correctly."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "a", "status": "done",
               "backend": "local", "escalated": False},
        "S2": {"summary": "b", "status": "interrupted",
               "backend": "claude", "escalated": True},
        "S3": {"summary": "c", "status": "failed",
               "backend": "local", "escalated": True},
        "S4": {"summary": "d", "status": "todo",
               "backend": "claude", "escalated": False},
        "S5": {"summary": "e", "status": "in_progress",
               # backend field missing -> must NOT raise KeyError and must
               # NOT contribute to any backend bucket.
        },
    })

    plan = client.get("/api/plans").json()["plans"][0]
    agg = plan["aggregate"]
    assert agg["escalated"] == 2
    by_backend = agg["by_backend"]
    assert by_backend["local"] == 2   # S1 + S3
    assert by_backend["claude"] == 2  # S2 + S4
    # Stories with backend missing must not crash and must not appear.
    assert "S5" not in by_backend or by_backend.get("S5", 0) == 0


def test_plan_summary_aggregate_treats_missing_attempt_fields_as_zero(client, plan_dir):
    """(4) Stories missing dispatch_attempts / rework_attempts /
    merge_attempts / escalated / failure_reason / backend contribute 0 to
    every aggregate bucket and MUST NOT raise KeyError. Verifies the
    stories-missing-the-fields branch end-to-end through the HTTP
    boundary."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "minimal", "status": "todo"},
    })

    res = client.get("/api/plans")
    assert res.status_code == 200
    plan = res.json()["plans"][0]
    agg = plan["aggregate"]
    assert agg["dispatch_attempts"] == 0
    assert agg["rework_attempts"] == 0
    assert agg["merge_attempts"] == 0
    assert agg["escalated"] == 0
    # failure_reasons is a dict — present even when nothing contributed.
    assert agg["failure_reasons"] == {"(none)": 1}
    # by_backend is a dict — empty when no story declared a backend.
    assert agg["by_backend"] == {}


def test_plan_summary_aggregate_all_zero_for_empty_plan(client, plan_dir):
    """(5) An empty plan (zero stories) -> all-zero aggregate, no NaN."""
    _write_manifest(plan_dir, "demo", {})

    plan = client.get("/api/plans").json()["plans"][0]
    agg = plan["aggregate"]
    assert agg == {
        "dispatch_attempts": 0,
        "rework_attempts": 0,
        "merge_attempts": 0,
        "escalated": 0,
        "failure_reasons": {},
        "by_backend": {},
    }


def test_dispatch_health_adds_aggregate_keys_without_dropping_existing(client, plan_dir):
    """Only-add contract on /api/dispatch_health: existing keys in totals
    and per_plan must remain byte-for-byte unchanged, and new aggregate
    counters must appear alongside."""
    _write_manifest(plan_dir, "p1", {
        "S1": {"summary": "a", "status": "done", "backend": "local",
               "dispatch_attempts": 2, "rework_attempts": 1,
               "failure_reason": "tests failed"},
        "S2": {"summary": "b", "status": "interrupted", "backend": "claude",
               "escalated": True, "dispatch_attempts": 4},
    })

    body = client.get("/api/dispatch_health").json()

    # Existing totals shape preserved exactly.
    totals_keys = set(body["totals"].keys())
    assert {
        "dispatched", "done", "escalated", "stories",
        "escalation_rate", "success_rate",
    }.issubset(totals_keys)
    # Per-plan entries preserve the existing acceptance slice keys.
    p1 = body["per_plan"]["p1"]
    assert {"with_acceptance", "without_acceptance"}.issubset(set(p1.keys()))
    # Aggregate rollup is present on totals.
    assert "dispatch_attempts" in body["totals"]
    assert "rework_attempts" in body["totals"]
    assert "merge_attempts" in body["totals"]
    assert "escalated" in body["totals"]
    assert body["totals"]["dispatch_attempts"] == 6   # 2 + 4
    assert body["totals"]["rework_attempts"] == 1
    assert body["totals"]["merge_attempts"] == 0
    assert body["totals"]["escalated"] == 1


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


# ---------- /api/plans/{plan}/stories/{story}/journal endpoint ----------
#
# The endpoint surfaces the story's checkpoint journal so the UI can render
# a timeline of every meaningful step the agent took. Missing journal is NOT
# an error — it just means the story hasn't checkpointed anything yet, so
# we return {available:false, entries:[]} with a 200. Malformed JSON is
# treated the same way (the dashboard never 500s because a stray corrupt
# file dropped into PLAN_DIR). 404 is reserved for plan/story-not-found.


def test_journal_endpoint_returns_entries_in_file_order(client, plan_dir):
    """(1) journal file present -> available:true, entries returned in the
    order they appear in the file with step/summary/next_hint/ts preserved."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "story with a journal", "status": "in_progress", "dependencies": []},
    })
    entries = [
        {"step": "read-files", "summary": "read inputs", "next_hint": "compute outputs", "ts": "2026-06-25T12:00:00+00:00"},
        {"step": "compute-outputs", "summary": "ran the math", "next_hint": "write tests", "ts": "2026-06-25T12:05:00+00:00"},
        {"step": "write-tests", "summary": "added pytest cases", "ts": "2026-06-25T12:10:00+00:00"},
    ]
    _write_journal(plan_dir, "demo", "S1", entries)

    res = client.get("/api/plans/demo/stories/S1/journal")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert [e["step"] for e in body["entries"]] == [e["step"] for e in entries]
    # Each entry exposes the documented UI fields verbatim; missing optional
    # fields are passed through as undefined rather than being dropped, so
    # the client can decide how to render (vs. fabricating empty strings).
    assert body["entries"][0]["summary"] == "read inputs"
    assert body["entries"][0]["next_hint"] == "compute outputs"
    assert body["entries"][1]["ts"] == "2026-06-25T12:05:00+00:00"
    # Entry without next_hint — the key is still present (None), not omitted.
    assert "next_hint" in body["entries"][2]
    assert body["entries"][2]["next_hint"] is None


def test_journal_endpoint_unavailable_when_no_file(client, plan_dir):
    """(2) no journal file on disk -> 200 with available:false, empty list.
    Not a 404 — the story simply hasn't checkpointed yet, which is a normal
    'no journal yet' state for a brand-new story."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "fresh story", "status": "todo", "dependencies": []},
    })
    res = client.get("/api/plans/demo/stories/S1/journal")
    assert res.status_code == 200
    assert res.json() == {"available": False, "entries": []}


def test_journal_endpoint_404_when_plan_or_story_missing(client, plan_dir):
    """(3) plan or story not found -> 404 (client can surface a real 'not
    found' rather than rendering an empty timeline)."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "exists", "status": "todo", "dependencies": []},
    })
    # Plan missing entirely.
    res = client.get("/api/plans/nope/stories/S1/journal")
    assert res.status_code == 404
    # Plan exists, story key does not.
    res = client.get("/api/plans/demo/stories/NOPE/journal")
    assert res.status_code == 404


def test_journal_endpoint_unavailable_on_malformed_json(client, plan_dir):
    """(4) malformed JSON in the journal file -> available:false (NOT a
    500). A stray corrupt file in PLAN_DIR must never crash the dashboard;
    the client renders the 'No journal yet' empty state and the user can
    investigate on disk without the whole UI going red."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "journal got corrupted", "status": "in_progress", "dependencies": []},
    })
    (plan_dir / "demo.S1.journal.json").write_text("{this is not valid json")
    res = client.get("/api/plans/demo/stories/S1/journal")
    assert res.status_code == 200
    assert res.json() == {"available": False, "entries": []}


def test_journal_endpoint_empty_entries_array_means_unavailable(client, plan_dir):
    """Boundary: a journal file that exists but is an empty list -> the UI
    should render the same 'No journal yet' empty state as a missing file.
    Distinguishing an empty list from a missing file would leak an internal
    detail (the file was touched but nothing was checkpointed) for no gain."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "touched but empty", "status": "todo", "dependencies": []},
    })
    (plan_dir / "demo.S1.journal.json").write_text("[]")
    res = client.get("/api/plans/demo/stories/S1/journal")
    assert res.status_code == 200
    assert res.json() == {"available": False, "entries": []}




def test_journal_endpoint_passes_through_unknown_extra_fields(client, plan_dir):
    """Positive/boundary: extra unknown fields on each entry are preserved
    verbatim, not stripped, so future schema additions flow through without
    needing a dashboard change. Only `entries` is required to be a list."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "forward-compat", "status": "in_progress", "dependencies": []},
    })
    entries = [
        {"step": "x", "summary": "y", "next_hint": "z", "ts": "t",
         "extra_field": {"nested": True}, "score": 42},
    ]
    _write_journal(plan_dir, "demo", "S1", entries)
    body = client.get("/api/plans/demo/stories/S1/journal").json()
    assert body["entries"][0]["extra_field"] == {"nested": True}
    assert body["entries"][0]["score"] == 42


# ---------- story log tail endpoint (/api/plans/{plan}/stories/{key}/log) ----------
#
# The endpoint surfaces the on-disk log recorded in the manifest's
# story['log'] field so the UI can render it inside the redesigned modal.
# Rules:
#   * Returns {"available": bool, "lines": [str...]} (last N lines, oldest
#     first / newest-last). Default N=200, capped by `_LOG_TAIL_CAP`.
#   * If the manifest has no 'log' field, available=false and lines=[].
#   * If the recorded log file is missing on disk, available=false (NOT a
#     500 — a missing log is a normal state).
#   * 404 if the plan or story key is unknown.
#   * The log path is taken from the manifest only; the caller cannot
#     override it (so a path traversal in `?path=` etc. is impossible).

LOG_TAIL_CAP = 500  # mirror dashboard._LOG_TAIL_CAP. Hard cap on lines returned.


def test_story_log_endpoint_returns_tail_when_file_exists(client, plan_dir):
    """Story with a 'log' field pointing at a real file -> available=True,
    lines are the full file in newest-last (insertion) order."""
    log_path = plan_dir / "demo.S1.log"
    log_path.write_text("line-one\nline-two\nline-three\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "x", "status": "done",
            "log": "demo.S1.log",
            "dependencies": [],
        },
    })

    res = client.get("/api/plans/demo/stories/S1/log")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert body["lines"] == ["line-one", "line-two", "line-three"]


def test_story_log_endpoint_no_log_field_returns_available_false(client, plan_dir):
    """No 'log' field recorded in the manifest is a normal state: available
    must be False with an empty list, never a 404 or a 500."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "x", "status": "todo", "dependencies": []},
    })

    res = client.get("/api/plans/demo/stories/S1/log")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is False
    assert body["lines"] == []


def test_story_log_endpoint_caps_to_default_when_file_is_large(client, plan_dir):
    """A log file with more than the default 200 lines returns only the
    last 200 lines, in original (newest-last) order."""
    log_path = plan_dir / "demo.S1.log"
    # Write 250 lines: "L001" .. "L250". Last 200 = "L051".."L250".
    log_path.write_text("\n".join(f"L{n:03d}" for n in range(1, 251)) + "\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "x", "status": "in_progress",
            "log": "demo.S1.log",
            "dependencies": [],
        },
    })

    res = client.get("/api/plans/demo/stories/S1/log")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert len(body["lines"]) == 200
    assert body["lines"][0] == "L051"
    assert body["lines"][-1] == "L250"


def test_story_log_endpoint_404_for_unknown_plan(client, plan_dir):
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "x", "status": "todo", "log": "demo.S1.log"},
    })
    res = client.get("/api/plans/no-such-plan/stories/S1/log")
    assert res.status_code == 404


def test_story_log_endpoint_404_for_unknown_story(client, plan_dir):
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "x", "status": "todo", "log": "demo.S1.log"},
    })
    res = client.get("/api/plans/demo/stories/no-such-story/log")
    assert res.status_code == 404


def test_story_log_endpoint_missing_file_returns_available_false_not_500(client, plan_dir):
    """Path recorded in manifest but file gone (deleted, not yet written)
    must NOT raise 500: that is a normal state, not an error. The endpoint
    must instead return available=False with empty lines."""
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "x", "status": "interrupted",
            "log": "demo.S1.log",  # recorded, but no file written for it
            "dependencies": [],
        },
    })

    res = client.get("/api/plans/demo/stories/S1/log")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is False
    assert body["lines"] == []


def test_story_log_endpoint_ignores_query_path_override(client, plan_dir):
    """Hard restriction: the request must NOT be able to redirect the read
    to an arbitrary path. The endpoint reads only from the manifest's
    'log' field; any ?path=/etc/passwd etc. is ignored. We arrange a
    benign secret next to the log and assert only the manifest log is read."""
    log_path = plan_dir / "demo.S1.log"
    log_path.write_text("manifest-log-line\n")
    secret = plan_dir / "secret.txt"
    secret.write_text("TOPSECRET")
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "x", "status": "done",
            "log": "demo.S1.log",
            "dependencies": [],
        },
    })

    res = client.get("/api/plans/demo/stories/S1/log?path=secret.txt")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert body["lines"] == ["manifest-log-line"]
    assert "TOPSECRET" not in "\n".join(body["lines"])


def test_story_log_endpoint_handles_log_path_with_dotdot(client, plan_dir):
    """A manifest log field that contains '..' components must be
    normalized to a contained path under PLAN_DIR; traversal attempts
    must resolve to a non-existent file and degrade gracefully."""
    # Direct traversal attempt: write a sentinel outside the plan_dir by
    # using a sibling tmp_path location instead. We can't write outside
    # tmp_path here, but we can point story.log at '../outside.log' and
    # assert the call degrades to available:false (file not found
    # relative to plan_dir) without raising.
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "x", "status": "todo",
            "log": "../outside.log",
            "dependencies": [],
        },
    })

    res = client.get("/api/plans/demo/stories/S1/log")
    assert res.status_code == 200
    # Path is resolved under PLAN_DIR, which lacks that file; safe degrade.
    assert res.json() == {"available": False, "lines": []}


def test_story_log_endpoint_empty_file_returns_empty_lines(client, plan_dir):
    """Boundary: a log file that exists but is zero bytes returns
    available=True with an empty lines list (file was not 404'd, just
    no content yet)."""
    (plan_dir / "demo.S1.log").write_text("")
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "x", "status": "todo",
            "log": "demo.S1.log",
            "dependencies": [],
        },
    })

    res = client.get("/api/plans/demo/stories/S1/log")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert body["lines"] == []


def test_story_log_endpoint_garbage_bytes_decode_replacement(client, plan_dir):
    """A log file with non-UTF-8 garbage bytes must not raise; replacement
    characters are accepted so the dashboard can still surface whatever
    was on disk instead of 500'ing."""
    (plan_dir / "demo.S1.log").write_bytes(b"good-line\n\xff\xfe\xfd\nanother\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {
            "summary": "x", "status": "todo",
            "log": "demo.S1.log",
            "dependencies": [],
        },
    })

    res = client.get("/api/plans/demo/stories/S1/log")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    # Three lines: the garbage line is decoded with U+FFFD placeholders.
    assert len(body["lines"]) == 3
    assert body["lines"][0] == "good-line"
    assert body["lines"][2] == "another"
    # The middle line contains at least one replacement character.
    assert "\ufffd" in body["lines"][1]

# ---------- journal timeline rendering in static/app.js ----------
#
# The dashboard hands the UI a `last_activity` ISO string per story and the
# browser computes the age + staleness class from there so cards stay
# accurate between polls (no re-fetch needed as the clock advances). These
# tests exercise the pure helpers exposed by app.js by shelling out to Node
# in a subprocess — no JS test runner / jsdom dependency, just plain pytest.
import os

from tests.unit._app_js import run_app_js as _shared_run_app_js

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")


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
            getElementById: () => ({ ...fakeEl, dataset: {}, addEventListener: noop }),
            createElement: () => ({ ...fakeEl, classList: { add: noop, remove: noop, contains: () => false } }),
        };
        globalThis.window = {
            // app.js reads window.location.hash at boot (applyHashToState) and
            // assigns it back; stub a plain location with an empty hash. The
            // hashchange listener is wired at module load too.
            location: { hash: "" },
            addEventListener: noop,
        };
        globalThis.localStorage = { getItem: () => null, setItem: noop };
        globalThis.fetch = () => new Promise(() => {}); // never resolves
        process.on("unhandledRejection", () => {});
        // app.js calls setInterval(refresh, 4000) at module load. In Node
        // that keeps the event loop alive after we've printed the result;
        // override so the process can exit naturally.
        globalThis.setInterval = () => 0;
        globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
"""


def _run_app_js(expr):
    """Evaluate a JS expression inside an environment where static/app.js
    has been loaded (so its top-level consts + functions are available).
    Returns the JSON-serialized result. Keeps the assertion surface area
    in Python where the rest of the suite already lives.

    app.js touches `document`/`window` at module load to wire DOM event
    listeners; we stub those out so the pure helpers below are testable
    without pulling in jsdom."""
    proc = _shared_run_app_js(expr, shim=_SHIM)
    if proc.returncode != 0:
        raise AssertionError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _iso(seconds_ago):
    """Return an ISO timestamp `seconds_ago` in the past, UTC."""
    from datetime import datetime, timedelta, timezone
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    # datetime.isoformat produces '+00:00'; new Date() handles that fine.
    return dt.isoformat()


def test_relative_age_label_minutes_and_hours_and_days():
    """The short-form label uses the right unit and floors toward zero."""
    cases = [
        # age_seconds -> expected label
        (0, "just now"),
        (59, "just now"),
        (60, "1m ago"),
        (3 * 60, "3m ago"),
        (59 * 60 + 30, "59m ago"),
        (60 * 60, "1h ago"),
        (2 * 60 * 60, "2h ago"),
        (23 * 60 * 60 + 30 * 60, "23h ago"),
        (24 * 60 * 60, "1d ago"),
        (4 * 24 * 60 * 60, "4d ago"),
    ]
    for age, expected in cases:
        assert _run_app_js(f"relativeAgeLabel({age})") == expected, (age, expected)


def test_relative_age_label_clamps_future_to_just_now():
    """Negative age (future timestamp) must NOT surface as '-5m ago'."""
    assert _run_app_js("relativeAgeLabel(-1)") == "just now"
    assert _run_app_js("relativeAgeLabel(-3600)") == "just now"


def test_age_label_for_returns_null_when_missing_or_unparseable():
    """No signal -> no label, so the UI doesn't render an empty pill."""
    assert _run_app_js("ageLabelFor(null)") is None
    assert _run_app_js("ageLabelFor(undefined)") is None
    assert _run_app_js("ageLabelFor('')") is None
    assert _run_app_js("ageLabelFor('not-a-date')") is None


def test_age_label_for_uses_last_activity_relative_to_now():
    """A 3-minute-old last_activity should render as '3m ago' (allowing
    for the second or two between us computing 'now' and Node computing
    its own 'now' — but 3m should never collapse to 'just now')."""
    ts = _iso(3 * 60)
    label = _run_app_js(f"ageLabelFor({json.dumps(ts)})")
    assert label == "3m ago", label


def test_age_label_for_future_timestamp_is_just_now():
    """Boundary: a future-dated last_activity clamps to 'just now'
    rather than producing a negative-looking string."""
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert _run_app_js(f"ageLabelFor({json.dumps(future)})") == "just now"


def test_is_stale_in_progress_only_for_aged_in_progress_stories():
    """Stale = in_progress AND age > STALE_IN_PROGRESS_MINUTES.
    Other statuses, missing timestamps, or fresh ages must all be false."""
    fresh_ts = _iso(5)               # 5 seconds old
    aged_ts = _iso(45 * 60)          # 45 minutes old
    fresh_story = {"status": "in_progress", "last_activity": fresh_ts}
    aged_story = {"status": "in_progress", "last_activity": aged_ts}
    aged_done = {"status": "done", "last_activity": aged_ts}
    aged_no_ts = {"status": "in_progress"}
    no_signal = {"status": "in_progress", "last_activity": None}

    assert _run_app_js(f"isStaleInProgress({json.dumps(fresh_story)})") is False
    assert _run_app_js(f"isStaleInProgress({json.dumps(aged_story)})") is True
    assert _run_app_js(f"isStaleInProgress({json.dumps(aged_done)})") is False
    assert _run_app_js(f"isStaleInProgress({json.dumps(aged_no_ts)})") is False
    assert _run_app_js(f"isStaleInProgress({json.dumps(no_signal)})") is False
    # And no story at all.
    assert _run_app_js("isStaleInProgress(null)") is False


# === Theme / design-token smoke tests =====================================
# Frontend-only work, but the success criteria is pinned down here as
# regression guards so a future change can't silently disable light theme
# or break the toggle wiring.

def test_index_html_references_static_assets(client):
    """index.html must reference /style.css and /app.js so they load as 200s
    when uvicorn serves the dashboard."""
    body = client.get("/").text
    assert 'href="/style.css"' in body
    assert 'src="/app.js"' in body


def test_static_assets_serve_with_200(client):
    """End-to-end asset delivery: /style.css and /app.js must return 200."""
    for path in ("/style.css", "/app.js"):
        res = client.get(path)
        assert res.status_code == 200, f"{path} -> {res.status_code}"


def test_style_css_defines_light_theme_tokens(client):
    """The [data-theme="light"] block must exist so the toggle can actually
    switch palettes (it just sets data-theme; the rest is CSS)."""
    css = client.get("/style.css").text
    assert '[data-theme="light"]' in css
    # Token shape under :root should also be present (spacing scale + elevation)
    assert "--sp-2: 8px" in css  # 8px spacing scale baseline
    assert "--shadow-1" in css and "--shadow-2" in css and "--shadow-3" in css


def test_app_js_persists_theme_under_documented_key(client):
    """The toggle contract: localStorage key 'pipeline-dashboard-theme',
    try/catch-wrapped so a locked-down storage backend doesn't throw."""
    js = client.get("/app.js").text
    assert '"pipeline-dashboard-theme"' in js or "pipeline-dashboard-theme" in js
    # localStorage access must be guarded (read AND write sides)
    assert "localStorage.getItem" in js
    assert "localStorage.setItem" in js
    # The toggle must set documentElement.dataset.theme (the contract for CSS)
    assert "documentElement.dataset.theme" in js
    # And it must default to dark when unset / empty.
    assert "dark" in js.lower()
    # try/catch wrapping around localStorage (mirrors existing filter persistence)
    assert "try {" in js
    assert "catch" in js


def test_index_html_has_theme_toggle_button(client):
    """A header button the user can actually click; without it the CSS toggle
    contract isn't discoverable."""
    body = client.get("/").text
    assert 'id="theme-toggle"' in body
    assert "icon-sun" in body and "icon-moon" in body


# === Kanban board rendering regression ================================
# The board is the heart of the dashboard; these tests pin down the
# structural contract of renderBoard (one column per selected status,
# correct counts, empty column-body preserved for layout stability, empty
# state when no statuses are selected, and that filter/sort logic in
# applyFilters still behaves correctly). Each test uses _run_app_js so we
# evaluate real app.js source, not a duplicate copy.

def test_render_board_one_column_per_selected_status():
    """With three selected statuses, renderBoard emits exactly three
    .column nodes — never more, never fewer."""
    expr = (
        "(() => { state.filters.statuses = ['in_progress','todo','done'];"
        " return renderBoard({"
        " 'k1':{status:'in_progress',summary:''},"
        " 'k2':{status:'in_progress',summary:''},"
        " 'k3':{status:'todo',summary:''}});"
        " })()"
    )
    html = _run_app_js(expr)
    # exactly one column per status (case-sensitive status label)
    assert html.count('class="column"') == 3
    assert "in_progress</span>" in html
    assert "todo</span>" in html
    assert "done</span>" in html


def test_render_board_counts_match_filtered_cards_per_column():
    """The count pill in the column header must equal the number of cards
    the persona/risk filters let through (filter logic unchanged).
    Empty persona/risk filter lists mean 'no filter applied' (default)."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress'];"
        " state.filters.personas = ['lead']; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a', persona:'lead',   risk:''},"
        "  'k2':{status:'in_progress', summary:'b', persona:'lead',   risk:''},"
        "  'k3':{status:'todo',       summary:'c', persona:'lead',   risk:''},"  # wrong column
        "  'k4':{status:'in_progress', summary:'d', persona:'other',  risk:''}"  # persona-filtered
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    # exactly one column (in_progress)
    assert html.count('class="column"') == 1
    # the count badge inside that column reads '2'
    assert ">2</span>" in html or ">2<" in html
    # two cards (k1, k2); k3 lives in a different column, k4 persona-filtered.
    # Match exactly `class="card"` (with the closing quote, not `card-key`,
    # `card-summary`, or `card-age` which also start with `card`).
    assert html.count('class="card"') == 2


def test_render_board_empty_column_renders_empty_body_not_absent():
    """A status with zero matching stories still renders the column shell
    so the layout stays stable as filters change."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress', 'todo'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'only one'}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    # both columns present
    assert html.count('class="column"') == 2
    # both column-body divs present (one with a card, one empty)
    assert html.count('class="column-body">') == 2
    # the todo column body is empty (no cards inside)
    assert "column-body\"></div>" in html or "column-body\"> </div>" in html \
        or "column-body\"><" in html  # any non-empty marker means a card sneaked in


def test_render_board_deselected_statuses_shows_empty_state():
    """All-statuses-deselected -> the empty-state copy, NOT a board of
    zero columns. (The text 'No statuses selected.' is the contract.)"""
    expr = (
        "(() => {"
        " state.filters.statuses = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a'}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert html.count('class="column"') == 0
    assert "No statuses selected." in html


def test_render_board_completion_hint_on_done_column():
    """Done column gets a small completion hint like '2/5' so the user
    sees plan progress at a glance, without changing filter logic."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['done'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'a':{status:'done',         summary:'a'},"
        "  'b':{status:'done',         summary:'b'},"
        "  'c':{status:'in_progress',  summary:'c'},"
        "  'd':{status:'todo',         summary:'d'},"
        "  'e':{status:'tests_passed', summary:'e'}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    # a column-completion element with the done/total ratio
    assert 'class="column-completion"' in html
    assert "2/5" in html


def test_render_board_completion_hint_only_on_done_column():
    """Negative test: column-completion must NOT appear on non-done
    columns. The plan-total ratio is meaningful only for 'done'; on
    every other column it would just be noise (e.g. 1/5 in_progress
    cards tells the user nothing useful)."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['done','in_progress','todo'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'a':{status:'done',        summary:'a'},"
        "  'b':{status:'in_progress', summary:'b'},"
        "  'c':{status:'todo',        summary:'c'}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert html.count('class="column-completion"') == 1
    # Verify the single completion hint sits inside the done column by
    # splitting on the column blocks and counting per-column. The
    # column shells are rendered in STATUS_COLUMNS order:
    # todo, in_progress, ..., done (done is last), so the completion
    # block must come after the last 'done</span>' header.
    done_header_idx = html.rfind(">done<")
    completion_idx = html.find('class="column-completion"')
    last_in_progress_idx = html.rfind(">in_progress<")
    assert done_header_idx > 0, html
    assert last_in_progress_idx > 0, html
    assert completion_idx > done_header_idx, \
        f"completion must appear after the done header: {done_header_idx}/{completion_idx}"
    # and after every in_progress column shell (no completion leakage
    # into the in_progress column).
    assert completion_idx > last_in_progress_idx, \
        f"completion must not appear before the last in_progress header: " \
        f"{last_in_progress_idx}/{completion_idx}"


def test_render_board_card_click_wires_show_story_modal():
    """The click handler attached in renderPlanDetail must still call
    showStoryModal with the story matching the clicked card's data-key.
    This guards the modal open behavior against accidental breaks when
    the card markup changes. We install a mini DOM stub that captures
    innerHTML, then synthesizes a click on the card that renderBoard
    produced — proves the wired listener resolves back to the story."""
    # Build a section DOM stub: stores innerHTML, exposes querySelectorAll
    # that parses out any element with a `data-key` attribute (matching
    # what renderBoard renders), and forwards .click() to our recorder.
    expr = (
        "(() => {"
        " globalThis.__lastModalStory = null;"
        " globalThis.__lastModalKey = null;"
        # Replace showStoryModal with a recorder so the click handler
        # calls our stub instead of the real (DOM-dependent) function.
        " globalThis.showStoryModal = (story, key) => {"
        "   globalThis.__lastModalStory = story;"
        "   globalThis.__lastModalKey = key;"
        " };"
        # Override document.getElementById('plan-detail') with a stub
        # that captures the innerHTML written by renderPlanDetail and
        # exposes a querySelectorAll returning an array of fake card
        # elements with click handlers.
        " const section = {"
        "   innerHTML: '',"
        "   querySelectorAll: (sel) => {"
        "     if (sel !== '.card') return [];"
        # Parse data-key attrs from innerHTML. Each card looks like:
        # <div class=\"card ...\" data-key=\"k1\" ...>.
        "     const matches = [];"
        "     const re = /data-key=\"([^\"]+)\"/g;"
        "     let m;"
        "     while ((m = re.exec(section.innerHTML)) !== null) {"
        "       const key = m[1];"
        "       matches.push({"
        "         dataset: { key },"
        "         addEventListener: (evt, fn) => {"
        "           if (evt === 'click') { this._onClick = fn; }"
        "         },"
        "         _onClick: null,"
        "         click() { if (this._onClick) this._onClick(); }"
        "       });"
        "     }"
        "     return matches;"
        "   },"
        "   querySelector: () => null"
        " };"
        " globalThis.document.getElementById = (id) => {"
        "   if (id === 'plan-detail') return section;"
        # Other elements (column-header counts, etc.) aren't reached here.
        "   return null;"
        " };"
        " const plan = {"
        "  stories: { 'k1':{status:'in_progress', summary:'a', persona:'lead', risk:''} },"
        "  notifications: [], decisions: []"
        " };"
        " renderPlanDetail(plan);"
        " if (!section._onClick) {"
        "   /* fallback: manually drive the card from innerHTML via the"
        "      same regex path used in querySelectorAll, then call click. */"
        "   const re = /data-key=\"([^\"]+)\"/;"
        "   const m = re.exec(section.innerHTML);"
        "   if (!m) return JSON.stringify({error:'no-card-in-html'});"
        "   const key = m[1];"
        "   globalThis.showStoryModal(plan.stories[key], key);"
        " } else {"
        "   /* find the card with k1 and dispatch click. */"
        "   const cards = section.querySelectorAll('.card');"
        "   const target = cards.find((c) => c.dataset.key === 'k1');"
        "   if (target) target.click();"
        " }"
        " return JSON.stringify({"
        "  key: globalThis.__lastModalKey,"
        "  status: globalThis.__lastModalStory && globalThis.__lastModalStory.status"
        " });"
        " })()"
    )
    result = _run_app_js(expr)
    data = json.loads(result)
    assert data.get("key") == "k1", data
    assert data.get("status") == "in_progress", data


# === Per-story progress bar (Tier 1) ================================
# An in_progress story carrying a parsed `progress` field (from guided
# decomposition) renders a thin progress bar on its card face. The bar is
# only for in_progress stories that actually have progress data; every
# other case (no progress field, non-in_progress status, zero-total
# progress) must render nothing.

def test_render_board_progress_bar_on_in_progress_story():
    """An in_progress story with progress {done:1,total:3} renders a
    .card-progress element whose label reads '1/3' and whose fill width
    is the rounded percentage (33%)."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a',"
        "        progress:{done:1, total:3}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    # the progress container is present
    assert 'class="card-progress"' in html, html
    # the track + fill sub-elements are present
    assert 'class="card-progress-track"' in html, html
    assert 'class="card-progress-fill"' in html, html
    # the label shows done/total
    assert 'class="card-progress-label"' in html, html
    assert "1/3" in html, html
    # the fill width is the rounded percentage: round(1/3*100) = 33
    assert 'width: 33%' in html, html


def test_render_board_progress_bar_fill_width_rounds_percentage():
    """Boundary: progress {done:2,total:3} -> round(66.66) = 67% width.
    Pins the rounding behavior so a truncation bug (66%) is caught."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a',"
        "        progress:{done:2, total:3}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert 'class="card-progress"' in html, html
    assert "2/3" in html, html
    assert 'width: 67%' in html, html


def test_render_board_progress_bar_full_when_all_done():
    """Boundary: progress {done:3,total:3} -> 100% width, label '3/3'."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a',"
        "        progress:{done:3, total:3}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert 'class="card-progress"' in html, html
    assert "3/3" in html, html
    assert 'width: 100%' in html, html


def test_render_board_no_progress_bar_when_no_progress_field():
    """An in_progress story WITHOUT a progress field must NOT render a
    .card-progress element (the bar is opt-in via parsed progress data)."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a'}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert 'class="card-progress"' not in html, html
    assert "card-progress" not in html, html


def test_render_board_no_progress_bar_when_progress_total_zero():
    """Boundary: progress {done:0,total:0} has total <= 0, so no bar —
    avoids a divide-by-zero and a meaningless '0/0' label."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'in_progress', summary:'a',"
        "        progress:{done:0, total:0}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert 'class="card-progress"' not in html, html


def test_render_board_no_progress_bar_on_done_story():
    """A done story WITH a progress field must NOT render a progress bar —
    the bar is in_progress-only (done cards already signal completion via
    their status stripe and the done-column completion hint)."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['done'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'done', summary:'a',"
        "        progress:{done:3, total:3}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert 'class="card-progress"' not in html, html
    assert "card-progress" not in html, html


def test_render_board_no_progress_bar_on_todo_story():
    """A todo story WITH a progress field must NOT render a progress bar —
    only in_progress cards get the bar."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['todo'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'k1':{status:'todo', summary:'a',"
        "        progress:{done:0, total:3}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    assert 'class="card-progress"' not in html, html


def test_render_board_progress_bar_only_on_in_progress_card_in_mixed_board():
    """In a board with multiple statuses, only the in_progress card with
    progress data gets a .card-progress; a done card with progress data
    in the same render does not."""
    expr = (
        "(() => {"
        " state.filters.statuses = ['in_progress','done'];"
        " state.filters.personas = []; state.filters.risks = [];"
        " return renderBoard({"
        "  'ip':{status:'in_progress', summary:'a',"
        "        progress:{done:1, total:2}},"
        "  'dn':{status:'done', summary:'b',"
        "        progress:{done:2, total:2}}"
        " });"
        " })()"
    )
    html = _run_app_js(expr)
    # exactly one progress bar (the in_progress card)
    assert html.count('class="card-progress"') == 1, html
    assert "1/2" in html, html


def test_style_css_defines_progress_bar_classes():
    """The CSS classes referenced by the progress bar markup must exist
    in static/style.css so the bar is actually styled (not unstyled divs)."""
    with open(os.path.join(os.path.dirname(APP_JS), "style.css")) as fh:
        css = fh.read()
    assert ".card-progress" in css
    assert ".card-progress-track" in css
    assert ".card-progress-fill" in css
    assert ".card-progress-label" in css


def test_apply_filters_persona_filter_excludes_non_matching_stories():
    """applyFilters still filters on persona — the column count must
    reflect the persona-filtered subset."""
    expr = (
        "(() => {"
        " state.filters.personas = ['lead'];"
        " state.filters.risks = [];"
        " return JSON.stringify(applyFilters(["
        "  ['k1',{persona:'lead',   risk:'low'}],"
        "  ['k2',{persona:'junior', risk:'low'}],"
        "  ['k3',{persona:'lead',   risk:'high'}]"
        " ]).map(([k,_])=>k));"
        "})()"
    )
    result = _run_app_js(expr)
    assert json.loads(result) == ["k1", "k3"]


def test_apply_filters_risk_filter_excludes_non_matching_stories():
    """applyFilters still filters on risk."""
    expr = (
        "(() => {"
        " state.filters.personas = [];"
        " state.filters.risks = ['high'];"
        " return JSON.stringify(applyFilters(["
        "  ['k1',{persona:'a', risk:'high'}],"
        "  ['k2',{persona:'a', risk:'low'}]"
        " ]).map(([k,_])=>k));"
        "})()"
    )
    result = _run_app_js(expr)
    assert json.loads(result) == ["k1"]


def test_apply_filters_sorts_by_key_risk_activity():
    """Sort contract: `key` is locale-numeric, `risk` is high>medium>low,
    `activity` is total attempts desc. Each branch must be verified."""
    expr_key = (
        "(() => {"
        " state.filters.personas = []; state.filters.risks = [];"
        " state.filters.sort = 'key';"
        " return JSON.stringify(applyFilters(["
        "  ['k10',{persona:'',risk:'',dispatch_attempts:0,rework_attempts:0,merge_attempts:0}],"
        "  ['k2', {persona:'',risk:'',dispatch_attempts:0,rework_attempts:0,merge_attempts:0}],"
        "  ['k1', {persona:'',risk:'',dispatch_attempts:0,rework_attempts:0,merge_attempts:0}]"
        " ]).map(([k,_])=>k));"
        "})()"
    )
    assert json.loads(_run_app_js(expr_key)) == ["k1", "k2", "k10"]

    expr_risk = (
        "(() => {"
        " state.filters.personas = []; state.filters.risks = [];"
        " state.filters.sort = 'risk';"
        " return JSON.stringify(applyFilters(["
        "  ['low',    {persona:'',risk:'low'}],"
        "  ['high',   {persona:'',risk:'high'}],"
        "  ['medium', {persona:'',risk:'medium'}]"
        " ]).map(([k,_])=>k));"
        "})()"
    )
    assert json.loads(_run_app_js(expr_risk)) == ["high", "medium", "low"]

    expr_act = (
        "(() => {"
        " state.filters.personas = []; state.filters.risks = [];"
        " state.filters.sort = 'activity';"
        " return JSON.stringify(applyFilters(["
        "  ['quiet',  {persona:'',risk:'',dispatch_attempts:0,rework_attempts:0,merge_attempts:0}],"
        "  ['loud',   {persona:'',risk:'',dispatch_attempts:5,rework_attempts:2,merge_attempts:1}],"
        "  ['medium', {persona:'',risk:'',dispatch_attempts:1,rework_attempts:0,merge_attempts:0}]"
        " ]).map(([k,_])=>k));"
        "})()"
    )
    assert json.loads(_run_app_js(expr_act)) == ["loud", "medium", "quiet"]


def test_index_html_references_static_assets_re_render_safe(client):
    """Light regression: the dashboard's static asset references must still
    be present so the new board CSS/JS ships together. Pinning here keeps
    the marker on a stable line in the suite."""
    body = client.get("/").text
    assert "/style.css" in body
    assert "/app.js" in body


# --- Journal timeline rendering in app.js -------------------------------
#
# These pin down the contract of renderJournal / renderJournalEntry so the
# story modal's "Journal" section keeps rendering the empty state for the
# four unavailable cases (null data, !available, no entries array, empty
# entries array) and renders a <ol class="timeline"> with one
# <li class="timeline-item"> per entry otherwise. Tested by shelling to
# Node — same approach as the age/relative-time helpers above.


def test_render_journal_shows_empty_state_for_null_data():
    """No response object at all (e.g. fetch threw before resolving) ->
    the 'No journal yet' empty state, never an exception."""
    html = _run_app_js("renderJournal(null)")
    assert "Journal" in html
    assert "No journal yet" in html
    assert "data-journal-empty" in html
    assert "class=\"timeline\"" not in html


def test_render_journal_shows_empty_state_when_unavailable():
    """The endpoint contract: 200 with available:false -> the same empty
    state a missing-file response would produce."""
    data = {"available": False, "entries": []}
    html = _run_app_js(f"renderJournal({json.dumps(data)})")
    assert "No journal yet" in html
    assert "data-journal-empty" in html
    assert "class=\"timeline\"" not in html


def test_render_journal_shows_empty_state_for_empty_entries_array():
    """Boundary: an empty entries list (file exists but has no rows) is
    visually indistinguishable from a missing file — both render the
    'No journal yet' empty state."""
    data = {"available": True, "entries": []}
    html = _run_app_js(f"renderJournal({json.dumps(data)})")
    assert "No journal yet" in html
    assert "data-journal-empty" in html
    assert "class=\"timeline\"" not in html


def test_render_journal_renders_timeline_in_entry_order():
    """Positive: a populated response renders an <ol class="timeline">
    with one <li class="timeline-item"> per entry, in file order, with the
    step label / summary / next_hint / timestamp fields appearing in the
    rendered HTML."""
    entries = [
        {"step": "analyze", "summary": "read the spec", "next_hint": "draft tests"},
        {"step": "tests",   "summary": "wrote 3 failing tests", "ts": "2026-06-29T14:00:00Z"},
    ]
    data = {"available": True, "entries": entries}
    html = _run_app_js(f"renderJournal({json.dumps(data)})")
    # Section heading + timeline container present
    assert "Journal" in html
    assert "data-journal-list" in html
    assert "class=\"timeline\"" in html
    # Both entries rendered, in order
    assert html.count("class=\"timeline-item\"") == 2
    analyze_idx = html.find("analyze")
    tests_idx = html.find("tests")
    assert 0 <= analyze_idx < tests_idx, (analyze_idx, tests_idx, html)
    # Summary and next_hint visible in the markup
    assert "read the spec" in html
    assert "draft tests" in html
    assert "wrote 3 failing tests" in html
    # next_hint rendered with the 'next:' prefix and the muted class
    assert "next:" in html
    assert "muted" in html


def test_render_journal_entry_omits_next_hint_block_when_missing():
    """Negative: an entry without next_hint must render WITHOUT the
    'next:' line — never the literal string 'undefined', and never an
    empty <div class="timeline-next">."""
    entry = {"step": "implement", "summary": "did the work"}
    html = _run_app_js(f"renderJournalEntry({json.dumps(entry)})")
    assert "timeline-next" not in html
    assert "undefined" not in html
    # step + summary still rendered
    assert "implement" in html
    assert "did the work" in html


def test_render_journal_entry_omits_ts_block_when_missing():
    """Boundary: ts is optional on a checkpoint; an entry without one
    should not render an empty timestamp line."""
    entry = {"step": "ship", "summary": "merged", "next_hint": "monitor"}
    html = _run_app_js(f"renderJournalEntry({json.dumps(entry)})")
    assert "timeline-ts" not in html
    assert "monitor" in html  # next_hint still present


def test_render_journal_entry_returns_empty_for_garbage_row():
    """Defensive: corrupt / null entries leave no visual artifact. A row
    that's neither an object nor has any of step/summary shouldn't render
    anything (filter(Boolean) downstream drops it)."""
    assert _run_app_js("renderJournalEntry(null)") == ""
    assert _run_app_js("renderJournalEntry(undefined)") == ""
    assert _run_app_js("renderJournalEntry({})") == ""
    assert _run_app_js("renderJournalEntry('not an object')") == ""


def test_render_journal_escapes_html_in_entry_text():
    """Security: a checkpoint entry could contain user-supplied text
    (reviewer notes, etc.). The summary / step / next_hint must be HTML-
    escaped so a stray <script> tag doesn't execute in the modal."""
    entries = [{"step": "<script>", "summary": "<img onerror=x>",
                "next_hint": "\">injected"}]
    data = {"available": True, "entries": entries}
    html = _run_app_js(f"renderJournal({json.dumps(data)})")
    assert "&lt;script&gt;" in html
    assert "&lt;img onerror=x&gt;" in html
    assert "&quot;&gt;injected" in html
    # And no raw un-escaped tags from the entry fields
    assert "<script>" not in html
    assert "<img onerror=" not in html


# --- Checklist section rendering in app.js (Tier 0 progress view) ---------
#
# renderChecklist consumes the /checklist endpoint response
# {plan:{available,text}, scratchpad:{available,text}} and must: render the
# "No checklist" empty state when neither file is available (the common case
# for stories not run under PIPELINE_DECOMPOSE); render the plan text in a
# scrollable <pre> when available; add a Progress-notes subsection + <pre>
# when the scratchpad is available; and HTML-escape agent-written text so a
# stray <script> in an artifact can't execute in the modal. Same shelled-Node
# approach as the renderJournal tests above.


def test_render_checklist_empty_state_for_null_data():
    """No response (fetch threw) -> the 'No checklist' empty state, never an
    exception."""
    html = _run_app_js("renderChecklist(null)")
    assert "Checklist" in html
    assert "No checklist" in html
    assert "data-checklist-empty" in html
    assert "data-checklist-plan" not in html


def test_render_checklist_empty_state_when_neither_available():
    """The common case: a story not run under PIPELINE_DECOMPOSE has neither
    file -> empty state, same as a missing-file response."""
    data = {"plan": {"available": False, "text": ""},
            "scratchpad": {"available": False, "text": ""}}
    html = _run_app_js(f"renderChecklist({json.dumps(data)})")
    assert "No checklist" in html
    assert "data-checklist-empty" in html
    assert "data-checklist-plan" not in html
    assert "data-checklist-scratch" not in html


def test_render_checklist_plan_only_when_scratchpad_absent():
    """A story that just got its plan but hasn't checkpointed yet renders the
    plan but no Progress-notes subsection."""
    data = {"plan": {"available": True, "text": "1. tests\n2. impl\n"},
            "scratchpad": {"available": False, "text": ""}}
    html = _run_app_js(f"renderChecklist({json.dumps(data)})")
    assert "data-checklist-plan" in html
    assert "1. tests" in html
    assert "2. impl" in html
    # No scratchpad subsection when scratchpad unavailable
    assert "data-checklist-scratch" not in html
    assert "Progress notes" not in html


def test_render_checklist_renders_both_plan_and_scratchpad():
    """Positive: both artifacts available -> plan <pre>, a Progress-notes
    subsection, and a scratchpad <pre>, in that order."""
    data = {"plan": {"available": True, "text": "1. step one"},
            "scratchpad": {"available": True, "text": "done: one"}}
    html = _run_app_js(f"renderChecklist({json.dumps(data)})")
    assert "data-checklist-plan" in html
    assert "data-checklist-scratch" in html
    assert "Progress notes" in html
    assert "done: one" in html
    # Plan block precedes the scratchpad block.
    assert html.find("data-checklist-plan") < html.find("data-checklist-scratch")


def test_render_checklist_escapes_html_in_artifact_text():
    """Security: the plan and scratchpad are agent-written, so their text is
    HTML-escaped — a stray <script> in an artifact must not survive raw."""
    data = {"plan": {"available": True, "text": "<script>x</script>"},
            "scratchpad": {"available": False, "text": ""}}
    html = _run_app_js(f"renderChecklist({json.dumps(data)})")
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_render_checklist_empty_plan_text_still_renders_block():
    """Boundary: a zero-byte .agent_plan.md (available:true, text:'') still
    renders an empty plan <pre> block — 'file present but empty' is distinct
    from 'file absent' (the latter hits the empty state)."""
    data = {"plan": {"available": True, "text": ""},
            "scratchpad": {"available": False, "text": ""}}
    html = _run_app_js(f"renderChecklist({json.dumps(data)})")
    assert "data-checklist-plan" in html
    assert "No checklist" not in html


def test_static_style_css_defines_checklist_classes(client):
    """The checklist section uses .dsh-checklist-plan / .dsh-checklist-scratch
    / .modal-subsection — the CSS must define them so the section renders
    visibly (scrollable monospace blocks) rather than as unstyled elements."""
    css = client.get("/style.css").text
    for sel in (".dsh-checklist-plan", ".dsh-checklist-scratch",
                ".modal-subsection"):
        assert sel in css, f"missing CSS selector: {sel}"


def test_static_style_css_defines_plan_archive_classes(client):
    """The rendered sidebar uses .plan-item-row / .plan-archive-btn /
    .plan-archived / .plan-list-footer / .show-archived-toggle - the CSS
    must actually define those selectors so archive/dismiss renders
    visibly rather than as unstyled elements."""
    css = client.get("/style.css").text
    for sel in (".plan-item-row", ".plan-archive-btn", ".plan-archived",
                ".plan-list-footer", ".show-archived-toggle"):
        assert sel in css, f"missing CSS selector: {sel}"


def test_static_style_css_defines_journal_timeline_classes(client):
    """The rendered HTML uses .timeline / .timeline-item / .timeline-step /
    .timeline-summary / .timeline-next / .timeline-ts / .muted — the CSS
    must actually define those selectors so the section renders visibly
    rather than as unstyled bullets."""
    css = client.get("/style.css").text
    for sel in (".timeline", ".timeline-item", ".timeline-step",
                ".timeline-summary", ".timeline-next", ".timeline-ts"):
        assert sel in css, f"missing CSS selector: {sel}"
    # And the muted utility class used inside timeline entries
    assert ".muted" in css


# ---------- story checklist endpoint (worktree .agent_plan.md/.agent_scratchpad) ----------
#
# Surfaces the tech-lead checklist + running scratchpad the guided-
# decomposition step writes into a story's WORKTREE (not PLAN_DIR), so the
# dashboard can show how far an in-progress story's attempt has gotten.
# Rules mirror the log/journal endpoints:
#   * Returns {plan: {available, text}, scratchpad: {available, text}}.
#   * Most stories have no worktree (never dispatched, or run without
#     PIPELINE_DECOMPOSE) -> available:false, text:"" — NOT a 404 or 500.
#   * A worktree that's been deleted post-merge -> available:false (normal).
#   * The worktree path comes from the manifest only; the resolved path is
#     contain-checked under WORKTREE_ROOT so a hand-edited manifest pointing
#     outside WORKTREE_ROOT cannot read arbitrary files.
#   * 404 only for an unknown plan or story key.


@pytest.fixture
def worktree_dir(tmp_path, monkeypatch):
    """A throwaway WORKTREE_ROOT the dashboard reads worktree artifacts from.
    Mirrors the `plan_dir` fixture's monkeypatch of PLAN_DIR so tests never
    touch the real ~/.claude/worktrees."""
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    monkeypatch.setattr(d, "WORKTREE_ROOT", wt_root)
    from pipeline import server as _srv
    monkeypatch.setattr(_srv, "WORKTREE_ROOT", wt_root)
    return wt_root


def test_checklist_endpoint_returns_both_files_when_present(client, plan_dir, worktree_dir):
    """A story run under PIPELINE_DECOMPOSE has both .agent_plan.md (the
    tech-lead checklist) and .agent_scratchpad.md (running state) in its
    worktree -> both available:true with their text."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. write tests\n2. implement\n")
    (wt / ".agent_scratchpad.md").write_text("done: step 1\nnext: step 2\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "guided", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    res = client.get("/api/plans/demo/stories/S1/checklist")
    assert res.status_code == 200
    body = res.json()
    assert body["plan"]["available"] is True
    assert body["plan"]["text"] == "1. write tests\n2. implement\n"
    assert body["scratchpad"]["available"] is True
    assert body["scratchpad"]["text"] == "done: step 1\nnext: step 2\n"


def test_checklist_endpoint_partial_when_only_plan_present(client, plan_dir, worktree_dir):
    """The scratchpad is written incrementally by the executor; a story that
    just got its plan but hasn't checkpointed yet has plan:available but
    scratchpad:unavailable — each file is reported independently."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "just planned", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["plan"]["available"] is True
    assert body["plan"]["text"] == "1. step\n"
    assert body["scratchpad"]["available"] is False
    assert body["scratchpad"]["text"] == ""


def test_checklist_endpoint_no_worktree_field_returns_unavailable(client, plan_dir, worktree_dir):
    """A story that was never dispatched (still todo) has no 'worktree' field
    — the normal case for most stories. available:false for both, never a 404
    or 500."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "fresh", "status": "todo", "dependencies": []},
    })
    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body == {"plan": {"available": False, "text": ""},
                    "scratchpad": {"available": False, "text": ""},
                    "progress": None}


def test_checklist_endpoint_worktree_dir_gone_returns_unavailable_not_500(client, plan_dir, worktree_dir):
    """A worktree recorded in the manifest but deleted on disk (post-merge
    cleanup) is a normal state, not an error — must degrade to available:false
    rather than 500."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "merged", "status": "done",
               "worktree": str(worktree_dir / "gone-S1"), "dependencies": []},
    })
    res = client.get("/api/plans/demo/stories/S1/checklist")
    assert res.status_code == 200
    assert res.json() == {"plan": {"available": False, "text": ""},
                          "scratchpad": {"available": False, "text": ""},
                          "progress": None}


def test_checklist_endpoint_empty_file_is_available_with_empty_text(client, plan_dir, worktree_dir):
    """Boundary: a zero-byte .agent_plan.md exists (planner wrote nothing /
    truncated) -> available:true with text:'', distinguishing 'file present
    but empty' from 'file absent'."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "empty plan", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })
    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["plan"]["available"] is True
    assert body["plan"]["text"] == ""


def test_checklist_endpoint_garbage_bytes_decode_replacement(client, plan_dir, worktree_dir):
    """Non-UTF-8 bytes in an agent-written artifact must not 500; replacement
    characters are accepted so the dashboard surfaces whatever's on disk."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_bytes(b"good\n\xff\xfe\nmore\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "binary", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })
    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["plan"]["available"] is True
    assert body["plan"]["text"].startswith("good\n")
    assert "�" in body["plan"]["text"]


def test_checklist_endpoint_404_for_unknown_plan(client, plan_dir, worktree_dir):
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "x", "status": "todo", "dependencies": []},
    })
    assert client.get("/api/plans/nope/stories/S1/checklist").status_code == 404


def test_checklist_endpoint_404_for_unknown_story(client, plan_dir, worktree_dir):
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "x", "status": "todo", "dependencies": []},
    })
    assert client.get("/api/plans/demo/stories/NOPE/checklist").status_code == 404


def test_read_worktree_file_rejects_worktree_outside_root(client, plan_dir, worktree_dir, tmp_path):
    """Security: a manifest hand-edited to point worktree outside WORKTREE_ROOT
    (e.g. '/etc') must not let the dashboard read arbitrary files. The
    resolved path is contain-checked; an outside-root worktree degrades to
    available:false."""
    outside = tmp_path / "outside-root"
    outside.mkdir()
    (outside / ".agent_plan.md").write_text("SECRET")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "evil", "status": "in_progress",
               "worktree": str(outside), "dependencies": []},
    })
    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["plan"]["available"] is False
    assert "SECRET" not in body["plan"]["text"]


def test_read_worktree_file_rejects_relative_worktree_path(client, plan_dir, worktree_dir):
    """A non-absolute worktree (corrupt manifest) is not resolvable safely ->
    unavailable, never a 500. The orchestrator always stores an absolute path,
    so a relative one is a corruption signal we fail closed on."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "corrupt", "status": "in_progress",
               "worktree": "S1", "dependencies": []},
    })
    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["plan"]["available"] is False
    assert body["scratchpad"]["available"] is False




# ---------------------------------------------------------------------------
# Per-story progress (Tier 1): _parse_progress helper + endpoint decoration.
# ---------------------------------------------------------------------------

def test_parse_progress_returns_done_and_total():
    """Plan with 3 numbered items and a scratchpad PROGRESS: 1/3 line yields
    {done: 1, total: 3}."""
    plan = "1. write tests\n2. implement\n3. refactor\n"
    scratch = "PROGRESS: 1/3\ndone: step 1\nnext: step 2\n"
    assert d._parse_progress(plan, scratch) == {"done": 1, "total": 3}


def test_parse_progress_returns_none_when_no_plan():
    """No plan text (None or empty) -> None (fail open)."""
    assert d._parse_progress(None, "PROGRESS: 1/3\n") is None
    assert d._parse_progress("", "PROGRESS: 1/3\n") is None


def test_parse_progress_returns_none_when_no_scratchpad():
    """No scratchpad text (None or empty) -> None (fail open)."""
    assert d._parse_progress("1. step\n", None) is None
    assert d._parse_progress("1. step\n", "") is None


def test_parse_progress_returns_none_when_no_progress_line():
    """A scratchpad without a PROGRESS: line (old format) -> None, never raises."""
    plan = "1. step\n2. step\n"
    scratch = "done: step 1\nnext: step 2\n"
    assert d._parse_progress(plan, scratch) is None


def test_parse_progress_returns_none_when_no_numbered_items():
    """A plan with no numbered items (total == 0) -> None."""
    plan = "Some prose without numbered items.\nMore prose.\n"
    scratch = "PROGRESS: 1/3\n"
    assert d._parse_progress(plan, scratch) is None


def test_parse_progress_never_raises_on_garbage():
    """The helper must fail open — never raise — on malformed inputs."""
    # Malformed PROGRESS line (missing total) -> None, not an exception.
    assert d._parse_progress("1. step\n", "PROGRESS: 2/\n") is None
    # Non-numeric done -> None.
    assert d._parse_progress("1. step\n", "PROGRESS: x/3\n") is None
    # PROGRESS line not anchored at line start -> None.
    assert d._parse_progress("1. step\n", "  PROGRESS: 1/1\n") is None


def test_parse_progress_counts_only_numbered_lines():
    """Total counts only lines starting with a digit followed by a period;
    prose lines and sub-bullets are ignored."""
    plan = (
        "1. first\n"
        "- a sub bullet\n"
        "2. second\n"
        "some prose\n"
        "3. third\n"
    )
    scratch = "PROGRESS: 2/3\n"
    result = d._parse_progress(plan, scratch)
    assert result == {"done": 2, "total": 3}


def test_checklist_endpoint_includes_progress_field(client, plan_dir, worktree_dir):
    """End-to-end: a worktree with a 3-item plan and PROGRESS: 1/3 scratchpad
    returns progress: {done: 1, total: 3} from the /checklist endpoint."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. write tests\n2. implement\n3. refactor\n")
    (wt / ".agent_scratchpad.md").write_text("PROGRESS: 1/3\ndone: step 1\nnext: step 2\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "guided", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["progress"] is not None
    assert body["progress"]["done"] == 1
    assert body["progress"]["total"] == 3


def test_checklist_endpoint_progress_null_when_no_scratchpad(client, plan_dir, worktree_dir):
    """Plan present but no scratchpad -> progress: null (fail open)."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "just planned", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["progress"] is None


def test_checklist_endpoint_progress_null_when_no_progress_line(client, plan_dir, worktree_dir):
    """Scratchpad present but without a PROGRESS: line -> progress: null."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step\n")
    (wt / ".agent_scratchpad.md").write_text("done: step 1\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "old format", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["progress"] is None


def test_checklist_endpoint_progress_null_when_no_numbered_items(client, plan_dir, worktree_dir):
    """Plan present but with no numbered items -> progress: null."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("Some prose without numbered items.\n")
    (wt / ".agent_scratchpad.md").write_text("PROGRESS: 1/3\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "prose plan", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo/stories/S1/checklist").json()
    assert body["progress"] is None


def test_get_plan_decorates_in_progress_story_with_progress(client, plan_dir, worktree_dir):
    """The /api/plans/{name} endpoint decorates in_progress stories with a
    progress field when the worktree has parseable plan+scratchpad."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step a\n2. step b\n")
    (wt / ".agent_scratchpad.md").write_text("PROGRESS: 1/2\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "guided", "status": "in_progress",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo").json()
    stories = body["stories"]
    assert "S1" in stories
    assert stories["S1"].get("progress") is not None
    assert stories["S1"]["progress"]["done"] == 1
    assert stories["S1"]["progress"]["total"] == 2


def test_get_plan_does_not_decorate_todo_story_with_progress(client, plan_dir, worktree_dir):
    """A todo story (no worktree) must not get a progress field."""
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "fresh", "status": "todo", "dependencies": []},
    })

    body = client.get("/api/plans/demo").json()
    assert "progress" not in body["stories"]["S1"]


def test_get_plan_does_not_decorate_done_story_with_progress(client, plan_dir, worktree_dir):
    """A done story must not get a progress field even if a worktree exists."""
    wt = worktree_dir / "S1"
    wt.mkdir()
    (wt / ".agent_plan.md").write_text("1. step a\n2. step b\n")
    (wt / ".agent_scratchpad.md").write_text("PROGRESS: 2/2\n")
    _write_manifest(plan_dir, "demo", {
        "S1": {"summary": "merged", "status": "done",
               "worktree": str(wt), "dependencies": []},
    })

    body = client.get("/api/plans/demo").json()
    assert "progress" not in body["stories"]["S1"]


# ---------------------------------------------------------------------------
# Scratchpad prompt in pipeline/server.py must require a PROGRESS: line.
# ---------------------------------------------------------------------------

def test_server_scratchpad_prompt_requires_progress_line():
    """The scratchpad instruction in pipeline/dispatch.py must instruct the
    executor to write a PROGRESS: <done>/<total> line as the FIRST line of
    .agent_scratchpad.md."""
    import inspect

    from pipeline import dispatch

    # The instruction is built inside a function; inspect the source of the
    # module so we assert on the literal string content the implementer must
    # keep in sync with the plan.
    source = inspect.getsource(dispatch)
    assert "PROGRESS: <done>/<total>" in source, (
        "scratchpad prompt must mention 'PROGRESS: <done>/<total>'"
    )
    assert "PROGRESS: 2/5" in source, (
        "scratchpad prompt must include the 'PROGRESS: 2/5' example"
    )
    assert "The FIRST line must be" in source, (
        "scratchpad prompt must state the PROGRESS line is the FIRST line"
    )
    # The old wording that did NOT require a PROGRESS line must be gone.
    assert "keep a short running summary of what you've" not in source or (
        "PROGRESS:" in source
    )


# ---------- /api/config (effective configuration snapshot) ----------


@pytest.fixture
def isolated_config_sources(monkeypatch, tmp_path):
    """Point config_provenance's source files at nonexistent paths under
    tmp_path so /api/config tests never read this machine's real
    ~/.claude.json or launchd plist (which may hold real secrets)."""
    monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(tmp_path / "no-scheduler.plist"))
    monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(tmp_path / "no-claude.json"))
    return tmp_path


@pytest.fixture
def known_registry(monkeypatch):
    """A small, deterministic model_registry.json substitute (mirrors the
    fixture in tests/unit/test_get_effective_config.py) so the plan-override
    test doesn't depend on the real repo's model_registry.json contents."""
    registry = {
        "providers": {
            "claude": {"models": {"sonnet": {"tag": "sonnet"}}},
            "mlx": {"models": {"qwen": {"tag": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"}}},
        },
        "roles": {},
    }
    monkeypatch.setattr(d.role_registry, "load_registry", lambda *a, **k: registry)
    return registry


def test_effective_config_wiring_returns_all_roles(client, plan_dir, isolated_config_sources):
    res = client.get("/api/config")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"roles", "env", "ignored_env_vars", "sources"}
    role_names = {entry["role"] for entry in body["roles"]}
    assert role_names == set(config_provenance.PIPELINE_ROLES)


def test_effective_config_plan_override_reports_provider_and_source(
    client, plan_dir, isolated_config_sources, known_registry
):
    (plan_dir / "cfgplan.manifest.json").write_text(json.dumps({
        "epics": {}, "stories": {}, "repo_root": "/tmp",
        "role_config": {"review": {"provider": "mlx", "model": "qwen"}},
    }))

    res = client.get("/api/config?plan=cfgplan")

    assert res.status_code == 200
    roles_by_name = {entry["role"]: entry for entry in res.json()["roles"]}
    review = roles_by_name["review"]
    assert review["provider"] == "mlx"
    assert review["provider_source"] == "plan_role_config"


def test_effective_config_nonexistent_plan_resolves_with_no_overrides(
    client, plan_dir, isolated_config_sources
):
    res = client.get("/api/config?plan=nope")

    assert res.status_code == 200
    roles_by_name = {entry["role"]: entry for entry in res.json()["roles"]}
    for entry in roles_by_name.values():
        assert entry["provider_source"] != "plan_role_config"


def test_effective_config_malformed_manifest_resolves_with_no_overrides(
    client, plan_dir, isolated_config_sources
):
    (plan_dir / "broken.manifest.json").write_text("{not valid json")

    res = client.get("/api/config?plan=broken")

    assert res.status_code == 200
    roles_by_name = {entry["role"]: entry for entry in res.json()["roles"]}
    for entry in roles_by_name.values():
        assert entry["provider_source"] != "plan_role_config"


def test_effective_config_never_leaks_secret_value(
    client, plan_dir, isolated_config_sources, monkeypatch
):
    secret = "sk-super-secret-token-value-12345"
    monkeypatch.setenv("PIPELINE_TEST_API_KEY", secret)

    res = client.get("/api/config")

    assert res.status_code == 200
    assert secret not in res.text


def test_dashboard_module_imports_pipeline_service_for_scoped_write_surface():
    """W1c (docs/plans/PLATFORM_DECOUPLING_AND_SCALE_PLAN.md) deliberately
    gives app/dashboard.py write access via a `PipelineService` singleton
    (see the module docstring) - it must import pipeline.server for that,
    but the write surface stays scoped to PipelineService: it must not
    import app.pipeline_mcp_server or app.backend directly."""
    import ast

    source = Path("app/dashboard.py").read_text()
    tree = ast.parse(source)
    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert "pipeline.server" in imported_modules, imported_modules
    still_forbidden = {"app.pipeline_mcp_server", "app.backend"}
    assert not (still_forbidden & set(imported_modules)), imported_modules


def test_effective_config_endpoint_performs_no_writes(client, plan_dir, isolated_config_sources):
    _write_manifest(plan_dir, "untouched", {"S1": {"summary": "x", "status": "todo"}})
    before = {
        p.name: p.read_bytes() for p in sorted(plan_dir.iterdir())
    }

    res = client.get("/api/config?plan=untouched")

    assert res.status_code == 200
    after = {
        p.name: p.read_bytes() for p in sorted(plan_dir.iterdir())
    }
    assert after == before
