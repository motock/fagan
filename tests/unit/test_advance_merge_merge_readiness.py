"""An UNCOMPUTED mergeability must never cost a story a merge attempt.

Right after a push GitHub reports ``mergeable: UNKNOWN`` while it computes, and
``gh pr merge`` refuses instead of waiting. The merge gate used to treat that
refusal exactly like a conflict: increment ``merge_attempts`` and, at
``MERGE_MAX_ATTEMPTS``, fail the story. On 2026-11-08 that failed PR #1618
(c1bfcfbf) after four attempts and PR #1621 (e0163dbc) after three - both were
CLEAN/MERGEABLE with all four required checks SUCCESS and both merged first try
by hand.

The graded contract (driven through the REAL
``pipeline.advance_merge._adjudicate_merges`` loop, against the manifest on
disk):

* pending -> ready   : exactly one merge, ZERO attempts consumed, status done;
* pending forever    : no merge, attempts unchanged, still pr_open, a
  ``merge_pending_reason`` recorded AND persisted;
* blocked (conflict) : today's path - one attempt consumed per tick, failed at
  ``MERGE_MAX_ATTEMPTS`` with ``merge_error`` set;
* unreadable read    : treated as pending (fail closed, never "ready");
* ready + gh refusal : the exception fallback still spends an attempt and the
  ``merge_error`` carries gh's stderr.
"""

# ruff: noqa: F811
from __future__ import annotations

import json

import pytest

import pipeline.advance as adv
from pipeline import advance_merge
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _write_manifest,
    plan_dir,
)

PLAN = "merge-readiness-plan"
KEY = "S10"
BRANCH = "agent/s10-2"


