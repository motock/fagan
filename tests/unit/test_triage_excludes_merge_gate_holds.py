"""TRIFID-2: a story parked by the merge gate is never a triage candidate.

A high-risk merge hold is ``status == "parked"``, so before this fix it was a
triage candidate on EVERY tick. The per-tick budget / attempt-cap path inside
``run_triage_sweep`` then called ``_park`` with a cap reason, and ``_park``
unconditionally overwrites ``parked_reason`` (pinned by
``tests/unit/test_triage_cap_silent_skip.py``), destroying the merge gate's
hold reason. ``pipeline/advance.py`` gates
``_readjudicate_parked_merge_hold`` on ``parked_reason == _MERGE_HOLD_REASON``,
so the hold became permanently unreachable.

The fix keys the exclusion on the immutable ``merge_park_evidence`` snapshot
(written only by the merge gate) rather than on the mutable ``parked_reason``
label, which the bug itself corrupted.

These tests call ``triage.triage_candidates`` directly: it is a pure function
over a dict of story dicts.
"""

from __future__ import annotations

import pytest

# triage is a circular import, so importing pipeline.triage standalone raises
# ImportError; importing pipeline.server first breaks the cycle (same as
# tests/unit/test_triage_cap_silent_skip.py).
import pipeline.server
from pipeline import triage
from pipeline.config import STEP_CAP_FALLBACK_THRESHOLD

# The exact corrupted label observed on WAP-7 / WAP-9 after the clobber.
CORRUPTED_REASON = "triage attempts (2) at or above cap (2)"
MERGE_HOLD_REASON = "high risk held for human review"


def _parked(**overrides) -> dict:
    story = {
        "status": "parked",
        "parked_reason": MERGE_HOLD_REASON,
    }
    story.update(overrides)
    return story


# ---------------------------------------------------------------------------
# Positive: a merge-gate-owned park is excluded
# ---------------------------------------------------------------------------


def test_parked_story_with_null_pr_checks_snapshot_is_excluded():
    stories = {"WAP-1": _parked(merge_park_evidence={"pr_checks": None})}

    assert triage.triage_candidates(stories) == []


def test_parked_story_with_passing_pr_checks_snapshot_is_excluded():
    stories = {
        "WAP-1": _parked(
            merge_park_evidence={"pr_checks": {"state": "pass", "error": ""}}
        )
    }

    assert triage.triage_candidates(stories) == []


def test_snapshot_excludes_even_when_parked_reason_was_clobbered():
    """The exact regression: the label is a cap message, the snapshot remains."""
    stories = {
        "WAP-9": _parked(
            parked_reason=CORRUPTED_REASON,
            merge_park_evidence={"pr_checks": None},
        )
    }

    assert triage.triage_candidates(stories) == []


def test_failed_story_with_snapshot_is_excluded():
    stories = {
        "WAP-7": {
            "status": "failed",
            "parked_reason": CORRUPTED_REASON,
            "merge_park_evidence": {"pr_checks": {"state": "pending"}},
        }
    }

    assert triage.triage_candidates(stories) == []


def test_excluded_story_does_not_hide_other_candidates():
    stories = {
        "WAP-9": _parked(merge_park_evidence={"pr_checks": None}),
        "WAP-2": _parked(),
    }

    assert triage.triage_candidates(stories) == ["WAP-2"]


# ---------------------------------------------------------------------------
# Negative / boundary: everything else keeps today's behaviour
# ---------------------------------------------------------------------------


def test_parked_story_without_snapshot_is_still_a_candidate():
    stories = {"WAP-2": _parked()}

    assert triage.triage_candidates(stories) == ["WAP-2"]


def test_snapshot_explicitly_none_is_still_a_candidate():
    stories = {"WAP-2": _parked(merge_park_evidence=None)}

    assert triage.triage_candidates(stories) == ["WAP-2"]


@pytest.mark.parametrize("evidence", ["yes", [], 0, True])
def test_non_dict_snapshot_is_still_a_candidate_and_never_raises(evidence):
    stories = {"WAP-2": _parked(merge_park_evidence=evidence)}

    assert triage.triage_candidates(stories) == ["WAP-2"]


def test_empty_dict_snapshot_is_still_a_candidate():
    """``pr_checks`` is the marker key; an empty dict carries no ruling."""
    stories = {"WAP-2": _parked(merge_park_evidence={})}

    assert triage.triage_candidates(stories) == ["WAP-2"]


def test_dict_snapshot_without_pr_checks_key_is_still_a_candidate():
    stories = {"WAP-2": _parked(merge_park_evidence={"other": 1})}

    assert triage.triage_candidates(stories) == ["WAP-2"]


def test_step_cap_streak_story_with_snapshot_is_still_a_candidate():
    """The streak rule is checked after the parked/failed branch: no collateral."""
    stories = {
        "WAP-3": {
            "status": "todo",
            "step_cap_streak": STEP_CAP_FALLBACK_THRESHOLD,
            "merge_park_evidence": {"pr_checks": None},
        }
    }

    assert triage.triage_candidates(stories) == ["WAP-3"]


def test_step_cap_streak_story_without_snapshot_is_still_a_candidate():
    stories = {"WAP-3": {"status": "interrupted", "step_cap_streak": STEP_CAP_FALLBACK_THRESHOLD}}

    assert triage.triage_candidates(stories) == ["WAP-3"]


def test_story_missing_status_never_raises():
    stories = {"WAP-4": {"merge_park_evidence": {"pr_checks": None}}}

    assert triage.triage_candidates(stories) == []


def test_result_is_sorted():
    stories = {
        "b": _parked(),
        "a": {"status": "failed"},
        "c": {"status": "todo", "step_cap_streak": STEP_CAP_FALLBACK_THRESHOLD},
    }

    assert triage.triage_candidates(stories) == ["a", "b", "c"]


def test_empty_stories_returns_empty_list():
    assert triage.triage_candidates({}) == []
