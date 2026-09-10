"""Wedge decoration on GET /api/plans/{plan_name} (app/dashboard.py get_plan).

For stories with status == "in_progress", get_plan additionally computes a
wedge state (dead pid / stale worktree activity) and attaches it to the
RESPONSE dict as story["wedge"] = {"wedged": ..., "reasons": ..., "measured":
...}. Contract under test:

- Only in_progress stories are probed (bounded cost: a `ps -p` subprocess and
  two stat calls each) — todo/done stories get NO "wedge" key at all and the
  signal collector is never called for them.
- A healthy in_progress story still gets wedge.wedged False (present, not
  absent — the UI needs the explicit false to distinguish "checked and fine"
  from "not checked").
- Fail-open per story: if signal collection raises, the endpoint still returns
  200 and that story simply has no "wedge" key.
- Existing fields are preserved verbatim — we never rewrite the story: the
  on-disk manifest is byte-identical after the request and the response story
  keeps every pre-existing field.

The signal collector (pipeline.wedge.collect_story_wedge_signals) is stubbed
here — it is I/O (subprocess + stat) and is the prerequisite story's surface.
The pure verdict (pipeline.wedge.wedge_verdict) is exercised for real.
"""
from __future__ import annotations

import time

import pytest

import pipeline.config
import pipeline.wedge
from app import dashboard as d
from tests.unit._dashboard_helpers import (  # noqa: F401
    _write_manifest,
    client,
    plan_dir,
)

PLAN = "wedge-plan"


def _stub_collector(monkeypatch, signals_by_key, calls):
    """Stub collect_story_wedge_signals in BOTH namespaces the implementation
    might bind it in (``from pipeline.wedge import ...`` into app.dashboard is
    the specified wiring; module-attribute access is the fallback). raising=
    False keeps the suite runnable (RED on the missing wiring) before the
    implementation exists."""

    def _collect(plan_name, story_key, story):
        calls.append(story_key)
        if story_key not in signals_by_key:
            raise AssertionError(f"collector called for unprobed story {story_key!r}")
        return signals_by_key[story_key]

    monkeypatch.setattr(d, "collect_story_wedge_signals", _collect, raising=False)
    monkeypatch.setattr(
        pipeline.wedge, "collect_story_wedge_signals", _collect, raising=False
    )


@pytest.fixture
def wedged_setup(tmp_path, monkeypatch, plan_dir):
    """plan_dir (manifest location) + a temp worktree root patched into both
    app.dashboard and pipeline.server, mirroring the existing dashboard-test
    convention of patching the path constants directly."""
    from pipeline import server as _srv

    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    monkeypatch.setattr(d, "WORKTREE_ROOT", worktree_root)
    monkeypatch.setattr(_srv, "WORKTREE_ROOT", worktree_root)
    return worktree_root


# --------------------------------------------------------------------------
# Wiring: the names get_plan needs actually exist in app.dashboard's namespace
# --------------------------------------------------------------------------


def test_dashboard_imports_wedge_names():
    """get_plan must import collect_story_wedge_signals + wedge_verdict from
    pipeline.wedge and WEDGE_STALE_ACTIVITY_SECONDS from pipeline.config."""
    assert d.collect_story_wedge_signals is pipeline.wedge.collect_story_wedge_signals
    assert d.wedge_verdict is pipeline.wedge.wedge_verdict
    assert d.WEDGE_STALE_ACTIVITY_SECONDS is pipeline.config.WEDGE_STALE_ACTIVITY_SECONDS


# --------------------------------------------------------------------------
# Wedged story: dead pid + stale activity -> wedge.wedged True
# --------------------------------------------------------------------------


def test_wedged_in_progress_story_gets_wedge_true(client, plan_dir, wedged_setup, monkeypatch):
    calls: list[str] = []
    _stub_collector(
        monkeypatch,
        {"S1": {"pid_alive": False, "activity_age_seconds": 999_999.0}},
        calls,
    )
    _write_manifest(
        plan_dir,
        PLAN,
        {"S1": {"summary": "wedge me", "status": "in_progress", "pid": 424242}},
    )

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    story = res.json()["stories"]["S1"]
    assert "wedge" in story, "in_progress story must carry a wedge key"
    wedge = story["wedge"]
    assert wedge["wedged"] is True
    assert isinstance(wedge["reasons"], list)
    assert "dead_pid" in wedge["reasons"] or "stale_activity" in wedge["reasons"]
    # measured carries the readings next to the verdict so a mis-thresholded
    # detector is diagnosable from its own output.
    assert wedge["measured"]["pid_alive"] is False
    assert wedge["measured"]["activity_age_seconds"] == 999_999.0
    assert calls == ["S1"]