class FakeClock:
    """``time`` stand-in: ``sleep`` advances ``monotonic`` instead of waiting."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeForge:
    """Scripted ``merge_readiness``; the last value repeats once exhausted."""

    def __init__(self, states, *, raises=None):
        self.states = list(states)
        self.raises = raises
        self.reads = 0

    def merge_readiness(self, worktree, branch):
        self.reads += 1
        if self.raises is not None:
            raise self.raises
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]


class Harness:
    def __init__(self, clock, merged, notified):
        self.clock = clock
        self.merged = merged
        self.notified = notified


def _story(**over):
    story = {
        "summary": "land the merge-readiness fix",
        "status": "pr_open",
        "worktree": "",
        "pr_url": "https://example.test/pr/1618",
        "review_verdict": "APPROVE",
        "review_verdict_sha": "head-sha",
        "risk": "low",
        "merge_attempts": 1,
        "dependencies": [],
    }
    story.update(over)
    return story


def _harness(monkeypatch, tmp_path, forge, *, merge_raises=None):
    """Wire every boundary the adjudication loop touches; return the harness."""
    from pipeline import review_merge_binding

    clock = FakeClock()
    merged: list[str] = []
    notified: list[tuple] = []

    # A story worktree is a real git worktree in production; the readiness
    # gate only interrogates one that exists.
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    (wt / ".git").mkdir(exist_ok=True)

    monkeypatch.setattr(p, "PLAN_DIR", tmp_path, raising=False)
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated", raising=False)
    monkeypatch.setattr(p, "PIPELINE_RISK_THRESHOLD", "low", raising=False)

    # The loop's own gates, neutralised.
    monkeypatch.setattr(
        advance_merge, "_readjudicate_parked_merge_hold", lambda *a, **k: None
    )
    monkeypatch.setattr(
        advance_merge, "_merge_reverify_gate", lambda *a, **k: ("", False)
    )
    monkeypatch.setattr(adv, "_rebase_and_push_for_merge",
                        lambda *a, **k: ("", "pushed-sha"))
    monkeypatch.setattr(
        adv, "_merge_gate_ci_status", lambda *a, **k: {"state": "success", "error": ""}
    )
    monkeypatch.setattr(
        review_merge_binding, "branch_head_sha", lambda branch: "head-sha"
    )
    monkeypatch.setattr(review_merge_binding, "check", lambda story, head: {"ok": True})
    monkeypatch.setattr(
        review_merge_binding,
        "deny",
        lambda *a, **k: pytest.fail("the review binding must not deny here"),
    )

    # The merge itself.
    def _merge_pr(worktree, story_key):
        if merge_raises is not None:
            raise merge_raises
        merged.append(story_key)
        return "merged"

    monkeypatch.setattr(adv, "_merge_pr", _merge_pr)
    monkeypatch.setattr(
        adv, "_notify_user", lambda *a, **k: notified.append((a, k))
    )
    monkeypatch.setattr(adv, "_default_branch", lambda: "master")
    monkeypatch.setattr(adv, "_mcp_self_source_touched", lambda *a, **k: "")
    monkeypatch.setattr(adv, "_mark_plane_done", lambda *a, **k: None)
    monkeypatch.setattr(adv, "_maybe_record_retro", lambda *a, **k: None)

    # The readiness seam: the forge is a double, the clock is fake.
    monkeypatch.setattr(
        advance_merge, "_merge_readiness_forge", lambda: forge
    )
    monkeypatch.setattr(advance_merge, "time", clock)
    monkeypatch.setenv("PIPELINE_MERGE_READINESS_TIMEOUT", "180")

    return Harness(clock, merged, notified)


def _load(tmp_path) -> dict:
    path = tmp_path / f"{PLAN}.manifest.json"
    return json.loads(path.read_text())["stories"][KEY]


def _summary() -> dict:
    return {"parked": [], "notify": [], "failed": [], "merged": [],
            "ci_pending": []}


# ---------------------------------------------------------------------------
# pending -> ready: ONE merge, ZERO attempts consumed
# ---------------------------------------------------------------------------
def test_pending_then_ready_merges_once_and_spends_no_attempt(
    plan_dir, monkeypatch
):
    forge = FakeForge(["pending", "pending", "ready"])
    harness = _harness(monkeypatch, plan_dir, forge)
    _write_manifest(plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"))})

    advance_merge._adjudicate_merges(PLAN, _summary())

    assert harness.merged == [KEY], (
        f"the PR must be merged exactly once once GitHub says ready; got "
        f"{harness.merged!r}"
    )
    assert forge.reads == 3, "the gate must poll until it gets an answer"
    story = _load(plan_dir)
    assert story["status"] == "done"
    assert "merge_attempts" not in story, (
        "a pending wait must not consume a merge attempt: the story was merged "
        "with merge_attempts still at 1"
    )
    assert "merge_pending_reason" not in story, (
        "the stale pending reason must not outlive the successful merge"
    )


def test_pending_then_ready_sleeps_with_bounded_backoff(plan_dir, monkeypatch):
    forge = FakeForge(["pending", "pending", "pending", "ready"])
    harness = _harness(monkeypatch, plan_dir, forge)
    _write_manifest(plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"))})

    advance_merge._adjudicate_merges(PLAN, _summary())

    assert harness.clock.sleeps == [5.0, 10.0, 20.0], harness.clock.sleeps


# ---------------------------------------------------------------------------
# pending forever: no merge, no attempt, still pr_open, reason persisted
# ---------------------------------------------------------------------------
def test_pending_forever_never_merges_and_never_spends_an_attempt(
    plan_dir, monkeypatch
):
    forge = FakeForge(["pending"])
    harness = _harness(monkeypatch, plan_dir, forge)
    _write_manifest(plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"))})

    advance_merge._adjudicate_merges(PLAN, _summary())

    assert harness.merged == [], (
        "a mergeability GitHub has not computed must never reach `gh pr merge`"
    )
    story = _load(plan_dir)
    assert story["merge_attempts"] == 1, (
        f"a pending wait must NOT increment merge_attempts, got "
        f"{story.get('merge_attempts')!r} - incrementing here is the bug that "
        f"failed PR #1618/#1621"
    )
    assert story["status"] == "pr_open", "the next tick must retry"
    assert story.get("merge_pending_reason"), (
        "the story must record WHY nothing merged this tick"
    )
    assert "180" in story["merge_pending_reason"], story["merge_pending_reason"]
    assert "merge_error" not in story, "pending is not a failure"


def test_pending_forever_stays_within_the_readiness_budget(plan_dir, monkeypatch):
    forge = FakeForge(["pending"])
    harness = _harness(monkeypatch, plan_dir, forge)
    _write_manifest(plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"))})

    advance_merge._adjudicate_merges(PLAN, _summary())

    assert sum(harness.clock.sleeps) == pytest.approx(180.0), harness.clock.sleeps
    assert harness.clock.sleeps == [5.0, 10.0, 20.0, 30.0, 30.0, 30.0, 30.0, 25.0], (
        harness.clock.sleeps
    )
    assert forge.reads == 9, f"expected 9 reads over 180s, got {forge.reads}"


# ---------------------------------------------------------------------------
# blocked: today's path, one attempt per tick, failed at the cap
# ---------------------------------------------------------------------------
def test_blocked_consumes_one_attempt_and_stays_pr_open(plan_dir, monkeypatch):
    forge = FakeForge(["blocked"])
    _harness(monkeypatch, plan_dir, forge, merge_raises=RuntimeError(
        "gh pr merge failed (exit 1): X Pull request #1618 is not mergeable\n"
        "cmd: gh pr merge agent/s10-2 --squash"
    ))
    _write_manifest(
        plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"), merge_attempts=1)}
    )

    advance_merge._adjudicate_merges(PLAN, _summary())

    story = _load(plan_dir)
    assert story["merge_attempts"] == 2, (
        "a DEFINITIVE refusal (conflict) must keep consuming an attempt"
    )
    assert story["status"] == "pr_open"
    assert forge.reads == 1, (
        "a blocked reading is final for this tick: no polling"
    )


def test_blocked_at_the_cap_fails_the_story_with_the_stderr_cause(
    plan_dir, monkeypatch
):
    forge = FakeForge(["blocked"])
    _harness(monkeypatch, plan_dir, forge, merge_raises=RuntimeError(
        "gh pr merge failed (exit 1): X Pull request #1618 is not mergeable: "
        "the merge commit cannot be cleanly created.\n"
        "cmd: gh pr merge agent/s10-2 --squash"
    ))
    _write_manifest(
        plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"), merge_attempts=2)}
    )

    advance_merge._adjudicate_merges(PLAN, _summary())

    story = _load(plan_dir)
    assert story["merge_attempts"] == 3
    assert story["status"] == "failed"
    assert "not mergeable" in story["merge_error"], (
        "the failure must carry gh's stderr, not just the argv: "
        f"{story.get('merge_error')!r}"
    )
    assert "gh pr merge" in story["merge_error"]


# ---------------------------------------------------------------------------
# Unreadable readiness fails CLOSED (pending), never open (ready)
# ---------------------------------------------------------------------------
def test_oserror_on_every_read_is_pending_not_ready(plan_dir, monkeypatch):
    forge = FakeForge(["ready"], raises=OSError("gh: not found"))
    harness = _harness(monkeypatch, plan_dir, forge)
    _write_manifest(plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"))})

    advance_merge._adjudicate_merges(PLAN, _summary())

    assert harness.merged == [], (
        "an unreadable readiness answer must never authorise a merge"
    )
    story = _load(plan_dir)
    assert story["merge_attempts"] == 1
    assert story["status"] == "pr_open"
    assert story.get("merge_pending_reason")


def test_forge_without_the_verb_keeps_todays_behaviour(plan_dir, monkeypatch):
    """A forge predating the verb must not park the story on a poll."""

    class _Verbless:
        pass

    harness = _harness(monkeypatch, plan_dir, _Verbless())
    _write_manifest(plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"))})

    advance_merge._adjudicate_merges(PLAN, _summary())

    assert harness.merged == [KEY]
    assert _load(plan_dir)["status"] == "done"


# ---------------------------------------------------------------------------
# ready then a gh refusal: today's exception fallback, with the cause
# ---------------------------------------------------------------------------
def test_ready_then_refusal_still_spends_an_attempt(plan_dir, monkeypatch):
    forge = FakeForge(["ready"])
    harness = _harness(
        monkeypatch,
        plan_dir,
        forge,
        merge_raises=RuntimeError(
            "gh pr merge failed (exit 1): base branch was modified\n"
            "cmd: gh pr merge agent/s10-2 --squash"
        ),
    )
    _write_manifest(
        plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"), merge_attempts=1)}
    )

    advance_merge._adjudicate_merges(PLAN, _summary())

    story = _load(plan_dir)
    assert story["merge_attempts"] == 2, (
        "a refusal AFTER a ready reading is a real merge failure: it must "
        "still count an attempt"
    )
    assert story["status"] == "pr_open"
    assert harness.merged == []


# ---------------------------------------------------------------------------
# The budget knob itself
# ---------------------------------------------------------------------------
def test_timeout_is_read_from_the_environment_each_call(monkeypatch):
    monkeypatch.setenv("PIPELINE_MERGE_READINESS_TIMEOUT", "7")
    assert advance_merge._merge_readiness_timeout() == 7.0
    monkeypatch.setenv("PIPELINE_MERGE_READINESS_TIMEOUT", "nonsense")
    assert advance_merge._merge_readiness_timeout() == 180.0
    monkeypatch.setenv("PIPELINE_MERGE_READINESS_TIMEOUT", "-5")
    assert advance_merge._merge_readiness_timeout() == 0.0


def test_pending_with_a_zero_budget_does_not_sleep(plan_dir, monkeypatch):
    forge = FakeForge(["pending"])
    harness = _harness(monkeypatch, plan_dir, forge)
    monkeypatch.setenv("PIPELINE_MERGE_READINESS_TIMEOUT", "0")
    _write_manifest(plan_dir, PLAN, {KEY: _story(worktree=str(plan_dir / "wt"))})

    advance_merge._adjudicate_merges(PLAN, _summary())

    assert harness.clock.sleeps == []
    assert harness.merged == []
    assert _load(plan_dir)["merge_attempts"] == 1
