"""Tests for the ``event="story_parked"`` stamp on the triage park notification.

Background
----------
``pipeline/notification_outbox.py:outbox_sink`` selects records for e-mail
delivery using ONLY the structured field ``event["payload"]["event"]``.  The
message text is never matched, so a notification emitted without an ``event=``
kwarg can never be e-mailed no matter what it says.

``pipeline/triage.py:_park`` is the single site that raises the most important
alert of all -- "your story parked and needs a human" -- and it called
``_notify_user`` without ``event=``.  This story stamps that one call with the
bare string literal ``event="story_parked"`` (matching the existing stamped
sites in ``pipeline/advance.py``, which use bare literals such as
``event="dispatch_failed"``).

What is graded here
-------------------
These tests drive the REAL ``_park`` function (and the real ``execute_ruling``
entry points that reach it) with ``_notify_user`` monkeypatched on the module
under test, so they prove the *wiring* rather than the existence of a constant.
A source-text grep over ``pipeline/triage.py`` would pass even if the call were
unreachable or the kwarg landed on the wrong call, so no such test is used.

Coverage:

* positive -- the park path emits ``event="story_parked"``;
* reachability -- the stamp is present when ``_park`` is reached through the
  real ``execute_ruling`` entry points (unhandled action, exhausted escalation
  ladder), not only when ``_park`` is called directly;
* negative -- a non-park notification from the same module does NOT carry
  ``event="story_parked"`` (proves the right call was stamped);
* behaviour preservation -- ``status``/``parked_reason``/return value and the
  notification message text are unchanged;
* boundary -- empty reason, empty plan name/story key, empty story dict;
* the existing never-break contract -- a ``_notify_user`` that raises still
  does not break the enclosing function.

The implementation does not exist yet, so this file is expected to be RED
(failing assertions) until it lands.  No real backend is ever contacted.
"""

import inspect

import pytest