def test_wedge_verdict_receives_config_threshold(client, plan_dir, wedged_setup, monkeypatch):
    """The threshold passed to wedge_verdict is WEDGE_STALE_ACTIVITY_SECONDS
    from pipeline.config, not a local literal."""
    seen = {}

    def _collect(plan_name, story_key, story):
        return {"pid_alive": False, "activity_age_seconds": None}

    real_verdict = pipeline.wedge.wedge_verdict

    def _spy_verdict(pid_alive, activity_age_seconds, stale_seconds):
        seen["stale_seconds"] = stale_seconds
        return real_verdict(pid_alive, activity_age_seconds, stale_seconds)

    monkeypatch.setattr(d, "collect_story_wedge_signals", _collect, raising=False)
    monkeypatch.setattr(d, "wedge_verdict", _spy_verdict, raising=False)
    _write_manifest(
        plan_dir, PLAN, {"S1": {"summary": "s", "status": "in_progress", "pid": 1}}
    )

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    assert seen["stale_seconds"] == pipeline.config.WEDGE_STALE_ACTIVITY_SECONDS
    assert res.json()["stories"]["S1"]["wedge"]["wedged"] is True  # dead pid alone wedges


def test_wedged_signals_derived_from_real_worktree_files(
    client, plan_dir, wedged_setup, monkeypatch
):
    """End-to-end shape check: a dead pid + an old agent.log mtime in a real
    temp worktree under the patched WORKTREE_ROOT produce a wedged verdict.
    The collector is stubbed with the readings those files would yield (the
    real collector is the prerequisite story's I/O surface), but the files
    exist so the wiring is exercised against a realistic worktree layout."""
    import os

    wt = wedged_setup / f"{PLAN}-S1"
    wt.mkdir()
    (wt / "agent.log").write_text("stale agent output\n")
    old = time.time() - 10_000
    os.utime(wt / "agent.log", (old, old))

    calls: list[str] = []
    _stub_collector(
        monkeypatch,
        {"S1": {"pid_alive": False, "activity_age_seconds": time.time() - old}},
        calls,
    )
    _write_manifest(
        plan_dir,
        PLAN,
        {
            "S1": {
                "summary": "stale attempt",
                "status": "in_progress",
                "pid": 424242,
                "worktree": str(wt),
            }
        },
    )

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    wedge = res.json()["stories"]["S1"]["wedge"]
    assert wedge["wedged"] is True
    assert "dead_pid" in wedge["reasons"]
    assert "stale_activity" in wedge["reasons"]


# --------------------------------------------------------------------------
# Healthy story: explicit False, not absence
# --------------------------------------------------------------------------


def test_healthy_in_progress_story_gets_explicit_wedged_false(
    client, plan_dir, wedged_setup, monkeypatch
):
    _stub_collector(
        monkeypatch, {"S1": {"pid_alive": True, "activity_age_seconds": 5.0}}, []
    )
    _write_manifest(
        plan_dir, PLAN, {"S1": {"summary": "fine", "status": "in_progress", "pid": 1}}
    )

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    wedge = res.json()["stories"]["S1"]["wedge"]
    assert "wedge" in res.json()["stories"]["S1"]
    assert wedge["wedged"] is False
    assert wedge["reasons"] == []
    assert wedge["measured"]["pid_alive"] is True
    assert wedge["measured"]["activity_age_seconds"] == 5.0


def test_dead_pid_with_agent_done_true_is_not_wedged_in_response(
    client, plan_dir, wedged_setup, monkeypatch
):
    """End-to-end: the collector reporting agent_done=True with a dead pid
    must yield an explicitly NOT-wedged story in the response (the finished
    agent is spared the dead_pid reason), and the measured reading travels
    with agent_done so the dashboard verdict is diagnosable from its own
    output."""
    calls: list[str] = []
    _stub_collector(
        monkeypatch,
        {
            "S1": {
                "pid_alive": False,
                "activity_age_seconds": None,
                "agent_done": True,
            }
        },
        calls,
    )
    _write_manifest(
        plan_dir,
        PLAN,
        {"S1": {"summary": "finished", "status": "in_progress", "pid": 999}},
    )

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    wedge = res.json()["stories"]["S1"]["wedge"]
    assert wedge["wedged"] is False
    assert wedge["reasons"] == []
    assert wedge["measured"]["agent_done"] is True
    assert wedge["measured"]["pid_alive"] is False


