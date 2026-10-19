"""LEG-1: untyped (event-less) sidecar messages map to canonical reason names.

The classifier used to run one broad regex over every event-less message, so
the harness's own escalation notice (which contains "escalat") double-counted
an escalation as ``legacy_message``, and a benign mention of a topic word such
as "triage" marked an otherwise clean story non-clean.  These tests pin the
precise mapping: the harness's notice forms resolve to the same reason names
structured events use, and only genuinely failure-shaped text falls through to
the ``legacy_message`` catch-all.

All fixtures are synthetic dicts; no real manifest or sidecar is read.
"""


def _ls():
    """Import the module under test at call time, not at collection time."""
    import pipeline.local_success as mod

    return mod


def classify_story(*args, **kwargs):
    """Delegate to pipeline.local_success.classify_story."""
    return _ls().classify_story(*args, **kwargs)


def _story(**overrides):
    """A done, on-device story that is in population and clean by default."""
    story = {
        "story_key": "S1",
        "status": "done",
        "backend": "local",
        "model": "qwen3-30b",
        "dispatched_model": "qwen3-30b",
        "dispatched_at": "2026-09-18T10:00:00Z",
    }
    story.update(overrides)
    return story


def _rec(**overrides):
    return dict(overrides)


def test_park_notice_maps_to_story_parked_only():
    out = classify_story(
        "S1",
        _story(),
        [_rec(event=None, message="S1 parked: high risk held for human review")],
    )
    assert out["clean"] is False
    assert out["reasons"] == ["story_parked"]


def test_escalation_notice_maps_to_escalated_only():
    out = classify_story(
        "S1",
        _story(),
        [_rec(event=None, message="S1 escalating to ollama (rework budget exhausted)")],
    )
    assert out["clean"] is False
    assert out["reasons"] == ["escalated"]


def test_escalation_notice_does_not_double_count_an_escalated_story():
    out = classify_story(
        "S1",
        _story(escalated=True),
        [_rec(event=None, message="S1 escalating to ollama (rework budget exhausted)")],
    )
    assert out["clean"] is False
    assert out["reasons"] == ["escalated"]


def test_fallback_notice_maps_to_model_fallback_only():
    out = classify_story(
        "S1",
        _story(),
        [_rec(event=None, message="S1 switching to fallback model")],
    )
    assert out["clean"] is False
    assert out["reasons"] == ["model_fallback"]


def test_benign_triage_mention_is_not_a_failure():
    out = classify_story(
        "S1",
        _story(),
        [_rec(event=None, message="S1 triage policy updated")],
    )
    assert out["clean"] is True
    assert out["reasons"] == []


def test_wedged_notice_still_falls_through_to_legacy_message():
    out = classify_story(
        "S1",
        _story(),
        [_rec(event=None, message="S1 wedged on a lock")],
    )
    assert out["clean"] is False
    assert out["reasons"] == ["legacy_message"]
