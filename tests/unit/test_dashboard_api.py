"""Tests for the dashboard's read-only API (dashboard.py): plans list/get,
usage, dispatch_health, aggregate attempt/failure metrics, last_activity,
journal endpoint, and story log tail endpoint.

Split out of test_dashboard.py to keep it under the project's line-count
target; shared fixtures/helpers moved to tests.unit._dashboard_helpers.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from tests.unit._dashboard_helpers import (  # noqa: F401
    _write_manifest,
    client,
    plan_dir,
)


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
