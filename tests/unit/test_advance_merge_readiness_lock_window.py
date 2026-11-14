"""The merge-readiness poll must not hold the plan lock while it waits.

Diagnosis (ts-11): ``advance_pipeline`` holds the per-plan flock for the whole
tick (service.py ``_store.transaction``), and ``_adjudicate_merges`` runs
``_await_merge_readiness`` inside it. That poll sleeps up to the readiness
budget (180 s by default), so ``approve_merge`` and ``mark_story_done`` - which
take the same flock fail-fast - returned "plan busy" for most of every tick.

The graded contract, driven through the REAL ``_adjudicate_merges`` loop with
the plan lock held exactly as ``advance_pipeline`` holds it:

* the lock is free while the poll sleeps (asserted via the lock's own API);
* a human approve during the poll acquires the lock and succeeds;
* a story merged by a human during the poll is not merged again by the tick;
* a poll result is applied only if the story is still pr_open for the same
  reviewed head sha;
* a human edit to another story during the poll survives the tick's write.
"""

# ruff: noqa: F811
from __future__ import annotations

import threading

import pytest

from pipeline import advance_merge, concurrency
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _write_manifest,
    plan_dir,
)
from tests.unit.test_advance_merge_merge_readiness import (
    KEY,
    PLAN,
    FakeForge,
    _harness,
    _load,
    _story,
    _summary,
)


def _from_other_thread(fn):
    """Run ``fn`` on a fresh thread: a second flock fd, like a human's command."""
    result = {}

    def _run():
        result["value"] = fn()

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join()
    return result["value"]


def _acquire_plan_lock_once():
    """What a command does before writing: one non-blocking attempt."""
    with concurrency._plan_lock_once(PLAN) as acquired:
        return acquired


class _HookClock:
    """``time`` stand-in whose first ``sleep`` runs a "human" action first."""

    def __init__(self, during_poll):
        self.now = 1000.0
        self.during_poll = during_poll
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if self.during_poll is not None:
            action, self.during_poll = self.during_poll, None
            action()
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def tick_under_lock(plan_dir, monkeypatch):
    """Run one merge-adjudication tick the way advance_pipeline holds the lock."""

    def _run(forge, during_poll):
        harness = _harness(monkeypatch, plan_dir, forge)
        monkeypatch.setattr(advance_merge, "time", _HookClock(during_poll))
        with concurrency._plan_lock(PLAN) as acquired:
            assert acquired, (
                "the tick must hold the plan lock, as advance_pipeline does"
            )
            advance_merge._adjudicate_merges(PLAN, _summary())
        return harness

    return _run


def _human_sets(plan_dir, key, **fields):
    """A human command: take the plan lock, write fields to one story."""

    def _write():
        with concurrency._plan_lock_once(PLAN) as acquired:
            if not acquired:
                return False
            manifest = p._store.records().get_manifest("default", PLAN)
            manifest["stories"][key].update(fields)
            p._store.records().put_manifest("default", PLAN, manifest)
            return True

    return _write


# ---------------------------------------------------------------------------
# The reproduction: a human command during the poll must not see "busy"
# ---------------------------------------------------------------------------
def test_human_approve_during_poll_acquires_the_plan_lock(
    plan_dir, monkeypatch, tick_under_lock
):
    _write_manifest(plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"))})
    seen = {}

    def _during_poll():
        seen["held_by_tick"] = PLAN in concurrency._held_plan_locks()
        seen["command_acquired"] = _from_other_thread(_acquire_plan_lock_once)

    tick_under_lock(FakeForge(["pending", "pending"]), _during_poll)

    assert seen["held_by_tick"] is False, (
        "the tick must not hold the plan lock while it waits for readiness"
    )
    assert seen["command_acquired"] is True, (
        "a human approve during the readiness poll must get the lock, not 'plan busy'"
    )


def test_human_approve_during_poll_succeeds_and_tick_does_not_remerge(
    plan_dir, monkeypatch, tick_under_lock
):
    _write_manifest(plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"))})
    approved = {}

    def _during_poll():
        approved["ok"] = _from_other_thread(
            _human_sets(plan_dir, KEY, status="done", merge_attempts=0)
        )

    harness = tick_under_lock(FakeForge(["pending", "ready"]), _during_poll)

    assert approved["ok"] is True, "the human approve must not be refused"
    assert harness.merged == [], (
        "the tick must not merge a story a human already merged during the poll"
    )
    assert _load(plan_dir)["status"] == "done"


# ---------------------------------------------------------------------------
# Re-validation: a story merged or changed by a human during the poll wins
# ---------------------------------------------------------------------------
def test_story_merged_by_human_during_poll_is_left_alone(
    plan_dir, monkeypatch, tick_under_lock
):
    _write_manifest(plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"))})

    def _during_poll():
        _from_other_thread(_human_sets(plan_dir, KEY, status="done"))

    harness = tick_under_lock(FakeForge(["pending"]), _during_poll)

    assert harness.merged == []
    story = _load(plan_dir)
    assert story["status"] == "done", "the human's merge must survive the tick"
    assert "merge_pending_reason" not in story, (
        "a poll result must not be written onto a story the human already moved on"
    )
    assert "merge_error" not in story


def test_poll_result_ignored_when_reviewed_head_sha_changed(
    plan_dir, monkeypatch, tick_under_lock
):
    _write_manifest(plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"))})

    def _during_poll():
        _from_other_thread(
            _human_sets(plan_dir, KEY, review_verdict_sha="new-head-sha")
        )

    # The first read is pending, so the poll sleeps and the human change lands
    # inside the window; the second read is ready for the OLD head.
    harness = tick_under_lock(FakeForge(["pending", "ready"]), _during_poll)

    assert harness.merged == [], (
        "a ready answer for the old head must not merge the new head"
    )
    story = _load(plan_dir)
    assert story["status"] == "pr_open"
    assert story["review_verdict_sha"] == "new-head-sha"
    assert "merge_pending_reason" not in story


def test_human_edit_to_another_story_during_poll_survives_the_tick_write(
    plan_dir, monkeypatch, tick_under_lock
):
    other = "S11"
    _write_manifest(
        plan_dir,
        PLAN,
        {
            KEY: _story(worktree=str(plan_dir / "wt")),
            other: _story(status="todo", summary="human edited while poll ran"),
        },
    )

    def _during_poll():
        _from_other_thread(
            _human_sets(plan_dir, other, summary="human edited while poll ran")
        )

    tick_under_lock(FakeForge(["pending"]), _during_poll)

    assert _load_key(plan_dir, other)["summary"] == "human edited while poll ran", (
        "the tick's final manifest write must not revert a concurrent human edit"
    )


def _load_key(plan_dir, key) -> dict:
    import json

    path = plan_dir / f"{PLAN}.manifest.json"
    return json.loads(path.read_text())["stories"][key]