# ``pipeline.triage`` imports ``pipeline.build_detect``, which imports
# ``pipeline.server``, which imports ``run_triage_sweep`` back out of
# ``pipeline.triage``.  Importing ``pipeline.triage`` first therefore trips a
# pre-existing circular import (the same thing happens to
# tests/unit/test_triage_execute_deferred.py when it is run on its own).
# Importing ``pipeline.server`` first resolves the cycle, so this module can be
# run standalone as well as as part of the whole suite.
import pipeline.server  # noqa: F401  (imported for its side effect: breaks the cycle)
from pipeline import triage

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def notify_calls(monkeypatch):
    """Capture every ``_notify_user`` call made by the module under test.

    Returns a list of ``{"args": tuple, "kwargs": dict}`` records, in call
    order.  ``_notify_user`` is imported into ``pipeline.triage`` at module
    level, so patching it there is what the production code actually calls.
    """
    calls = []

    def fake_notify(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(triage, "_notify_user", fake_notify)
    return calls


def _messages(calls):
    """The second positional argument (the message) of each captured call."""
    return [call["args"][1] for call in calls]


# ---------------------------------------------------------------------------
# Positive: the park path is stamped
# ---------------------------------------------------------------------------


def test_park_notification_carries_story_parked_event(notify_calls):
    """``_park`` must emit ``event="story_parked"`` on its notification."""
    story = {"status": "failed"}

    result = triage._park("plan-x", "story-1", story, "the build failed twice")

    assert result == "park_for_human"
    assert len(notify_calls) == 1, f"expected exactly one notification, got {notify_calls!r}"
    call = notify_calls[0]
    assert call["kwargs"].get("event") == "story_parked"


def test_park_event_is_a_bare_string_literal(notify_calls):
    """The stamp is the plain string ``"story_parked"`` (no enum/constant)."""
    triage._park("plan-x", "story-1", {"status": "failed"}, "reason")

    event = notify_calls[0]["kwargs"].get("event")
    assert isinstance(event, str)
    assert event == "story_parked"


def test_park_event_is_passed_as_a_keyword_not_positionally(notify_calls):
    """``event`` is a kwarg; the two positional arguments are unchanged."""
    triage._park("plan-x", "story-1", {"status": "failed"}, "reason")

    call = notify_calls[0]
    assert len(call["args"]) == 2
    assert call["args"][0] == "plan-x"
    assert call["args"][1] == "story-1 triage: reason"
    assert "event" in call["kwargs"]


def test_park_adds_no_other_kwargs(notify_calls):
    """Severity/correlation-id kwargs are not introduced by this story."""
    triage._park("plan-x", "story-1", {"status": "failed"}, "reason")

    assert set(notify_calls[0]["kwargs"]) == {"event"}


def test_park_signature_is_unchanged():
    """The enclosing function is neither renamed nor reformatted."""
    assert callable(triage._park)
    params = list(inspect.signature(triage._park).parameters)
    assert params == ["plan_name", "story_key", "story", "reason"]


# ---------------------------------------------------------------------------
# Reachability: the stamp survives the real entry points
# ---------------------------------------------------------------------------


def test_park_via_execute_ruling_unhandled_action_is_stamped(notify_calls):
    """An unrecognized ruling falls through to ``_park`` and is stamped."""
    story = {"status": "failed"}
    ruling = {"action": "totally_unknown", "rationale": "no idea what to do"}

    result = triage.execute_ruling(
        "plan-x", "story-1", story, ruling, {"stories": {}}, None
    )

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert len(notify_calls) == 1
    assert notify_calls[0]["kwargs"].get("event") == "story_parked"


def test_park_via_exhausted_escalation_ladder_is_stamped(notify_calls, monkeypatch):
    """The ``escalate_model`` ladder-exhausted park is stamped too."""
    monkeypatch.setattr(triage, "_auto_escalation_enabled", lambda: False)
    story = {"status": "failed", "backend": "local", "model": "small-model"}
    ruling = {"action": "escalate_model", "rationale": "needs a bigger model"}

    result = triage.execute_ruling(
        "plan-x", "story-1", story, ruling, {"stories": {}}, None
    )

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert len(notify_calls) == 1
    assert notify_calls[0]["kwargs"].get("event") == "story_parked"


# ---------------------------------------------------------------------------
# Negative: the non-park notification is NOT stamped
# ---------------------------------------------------------------------------


def test_deferred_action_direct_notification_is_not_stamped(notify_calls):
    """The extra, non-park notification in ``execute_ruling`` stays unstamped.

    A deferred action (``split_story``) parks the story via ``_park`` *and*
    then sends a second, direct notification carrying the ruling rationale.
    Only the park notification may carry ``event="story_parked"``.
    """
    story = {"status": "failed"}
    ruling = {"action": "split_story", "rationale": "too big for one implementer"}

    result = triage.execute_ruling(
        "plan-x", "story-1", story, ruling, {"stories": {}}, None
    )

    assert result == "park_for_human"
    assert len(notify_calls) == 2, f"expected two notifications, got {notify_calls!r}"

    park_calls = [c for c in notify_calls if "not implemented yet" in c["args"][1]]
    other_calls = [c for c in notify_calls if "not implemented yet" not in c["args"][1]]

    assert len(park_calls) == 1
    assert park_calls[0]["kwargs"].get("event") == "story_parked"

    assert len(other_calls) == 1
    other = other_calls[0]
    assert other["kwargs"].get("event") != "story_parked"
    assert "event" not in other["kwargs"], (
        "only the park notification may be stamped with event=; "
        f"the direct notification got {other['kwargs']!r}"
    )
    # The direct notification's message text is untouched.
    assert "split_story" in other["args"][1]
    assert "too big for one implementer" in other["args"][1]
    # The direct notification's message text is untouched.
    assert "split_story" in other["args"][1]
    assert "too big for one implementer" in other["args"][1]


def test_dry_run_notification_is_not_stamped(notify_calls, monkeypatch):
    """The dry-run notification in ``_apply_ruling_for_mode`` stays unstamped.

    ``_apply_ruling_for_mode`` sends a "triage dry-run" notification and does
    NOT park the story, so it must not carry ``event="story_parked"``.
    """
    import pipeline.server as server_mod

    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "dry-run")
    story = {"status": "failed"}
    ruling = {"action": "escalate_model", "rationale": "needs a bigger model"}

    result = triage._apply_ruling_for_mode(
        "plan-x", "story-1", story, ruling, {"stories": {}}, None
    )

    assert result == "dry-run"
    assert story.get("status") == "failed", "dry-run must not park the story"
    assert len(notify_calls) == 1
    call = notify_calls[0]
    assert "dry-run" in call["args"][1]
    assert call["kwargs"].get("event") != "story_parked"
    assert "event" not in call["kwargs"], (
        "only the park notification may be stamped with event=; "
        f"the dry-run notification got {call['kwargs']!r}"
    )


