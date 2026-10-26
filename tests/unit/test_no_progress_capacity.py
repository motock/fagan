"""UC-03: an agent that dies printing the backend's usage-cap banner is a
capacity wait, not a crash — it must not burn the rework or transport budget."""

import json

from pipeline import no_progress
from pipeline import server as p
from pipeline.no_progress import classify_no_progress, is_capacity_failure
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
)

BOOT = "[boot] model=x\n"
CAP_BANNER = "Claude usage limit reached. Your limit will reset at 3pm.\n"
TOOL_STEPS = "[step 1] bash: pytest -q\n[step 2] view_file: a.py\n"


def _setup(tmp_path, monkeypatch, log, **story_extra):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(log)
    story = {"summary": "s", "status": "tests_passed", "rework_attempts": 0, **story_extra}
    manifest = {"epics": {}, "stories": {"S1": story}}
    path = tmp_path / "plan.manifest.json"
    path.write_text(json.dumps(manifest))
    notes = []
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: notes.append((a, k)))
    return story, manifest, path, worktree, notes


def _run(story, worktree, manifest, path):
    return classify_no_progress("plan", "S1", story, str(worktree), manifest, path)


def _record_cooldown(monkeypatch):
    trips = []
    monkeypatch.setattr(no_progress.usage_cooldown, "trip", lambda *a, **k: trips.append((a, k)))
    return trips


def _record_announce(monkeypatch):
    calls = []
    monkeypatch.setattr(
        no_progress.usage_transitions, "announce", lambda *a, **k: calls.append((a, k))
    )
    return calls


def test_is_capacity_failure_matches_each_rate_limit_anchor():
    for banner in (
        "hit your session limit",
        "usage limit reached",
        "out_of_credits",
        "overageDisabledReason",
    ):
        assert is_capacity_failure(f"{BOOT}{banner}\n")


def test_is_capacity_failure_false_for_empty_log():
    assert not is_capacity_failure("")


def test_is_capacity_failure_false_for_other_error():
    assert not is_capacity_failure(BOOT + "[step 0] LLM call failed: bad json\n")


def test_is_capacity_failure_false_when_tool_steps_follow():
    assert not is_capacity_failure(BOOT + CAP_BANNER + TOOL_STEPS)


def test_is_capacity_failure_false_when_tool_steps_precede():
    assert not is_capacity_failure(BOOT + TOOL_STEPS + CAP_BANNER)


def test_is_capacity_failure_ignores_prior_segment_banner():
    assert not is_capacity_failure(BOOT + CAP_BANNER + BOOT + TOOL_STEPS)


def test_capacity_wait_does_not_charge_rework_or_transport(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, BOOT + CAP_BANNER)
    _record_cooldown(monkeypatch)
    _record_announce(monkeypatch)
    result = _run(story, wt, manifest, path)
    assert result == {"status": "changes_requested", "reason": "usage_capacity"}
    assert story["status"] == "changes_requested"
    assert story["rework_attempts"] == 0
    assert "transport_retries" not in story
    assert "dispatch_attempts" not in story
    assert story["capacity_waits"] == 1
    assert json.loads(path.read_text())["stories"]["S1"]["capacity_waits"] == 1


def test_capacity_wait_trips_cooldown_and_announces(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, BOOT + CAP_BANNER)
    trips = _record_cooldown(monkeypatch)
    announces = _record_announce(monkeypatch)
    _run(story, wt, manifest, path)
    assert len(trips) == 1
    assert len(announces) == 1
    assert announces[0][0][0] is True
    assert announces[0][1]["reason"] == "agent hit the usage cap"


def test_capacity_wait_notifies_once_with_event(tmp_path, monkeypatch):
    story, manifest, path, wt, notes = _setup(tmp_path, monkeypatch, BOOT + CAP_BANNER)
    _record_cooldown(monkeypatch)
    _record_announce(monkeypatch)
    _run(story, wt, manifest, path)
    assert len(notes) == 1
    assert notes[0][1]["event"] == "usage_capacity_hit"


def test_banner_after_tool_step_is_not_capacity(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, BOOT + TOOL_STEPS + CAP_BANNER)
    _record_cooldown(monkeypatch)
    _record_announce(monkeypatch)
    assert _run(story, wt, manifest, path) is None
    assert "capacity_waits" not in story


def test_no_banner_is_unchanged_behaviour(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, BOOT + TOOL_STEPS)
    _record_cooldown(monkeypatch)
    _record_announce(monkeypatch)
    assert _run(story, wt, manifest, path) is None
    assert "capacity_waits" not in story


def test_twentieth_wait_parks_with_accurate_reason(tmp_path, monkeypatch):
    story, manifest, path, wt, notes = _setup(
        tmp_path, monkeypatch, BOOT + CAP_BANNER, capacity_waits=19
    )
    _record_cooldown(monkeypatch)
    _record_announce(monkeypatch)
    result = _run(story, wt, manifest, path)
    assert result == {"status": "parked", "reason": "usage_capacity_exhausted"}
    assert story["status"] == "parked"
    assert story["capacity_waits"] == 20
    assert story["parked_reason"] == "usage cap kept interrupting this story: 20 waits"
    assert story["rework_attempts"] == 0
    assert notes[-1][1]["event"] == "story_parked"


def test_nineteenth_wait_still_redispatches(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(
        tmp_path, monkeypatch, BOOT + CAP_BANNER, capacity_waits=18
    )
    _record_cooldown(monkeypatch)
    _record_announce(monkeypatch)
    assert _run(story, wt, manifest, path) == {
        "status": "changes_requested",
        "reason": "usage_capacity",
    }
    assert story["capacity_waits"] == 19


def test_failing_cooldown_trip_does_not_stop_status_change(tmp_path, monkeypatch):
    story, manifest, path, wt, _ = _setup(tmp_path, monkeypatch, BOOT + CAP_BANNER)
    _record_announce(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("cooldown unavailable")

    monkeypatch.setattr(no_progress.usage_cooldown, "trip", _boom)
    result = _run(story, wt, manifest, path)
    assert result == {"status": "changes_requested", "reason": "usage_capacity"}
    assert story["status"] == "changes_requested"
    assert story["capacity_waits"] == 1
