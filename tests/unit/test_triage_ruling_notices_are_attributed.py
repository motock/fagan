"""Triage's "<key> triage: <action> - <rationale>" notices carry an event and a story key.

The four notices (invalid SPLIT payload, split over the plan ceiling,
uncorroborated mark_done, and a deferred action) were emitted with no
``event`` and no ``story_key``. The local-success classifier falls back to
keyword-matching such records (``legacy_message``), and story_metrics drops
them into the uncorrelated bucket. Each now carries ``event="triage_ruling"``,
``story_key`` and, when the story has one, ``correlation_id``.
"""

import pytest

from pipeline import triage

RATIONALE = "the reason"


@pytest.fixture
def notices(monkeypatch):
    calls = []
    monkeypatch.setattr(triage, "_notify_user", lambda *a, **k: calls.append((a, k)))
    return calls


def _story(**over):
    story = {"status": "parked", "parked_reason": "stuck", "worktree": "", "correlation_id": "cid000000042"}
    story.update(over)
    return story


def _triage_notice(calls):
    matches = [(a, k) for a, k in calls if len(a) > 1 and " triage: " in a[1] and RATIONALE in a[1]]
    assert len(matches) == 1, calls
    return matches[0][1]


def test_an_invalid_split_payload_notice_is_attributed(notices):
    ruling = {"action": "split_story", "rationale": RATIONALE, "split": ["only one"]}

    triage.execute_ruling("plan-x", "S-1", _story(), ruling, {"stories": {}}, None)

    kwargs = _triage_notice(notices)
    assert kwargs["event"] == "triage_ruling"
    assert kwargs["story_key"] == "S-1"
    assert kwargs["correlation_id"] == "cid000000042"


def test_a_split_over_the_plan_ceiling_notice_is_attributed(notices):
    ruling = {"action": "split_story", "rationale": RATIONALE, "split": ["a", "b"]}
    manifest = {"stories": {}, "triage_created_stories": triage.TRIAGE_MAX_CREATED_STORIES}

    triage.execute_ruling("plan-x", "S-1", _story(), ruling, manifest, None)

    kwargs = _triage_notice(notices)
    assert kwargs["event"] == "triage_ruling"
    assert kwargs["story_key"] == "S-1"


def test_an_uncorroborated_mark_done_notice_is_attributed(notices):
    ruling = {"action": "mark_done", "rationale": RATIONALE}

    triage.execute_ruling("plan-x", "S-1", _story(), ruling, {"stories": {}}, None)

    kwargs = _triage_notice(notices)
    assert kwargs["event"] == "triage_ruling"
    assert kwargs["story_key"] == "S-1"


def test_a_deferred_action_notice_is_attributed(notices, monkeypatch):
    monkeypatch.setattr(triage, "DEFERRED_ACTIONS", frozenset({"future_action"}))
    ruling = {"action": "future_action", "rationale": RATIONALE}

    triage.execute_ruling("plan-x", "S-1", _story(), ruling, {"stories": {}}, None)

    kwargs = _triage_notice(notices)
    assert kwargs["event"] == "triage_ruling"
    assert kwargs["story_key"] == "S-1"


def test_a_story_without_a_correlation_id_omits_the_field(notices):
    ruling = {"action": "mark_done", "rationale": RATIONALE}
    story = _story()
    del story["correlation_id"]

    triage.execute_ruling("plan-x", "S-1", story, ruling, {"stories": {}}, None)

    assert "correlation_id" not in _triage_notice(notices)


def test_the_notice_text_is_unchanged(notices):
    ruling = {"action": "mark_done", "rationale": RATIONALE}

    triage.execute_ruling("plan-x", "S-1", _story(), ruling, {"stories": {}}, None)

    texts = [a[1] for a, _ in notices if len(a) > 1 and RATIONALE in a[1]]
    assert texts == [f"S-1 triage: mark_done – {RATIONALE}"]
