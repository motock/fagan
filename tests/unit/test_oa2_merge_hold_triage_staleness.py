"""OA2-05 (G5): a stale merge-gate hold becomes a triage candidate.

The merge gate's park exclusion in ``triage_candidates`` is deliberate design
(see the docstring there): a story carrying a ``merge_park_evidence`` snapshot
is owned by the merge gate, not triage. But a hold that sits untouched longer
than the grace window dead-ends forever. These tests pin the rescue: once the
story's ``merge_parked_at`` timestamp is older than
``PIPELINE_TRIAGE_MERGE_HOLD_GRACE_SECONDS`` (default 86400s), the story is
handed to the triage ladder. Fail-secure: a missing, unparseable, or future
timestamp is never stale, so legacy parks keep today's exclusion exactly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

# Import pipeline.server FIRST: pipeline.triage -> build_detect -> server ->
# triage is a circular import, so importing pipeline.triage standalone raises
# ImportError. Importing the server module first breaks the cycle.
import pipeline.server  # noqa: F401  (import-order idiom: breaks the triage<->server cycle)
from pipeline import triage as triage_mod


def _parked_story(**extra):
    story = {
        "status": "parked",
        "parked_reason": "high risk held for human review",
        "merge_park_evidence": {"pr_checks": None},
    }
    story.update(extra)
    return story


def _candidates(story):
    return triage_mod.triage_candidates({"S1": story})


# ---------------------------------------------------------------------------
# 1/2: fresh vs stale hold
# ---------------------------------------------------------------------------


def test_fresh_hold_not_a_candidate():
    story = _parked_story(merge_parked_at=datetime.now(timezone.utc).isoformat())
    assert "S1" not in _candidates(story)


def test_stale_hold_is_a_candidate():
    stale = datetime.now(timezone.utc) - timedelta(seconds=86401)
    story = _parked_story(merge_parked_at=stale.isoformat())
    assert "S1" in _candidates(story)


# ---------------------------------------------------------------------------
# 3: legacy snapshot without a timestamp keeps today's exclusion
# ---------------------------------------------------------------------------


def test_legacy_snapshot_without_timestamp_not_a_candidate():
    story = _parked_story()
    assert "merge_parked_at" not in story
    assert "S1" not in _candidates(story)


# ---------------------------------------------------------------------------
# 4/5: fail-secure on unparseable and future timestamps
# ---------------------------------------------------------------------------


def test_unparseable_timestamp_not_a_candidate():
    story = _parked_story(merge_parked_at="not-a-date")
    assert "S1" not in _candidates(story)


def test_future_timestamp_not_a_candidate():
    future = datetime.now(timezone.utc) + timedelta(seconds=3600)
    story = _parked_story(merge_parked_at=future.isoformat())
    assert "S1" not in _candidates(story)


# ---------------------------------------------------------------------------
# 6/7: grace-window override and fallback
# ---------------------------------------------------------------------------


def test_grace_zero_lets_a_fresh_park_escape(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MERGE_HOLD_GRACE_SECONDS", "0")
    story = _parked_story(merge_parked_at=datetime.now(timezone.utc).isoformat())
    assert "S1" in _candidates(story)


def test_unparseable_grace_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("PIPELINE_TRIAGE_MERGE_HOLD_GRACE_SECONDS", "garbage")
    story = _parked_story(merge_parked_at=datetime.now(timezone.utc).isoformat())
    assert "S1" not in _candidates(story)


# ---------------------------------------------------------------------------
# 8: the merge-gate park writes the timestamp on the STORY, not the snapshot
# ---------------------------------------------------------------------------


def test_merge_park_writes_story_level_timestamp(monkeypatch, plan_dir):
    """The park branch of _adjudicate_merges stamps merge_parked_at on the story.

    Drives the REAL _adjudicate_merges over an on-disk manifest with the heavy
    seams stubbed (same shape as tests/unit/test_merge_park_readjudication.py).
    """
    from pipeline import advance as advance_mod
    from pipeline import server as server_mod

    story = {
        "key": "S1",
        "plan_name": "PLAN-1",
        "status": "pr_open",
        "pr_checks": {"ci": "pending"},
        "pr_url": "https://example.test/pr/1",
        "review_verdict": "APPROVE",
        "risk": "high",
        "worktree": "",
        "dependencies": [],
    }
    (plan_dir / "PLAN-1.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {"S1": story}}, indent=2)
    )
    # gated autonomy: a high-risk story parks without invoking the overlord.
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "gated", raising=False)
    monkeypatch.setattr(
        server_mod, "_notify_user", lambda *a, **k: None, raising=False
    )
    summary = {"parked": [], "notify": [], "failed": [], "merged": [], "ci_pending": []}
    advance_mod._adjudicate_merges("PLAN-1", summary)
    manifest = json.loads((plan_dir / "PLAN-1.manifest.json").read_text())
    parked = manifest["stories"]["S1"]
    assert parked["status"] == "parked"
    ts = parked["merge_parked_at"]
    assert isinstance(ts, str)
    datetime.fromisoformat(ts)  # raises if not ISO-8601
    assert set(parked["merge_park_evidence"]) == {"pr_checks"}


# ---------------------------------------------------------------------------
# 9: the merge ruling path pops the timestamp along with the snapshot
# ---------------------------------------------------------------------------


def test_merge_ruling_pops_timestamp(monkeypatch, plan_dir):
    """A fresh 'merge' ruling clears merge_parked_at with the snapshot."""
    from pipeline import advance as advance_mod
    from pipeline import ci as ci_mod
    from pipeline import server as server_mod

    story = {
        "key": "S1",
        "plan_name": "PLAN-1",
        "status": "parked",
        "parked_reason": "high risk held for human review",
        "merge_park_evidence": {"pr_checks": {"ci": "pending"}},
        "merge_parked_at": datetime.now(timezone.utc).isoformat(),
        "pr_checks": {"ci": "pass"},
        "pr_url": "https://example.test/pr/1",
        "review_verdict": "APPROVE",
        "worktree": "",
        "dependencies": [],
    }
    (plan_dir / "PLAN-1.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {"S1": story}}, indent=2)
    )
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "full", raising=False)
    monkeypatch.setattr(
        server_mod, "_merge_decision", lambda s, plan_name=None: {"action": "merge", "reason": "ruling"}
    )
    monkeypatch.setattr(
        ci_mod, "_ci_status_once", lambda branch, *, sha: {"ci": "pass"}
    )
    monkeypatch.setattr(
        advance_mod, "_ci_status_once", lambda branch, *, sha: {"ci": "pass"}, raising=False
    )
    monkeypatch.setattr(
        server_mod, "_notify_user", lambda *a, **k: None, raising=False
    )
    summary = {"parked": [], "notify": [], "failed": [], "merged": [], "ci_pending": []}
    advance_mod._adjudicate_merges("PLAN-1", summary)
    manifest = json.loads((plan_dir / "PLAN-1.manifest.json").read_text())
    merged = manifest["stories"]["S1"]
    assert merged["status"] == "pr_open"
    assert "merge_park_evidence" not in merged
    assert "merge_parked_at" not in merged


@pytest.mark.parametrize(
    "grace_env",
    ["-5", "0", "garbage"],
)
def test_grace_env_edge_values_fail_secure(grace_env, monkeypatch):
    """Negative grace falls back to the default; a fresh park stays excluded."""
    monkeypatch.setenv("PIPELINE_TRIAGE_MERGE_HOLD_GRACE_SECONDS", grace_env)
    story = _parked_story(merge_parked_at=datetime.now(timezone.utc).isoformat())
    if grace_env == "0":
        assert "S1" in _candidates(story)
    else:
        assert "S1" not in _candidates(story)