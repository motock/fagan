"""``park_for_human`` must not clobber an existing ``parked_reason``.

Background
----------
``pipeline/triage.py:execute_ruling`` had explicit branches for ``split_story``,
``mark_done``, ``patch_acceptance``, the ``DEFERRED_ACTIONS`` set and
``escalate_model`` -- but none for ``park_for_human``, even though that action
is a recognized member of ``TRIAGE_ACTIONS``.  A ``park_for_human`` ruling
therefore fell through to the "unhandled ruling action" fallback, which calls
``_park``.  ``_park`` unconditionally assigns ``story["parked_reason"]``, so a
ruling whose entire meaning is "a human must look at this, keep it parked"
silently destroyed the reason already on record.

That is load-bearing: ``pipeline/advance.py`` gates
``_readjudicate_parked_merge_hold`` on
``story.get("parked_reason") != "high risk held for human review"``.  Once
triage clobbered that string, the MERGEPARK-2 re-adjudication path became
permanently unreachable for that story.

What is graded here
-------------------
The REAL ``execute_ruling`` is driven with ``_notify_user`` monkeypatched on
the module under test, so these tests prove the wiring (branch reached, reason
preserved, exactly one park notification) rather than the existence of a
constant.

Coverage:

* positive -- an existing non-empty-string reason is preserved byte-for-byte
  and the story is parked;
* positive -- exactly one notification carrying ``event="story_parked"``;
* positive -- with no prior reason the ruling rationale is still carried;
* negative/boundary -- absent key, ``None``, ``""`` and the non-string falsy
  ``0`` all take the ``_park`` path without raising;
* guard -- an unrelated unknown action still reaches the fallthrough, so the
  new branch does not over-match.
"""

import pytest

# ``pipeline.triage`` imports ``pipeline.build_detect``, which imports
# ``pipeline.server``, which imports ``run_triage_sweep`` back out of
# ``pipeline.triage``.  Importing ``pipeline.triage`` first therefore trips a
# pre-existing circular import.  Importing ``pipeline.server`` first resolves
# the cycle, so this module can be run standalone as well as in the full suite.
import pipeline.server  # noqa: F401  (imported for its side effect: breaks the cycle)
from pipeline import triage

# The literal ``pipeline/advance.py`` gates its merge-hold re-adjudication on.
MERGE_HOLD_REASON = "high risk held for human review"

# A distinctive rationale so substring assertions cannot pass by accident.
RATIONALE = "distinctive-rationale-9f3c1a"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def notify_calls(monkeypatch):
    """Capture every ``_notify_user`` call made by the module under test.

    ``pipeline.triage`` imports ``_notify_user`` into its own namespace, so
    patching it there is what the production code actually calls.
    """
    calls = []

    def fake_notify(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(triage, "_notify_user", fake_notify)
    return calls


def _ruling(action="park_for_human", rationale=RATIONALE):
    return {"action": action, "rationale": rationale}


def _run(story, ruling=None):
    """Call the real ``execute_ruling`` with the production argument order."""
    return triage.execute_ruling(
        "plan-x", "story-1", story, ruling or _ruling(), {"stories": {}}, None
    )


def _park_events(calls):
    return [c for c in calls if c["kwargs"].get("event") == "story_parked"]


# ---------------------------------------------------------------------------
# Positive: an existing reason survives the park
# ---------------------------------------------------------------------------


def test_existing_reason_is_preserved_byte_for_byte(notify_calls):
    """A ``park_for_human`` ruling keeps the reason a human must see."""
    story = {"status": "in_progress", "parked_reason": MERGE_HOLD_REASON}

    result = _run(story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story["parked_reason"] == MERGE_HOLD_REASON


def test_preserved_reason_survives_a_second_ruling(notify_calls):
    """State persists across calls: a later ruling must not clobber either."""
    story = {"status": "in_progress", "parked_reason": MERGE_HOLD_REASON}

    _run(story, _ruling(rationale="needs human eyes"))
    result = _run(story, _ruling(rationale="second look"))

    assert result == "park_for_human"
    assert story["parked_reason"] == MERGE_HOLD_REASON


def test_preserving_park_emits_exactly_one_story_parked_notification(notify_calls):
    """The keep-branch still notifies the human -- exactly once."""
    story = {"status": "in_progress", "parked_reason": MERGE_HOLD_REASON}

    _run(story)

    assert len(notify_calls) == 1, f"expected one notification, got {notify_calls!r}"
    assert notify_calls[0]["kwargs"].get("event") == "story_parked"


def test_no_prior_reason_still_carries_the_rationale(notify_calls):
    """With nothing to preserve, the ruling rationale must not be dropped."""
    story = {"status": "in_progress"}

    result = _run(story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert RATIONALE in story["parked_reason"]
    assert len(_park_events(notify_calls)) == 1


# ---------------------------------------------------------------------------
# Negative / boundary: no usable prior reason -> the ``_park`` path
# ---------------------------------------------------------------------------


def test_absent_parked_reason_key_falls_to_park(notify_calls):
    story = {"status": "in_progress"}

    result = _run(story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert RATIONALE in story["parked_reason"]
    assert len(_park_events(notify_calls)) == 1


def test_none_parked_reason_falls_to_park(notify_calls):
    story = {"status": "in_progress", "parked_reason": None}

    result = _run(story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert RATIONALE in story["parked_reason"]
    assert len(_park_events(notify_calls)) == 1


def test_empty_string_parked_reason_falls_to_park(notify_calls):
    story = {"status": "in_progress", "parked_reason": ""}

    result = _run(story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert RATIONALE in story["parked_reason"]
    assert len(_park_events(notify_calls)) == 1


def test_non_string_falsy_parked_reason_is_treated_as_absent(notify_calls):
    """``0`` is falsy and not a string: it must not be "kept" as a reason."""
    story = {"status": "in_progress", "parked_reason": 0}

    result = _run(story)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert isinstance(story["parked_reason"], str)
    assert RATIONALE in story["parked_reason"]
    assert len(_park_events(notify_calls)) == 1


# ---------------------------------------------------------------------------
# Guard: the new branch must not over-match
# ---------------------------------------------------------------------------


def test_unrelated_unknown_action_still_reaches_the_fallthrough(notify_calls):
    """Only ``action == "park_for_human"`` takes the new branch."""
    story = {"status": "in_progress", "parked_reason": MERGE_HOLD_REASON}

    result = _run(story, _ruling(action="frobnicate"))

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert "frobnicate" in story["parked_reason"]
    assert len(_park_events(notify_calls)) == 1