# ---------------------------------------------------------------------------
# Behaviour preservation
# ---------------------------------------------------------------------------


def test_park_still_sets_status_and_parked_reason(notify_calls):
    """The stamp must not disturb the story mutation."""
    story = {"status": "failed", "parked_reason": "stale reason"}

    triage._park("plan-x", "story-1", story, "the tests failed three times")

    assert story["status"] == "parked"
    assert story["parked_reason"] == "the tests failed three times"


def test_park_message_text_is_unchanged(notify_calls):
    """The human-readable message keeps its exact original wording."""
    triage._park("plan-x", "story-1", {"status": "failed"}, "reason text")

    assert notify_calls[0]["args"][1] == "story-1 triage: reason text"


def test_park_returns_park_for_human_marker(notify_calls):
    """The scheduler marker returned by ``_park`` is unchanged."""
    assert triage._park("plan-x", "story-1", {}, "reason") == "park_for_human"


# ---------------------------------------------------------------------------
# Boundary values
# ---------------------------------------------------------------------------


def test_park_with_empty_reason_still_stamps_event(notify_calls):
    """An empty reason is still a park and is still stamped."""
    story = {}

    triage._park("plan-x", "story-1", story, "")

    assert notify_calls[0]["args"][1] == "story-1 triage: "
    assert notify_calls[0]["kwargs"].get("event") == "story_parked"
    assert story["status"] == "parked"
    assert story["parked_reason"] == ""


def test_park_with_empty_plan_name_and_story_key_still_stamps_event(notify_calls):
    """Empty identifiers do not change the stamp."""
    story = {}

    triage._park("", "", story, "r")

    assert notify_calls[0]["args"][0] == ""
    assert notify_calls[0]["args"][1] == " triage: r"
    assert notify_calls[0]["kwargs"].get("event") == "story_parked"
    assert story["status"] == "parked"


def test_park_with_empty_story_dict_still_stamps_event(notify_calls):
    """An empty story dict is mutated and the notification is still stamped."""
    story = {}

    result = triage._park("plan-x", "story-1", story, "reason")

    assert result == "park_for_human"
    assert story == {"status": "parked", "parked_reason": "reason"}
    assert notify_calls[0]["kwargs"].get("event") == "story_parked"


# ---------------------------------------------------------------------------
# The existing swallow / never-break contract still holds
# ---------------------------------------------------------------------------


def test_park_survives_a_raising_notify_user(monkeypatch):
    """A ``_notify_user`` that raises must not break the enclosing function."""

    def boom(*args, **kwargs):
        raise RuntimeError("smtp is down")

    monkeypatch.setattr(triage, "_notify_user", boom)
    story = {"status": "failed"}

    result = triage._park("plan-x", "story-1", story, "reason")

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story["parked_reason"] == "reason"


def test_park_survives_a_raising_notify_user_through_execute_ruling(monkeypatch):
    """The swallow contract holds on the real entry point as well."""

    def boom(*args, **kwargs):
        raise ValueError("outbox is unwritable")

    monkeypatch.setattr(triage, "_notify_user", boom)
    story = {"status": "failed"}
    ruling = {"action": "unhandled_action", "rationale": "whatever"}

    result = triage.execute_ruling(
        "plan-x", "story-1", story, ruling, {"stories": {}}, None
    )

    assert result == "park_for_human"
    assert story["status"] == "parked"
