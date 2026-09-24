"""A reviewer sending a story back counts as a rework cycle AND a dirty first pass.

Before this, a REQUEST_CHANGES verdict left no metric record at all: a story
bounced three times by review and then merged was reported as zero rework and
first-pass clean. ``review_story`` now notifies once with the structured event
``review_changes_requested`` every time it spends a rework attempt (the scope
gate's send-back flows through the same handler, so it emits it too). The
metrics count that event as one rework cycle and as a first-pass disqualifier.

``scope_gate_failed`` stays informational: counting it as well would count one
gate rejection twice, and leaving it out keeps historical sidecars unchanged.

Drives the REAL ``pipeline.server.review_story`` against a real git repo.
Fixture shape is cribbed from ``tests/unit/test_review_scope_gate.py``.
"""

import json
import subprocess
from pathlib import Path

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import story_metrics
from pipeline.notification_outbox import DEFAULT_OUTBOX_EVENTS

EVENT = "review_changes_requested"
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(tmp_path):
    """A repo on branch ``main`` with a base commit, then branch ``feature``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "pipeline").mkdir()
    (repo / "pipeline" / "foo.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-b", "feature")
    return repo


def _commit_change(repo, *, out_of_scope):
    (repo / "pipeline" / "foo.py").write_text("x = 2\n")
    if out_of_scope:
        (repo / "stray.py").write_text("y = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")


def _review(plan_dir, monkeypatch, repo, *, reviewer_output, **story_extra):
    """Run review_story with the LLM reviewer stubbed; return (notify kwargs list,
    the story as stored in the manifest afterwards)."""
    calls = []
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: reviewer_output)
    story = {
        "summary": "Add thing",
        "status": "tests_passed",
        "worktree": str(repo),
        "risk": "low",
        "files": ["pipeline/foo.py"],
        **story_extra,
    }
    (plan_dir / "pe.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {"S1": story}})
    )
    p.review_story("pe", "S1")
    saved = json.loads((plan_dir / "pe.manifest.json").read_text())
    return calls, saved["stories"]["S1"]


def _events(calls, name):
    return [c for c in calls if c.get("event") == name]


_BOUNCE = "Add a test.\nVERDICT: REQUEST_CHANGES"


def test_llm_reviewer_bounce_emits_one_event_with_the_story_key(plan_dir, monkeypatch):
    repo = _init_repo(plan_dir.parent)
    _commit_change(repo, out_of_scope=False)

    calls, _ = _review(plan_dir, monkeypatch, repo, reviewer_output=_BOUNCE)

    bounces = _events(calls, EVENT)
    assert len(bounces) == 1
    assert bounces[0]["story_key"] == "S1"
    assert "correlation_id" not in bounces[0]


def test_bounce_event_carries_correlation_id_and_attempt_when_the_story_has_one(
    plan_dir, monkeypatch
):
    repo = _init_repo(plan_dir.parent)
    _commit_change(repo, out_of_scope=False)

    calls, _ = _review(
        plan_dir, monkeypatch, repo, reviewer_output=_BOUNCE,
        correlation_id="c-1", dispatch_attempts=2,
    )

    bounce = _events(calls, EVENT)[0]
    assert bounce["correlation_id"] == "c-1"
    assert bounce["attempt"] == 2


def test_scope_gate_bounce_emits_both_events_once_each(plan_dir, monkeypatch):
    repo = _init_repo(plan_dir.parent)
    _commit_change(repo, out_of_scope=True)

    calls, _ = _review(plan_dir, monkeypatch, repo, reviewer_output=_BOUNCE)

    assert len(_events(calls, "scope_gate_failed")) == 1
    assert len(_events(calls, EVENT)) == 1


def test_inconclusive_review_emits_no_bounce_event_and_spends_no_attempt(
    plan_dir, monkeypatch
):
    repo = _init_repo(plan_dir.parent)
    _commit_change(repo, out_of_scope=False)

    calls, story = _review(
        plan_dir, monkeypatch, repo, reviewer_output="Looks plausible, no verdict given."
    )

    assert _events(calls, EVENT) == []
    assert story.get("rework_attempts", 0) == 0


def test_the_bounce_that_exhausts_the_rework_budget_still_emits_the_event(
    plan_dir, monkeypatch
):
    repo = _init_repo(plan_dir.parent)
    _commit_change(repo, out_of_scope=False)

    calls, story = _review(
        plan_dir, monkeypatch, repo, reviewer_output=_BOUNCE, rework_attempts=99
    )

    assert story["status"] == "parked"
    assert len(_events(calls, EVENT)) == 1
    assert len(_events(calls, "story_parked")) == 1


def _metrics(*events):
    records = [{"event": name, "story_key": "S1"} for name in events]
    return story_metrics.compute_story_metrics(records)["S1"]


def test_a_bounce_counts_one_rework_cycle_and_one_extra_cost():
    group = _metrics(EVENT, "story_merged")
    assert group["rework_cycles"] == 1
    assert group["cost"] == 2


def test_a_bounce_is_a_dirty_first_pass():
    group = _metrics(EVENT, "story_merged")
    assert group["disqualifying_events"] == 1
    assert group["first_pass_clean"] is False


def test_a_merge_with_no_bounce_is_still_first_pass_clean():
    group = _metrics("story_merged")
    assert group["rework_cycles"] == 0
    assert group["first_pass_clean"] is True


def test_scope_gate_failed_alone_is_informational_and_not_counted():
    group = _metrics("scope_gate_failed", "story_merged")
    assert group["rework_cycles"] == 0
    assert group["first_pass_clean"] is True


def test_a_scope_gate_bounce_is_counted_once_not_twice():
    group = _metrics("scope_gate_failed", EVENT, "story_merged")
    assert group["rework_cycles"] == 1
    assert group["cost"] == 2


def test_the_event_is_in_both_metric_sets():
    assert EVENT in story_metrics._REWORK_EVENTS
    assert EVENT in story_metrics._FIRST_PASS_DISQUALIFYING_EVENTS


def test_every_other_rework_event_stays_non_disqualifying():
    assert (story_metrics._REWORK_EVENTS - {EVENT}).isdisjoint(
        story_metrics._FIRST_PASS_DISQUALIFYING_EVENTS
    )


def test_the_event_is_never_emailed():
    assert EVENT not in DEFAULT_OUTBOX_EVENTS.split(",")


def test_reference_documents_the_event():
    assert f"`{EVENT}`" in (REPO_ROOT / "REFERENCE.md").read_text(encoding="utf-8")