def test_healthy_story_at_exact_threshold_is_not_wedged(
    client, plan_dir, wedged_setup, monkeypatch
):
    """Boundary: age exactly equal to the threshold is NOT wedged (verdict is
    strictly greater-than)."""
    threshold = pipeline.config.WEDGE_STALE_ACTIVITY_SECONDS
    _stub_collector(
        monkeypatch,
        {"S1": {"pid_alive": True, "activity_age_seconds": float(threshold)}},
        [],
    )
    _write_manifest(
        plan_dir, PLAN, {"S1": {"summary": "edge", "status": "in_progress", "pid": 1}}
    )

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    assert res.json()["stories"]["S1"]["wedge"]["wedged"] is False


def test_story_with_no_pid_and_no_activity_signals_is_not_wedged(
    client, plan_dir, wedged_setup, monkeypatch
):
    """pid None + activity None (no pid field, no activity files) must
    short-circuit to a cheap not-wedged verdict, never a wedge reason."""
    _stub_collector(
        monkeypatch, {"S1": {"pid_alive": None, "activity_age_seconds": None}}, []
    )
    _write_manifest(plan_dir, PLAN, {"S1": {"summary": "bare", "status": "in_progress"}})

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    wedge = res.json()["stories"]["S1"]["wedge"]
    assert wedge["wedged"] is False
    assert wedge["reasons"] == []
    assert wedge["measured"]["pid_alive"] is None
    assert wedge["measured"]["activity_age_seconds"] is None


# --------------------------------------------------------------------------
# Bounded cost: only in_progress stories are probed
# --------------------------------------------------------------------------


def test_collector_not_called_for_todo_and_done_stories(
    client, plan_dir, wedged_setup, monkeypatch
):
    calls: list[str] = []
    _stub_collector(
        monkeypatch,
        {"IP": {"pid_alive": True, "activity_age_seconds": 1.0}},
        calls,
    )
    _write_manifest(
        plan_dir,
        PLAN,
        {
            "TODO": {"summary": "t", "status": "todo"},
            "IP": {"summary": "i", "status": "in_progress", "pid": 1},
            "DONE": {"summary": "d", "status": "done"},
            "BLOCKED": {"summary": "b", "status": "blocked"},
        },
    )

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    stories = res.json()["stories"]
    assert calls == ["IP"], f"only in_progress stories are probed, got {calls}"
    assert "wedge" not in stories["TODO"]
    assert "wedge" not in stories["DONE"]
    assert "wedge" not in stories["BLOCKED"]
    assert stories["IP"]["wedge"]["wedged"] is False


def test_todo_story_has_no_wedge_key_even_when_signals_would_wedge(
    client, plan_dir, wedged_setup, monkeypatch
):
    """A todo story with a dead pid on record is still not probed — no wedge
    key, collector never called for it."""
    calls: list[str] = []
    _stub_collector(monkeypatch, {}, calls)
    _write_manifest(
        plan_dir, PLAN, {"S1": {"summary": "t", "status": "todo", "pid": 999}}
    )

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    assert res.json()["stories"]["S1"].get("wedge") is None
    assert calls == []


# --------------------------------------------------------------------------
# Fail-open: collector raising must not 500
# --------------------------------------------------------------------------


def test_collector_raise_is_fail_open_per_story(client, plan_dir, wedged_setup, monkeypatch):
    def _boom(plan_name, story_key, story):
        raise RuntimeError("ps exploded")

    monkeypatch.setattr(d, "collect_story_wedge_signals", _boom, raising=False)
    monkeypatch.setattr(pipeline.wedge, "collect_story_wedge_signals", _boom, raising=False)
    _write_manifest(
        plan_dir,
        PLAN,
        {
            "BAD": {"summary": "boom", "status": "in_progress", "pid": 1},
            "OK": {"summary": "fine", "status": "todo"},
        },
    )

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200, (
        "wedge computation must never 500 /api/plans/{plan}"
    )
    stories = res.json()["stories"]
    assert "wedge" not in stories["BAD"]
    assert "wedge" not in stories["OK"]


