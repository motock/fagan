"""LD90: the review-escalation notice must carry ``event`` and ``story_key``.

``pipeline.escalation._escalate_review_to_claude`` emits the pipeline's most
common escalation notice.  ``pipeline.story_metrics`` only counts a record as
an escalation when ``record["event"]`` is in ``{"escalated",
"model_fallback"}``, and it groups a record by ``correlation_id`` first, then
``story_key``, then ``"<uncorrelated>"``.  Without the two kwargs the notice is
invisible to metrics and, for a story with no ``correlation_id``, lands in the
``"<uncorrelated>"`` group.

These tests pin the stamping behavior only.  The message text and the
``_cid_kwargs`` construction (a story with no ``correlation_id`` must pass NO
``correlation_id`` kwarg at all - absent, not null) are unchanged by this
story and are pinned here as invariants.
"""

from __future__ import annotations

import pytest

import pipeline.escalation as esc
from pipeline import story_metrics


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Record every ``_notify_user`` call as ``(message, kwargs)``.

    ``_escalation_target`` is pinned to the default ``("claude", None)`` so the
    message label is deterministic and no registry/env lookup runs.
    """
    calls: list[tuple[str, dict]] = []

    def _fake_notify(plan_name, message, **kwargs):
        calls.append((message, kwargs))

    monkeypatch.setattr(esc, "_notify_user", _fake_notify)
    monkeypatch.setattr(esc, "_escalation_target", lambda: ("claude", None))
    return calls


def test_escalation_notice_stamps_event_and_story_key(monkeypatch):
    """A correlated story's notice carries event/story_key/correlation_id."""
    calls = _capture(monkeypatch)
    story = {"status": "tests_passed", "correlation_id": "cid-1"}

    esc._escalate_review_to_claude(
        story, "S1", "planA", "rework budget exhausted after 3 review cycles"
    )

    assert len(calls) == 1
    _message, kwargs = calls[0]
    assert kwargs["event"] == "escalated"
    assert kwargs["story_key"] == "S1"
    assert kwargs["correlation_id"] == "cid-1"


def test_escalation_notice_without_correlation_id_omits_the_kwarg(monkeypatch):
    """Legacy story: event/story_key stamped, correlation_id still absent."""
    calls = _capture(monkeypatch)
    story = {"status": "tests_passed"}

    esc._escalate_review_to_claude(
        story, "S1", "planA", "rework budget exhausted after 3 review cycles"
    )

    assert len(calls) == 1
    _message, kwargs = calls[0]
    assert kwargs["event"] == "escalated"
    assert kwargs["story_key"] == "S1"
    assert "correlation_id" not in kwargs


def test_escalation_notice_message_text_unchanged(monkeypatch):
    """The human-readable notice keeps its exact wording."""
    calls = _capture(monkeypatch)
    story = {"status": "tests_passed", "correlation_id": "cid-1"}

    esc._escalate_review_to_claude(
        story, "S1", "planA", "rework budget exhausted after 3 review cycles"
    )

    assert len(calls) == 1
    message, _kwargs = calls[0]
    assert message.startswith("S1 escalating to Claude (")
    assert "retrying the same worktree with a fresh budget." in message
    assert "rework budget exhausted after 3 review cycles" in message


def test_legacy_escalation_notice_is_counted_under_the_story(monkeypatch):
    """The stamped record is grouped under the story and counted as one escalation."""
    calls = _capture(monkeypatch)
    story = {"status": "tests_passed"}

    esc._escalate_review_to_claude(
        story, "S1", "planA", "rework budget exhausted after 3 review cycles"
    )

    assert len(calls) == 1
    message, kwargs = calls[0]
    record = {"ts": "2026-01-01T00:00:00+00:00", "message": message, **kwargs}

    metrics = story_metrics.compute_story_metrics([record])

    assert "S1" in metrics
    assert metrics["S1"]["escalations"] == 1
    assert story_metrics.UNCORRELATED_KEY not in metrics