def test_collector_raise_still_serves_other_fields(client, plan_dir, wedged_setup, monkeypatch):
    def _boom(plan_name, story_key, story):
        raise ValueError(f"no signals for {story_key}")

    monkeypatch.setattr(d, "collect_story_wedge_signals", _boom, raising=False)
    monkeypatch.setattr(pipeline.wedge, "collect_story_wedge_signals", _boom, raising=False)
    _write_manifest(
        plan_dir, PLAN, {"S1": {"summary": "keep me", "status": "in_progress", "pid": 7}}
    )

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    story = res.json()["stories"]["S1"]
    assert story["summary"] == "keep me"
    assert story["status"] == "in_progress"
    assert story["pid"] == 7
    assert "wedge" not in story


# --------------------------------------------------------------------------
# Never rewrite the story: manifest byte-identical + fields verbatim
# --------------------------------------------------------------------------


def test_manifest_is_byte_identical_after_request(client, plan_dir, wedged_setup, monkeypatch):
    _stub_collector(
        monkeypatch, {"S1": {"pid_alive": False, "activity_age_seconds": 99_999.0}}, []
    )
    _write_manifest(
        plan_dir,
        PLAN,
        {"S1": {"summary": "s", "status": "in_progress", "pid": 424242}},
    )
    manifest_path = plan_dir / f"{PLAN}.manifest.json"
    before = manifest_path.read_bytes()

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    assert manifest_path.read_bytes() == before, "get_plan must never rewrite the manifest"


def test_existing_story_fields_survive_decoration_verbatim(
    client, plan_dir, wedged_setup, monkeypatch
):
    _stub_collector(
        monkeypatch, {"S1": {"pid_alive": False, "activity_age_seconds": 99_999.0}}, []
    )
    original = {
        "summary": "verbatim",
        "status": "in_progress",
        "pid": 424242,
        "last_commit": "abc1234",
        "epic": "E1",
        "attempts": 2,
    }
    _write_manifest(plan_dir, PLAN, {"S1": dict(original)})

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    story = res.json()["stories"]["S1"]
    for key, value in original.items():
        assert story[key] == value, f"field {key!r} must survive decoration verbatim"
    # last_activity is the pre-existing decoration and must still be present.
    assert "last_activity" in story
    # The wedge decoration is additive, not a replacement of the story dict.
    assert set(original) <= set(story)


# --------------------------------------------------------------------------
# Malformed / boundary inputs must not break the endpoint
# --------------------------------------------------------------------------


def test_non_dict_story_is_passed_through_untouched(
    client, plan_dir, wedged_setup, monkeypatch
):
    calls: list[str] = []
    _stub_collector(monkeypatch, {}, calls)
    _write_manifest(plan_dir, PLAN, {"WEIRD": "not-a-dict", "S1": {"summary": "s", "status": "todo"}})

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    assert res.json()["stories"]["WEIRD"] == "not-a-dict"
    assert calls == [], "non-dict stories must not be probed"


def test_empty_stories_dict_returns_empty(client, plan_dir, wedged_setup, monkeypatch):
    calls: list[str] = []
    _stub_collector(monkeypatch, {}, calls)
    _write_manifest(plan_dir, PLAN, {})

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    assert res.json()["stories"] == {}
    assert calls == []


def test_in_progress_story_with_zero_age_and_live_pid_not_wedged(
    client, plan_dir, wedged_setup, monkeypatch
):
    """Boundary: zero age (activity right now) with a live pid is healthy."""
    _stub_collector(
        monkeypatch, {"S1": {"pid_alive": True, "activity_age_seconds": 0.0}}, []
    )
    _write_manifest(
        plan_dir, PLAN, {"S1": {"summary": "fresh", "status": "in_progress", "pid": 1}}
    )

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    wedge = res.json()["stories"]["S1"]["wedge"]
    assert wedge["wedged"] is False
    assert wedge["measured"]["activity_age_seconds"] == 0.0


def test_negative_age_is_fail_open_not_wedged(client, plan_dir, wedged_setup, monkeypatch):
    """Future mtime / clock skew yields a negative age — never a wedge reason."""
    _stub_collector(
        monkeypatch, {"S1": {"pid_alive": True, "activity_age_seconds": -30.0}}, []
    )
    _write_manifest(
        plan_dir, PLAN, {"S1": {"summary": "skew", "status": "in_progress", "pid": 1}}
    )

    res = client.get(f"/api/plans/{PLAN}")

    assert res.status_code == 200
    wedge = res.json()["stories"]["S1"]["wedge"]
    assert wedge["wedged"] is False
    assert wedge["reasons"] == []