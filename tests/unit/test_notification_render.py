import dataclasses

import pytest

from pipeline.notification_render import (
    STORY_LEVEL_EVENTS,
    SUBJECT_LABELS,
    RenderedNotification,
    render_notification,
)


def _record(event=None, key=None, message="hello", plan="p"):
    payload = {"message": message}
    if event is not None:
        payload["event"] = event
    rec = {"plan": plan, "payload": payload}
    if key is not None:
        rec["story_key"] = key
    return rec


def test_plan_completed_subject():
    r = render_notification(_record("plan_completed"))
    assert r.subject == "[pipeline] plan complete: p"


def test_story_parked_subject():
    assert render_notification(_record("story_parked")).subject == "[pipeline] story parked: p"


def test_dispatch_failed_subject():
    assert render_notification(_record("dispatch_failed")).subject == "[pipeline] story failed: p"


def test_tests_failed_subject():
    assert render_notification(_record("tests_failed")).subject == "[pipeline] story failed: p"


def test_agent_gave_up_subject():
    assert render_notification(_record("agent_gave_up")).subject == "[pipeline] story failed: p"


def test_missing_event_falls_back_to_plan_complete():
    r = render_notification(_record())
    assert r.subject == "[pipeline] plan complete: p"
    assert r.event is None


def test_unknown_event_falls_back_to_plan_complete():
    assert render_notification(_record("mystery")).subject == "[pipeline] plan complete: p"


def test_story_level_event_with_key_appends_key():
    r = render_notification(_record("story_parked", key="S-1"))
    assert r.subject == "[pipeline] story parked: p/S-1"
    assert r.story_key == "S-1"


def test_story_level_event_without_key_has_no_suffix():
    assert render_notification(_record("story_parked")).subject == "[pipeline] story parked: p"


def test_story_key_read_from_payload():
    rec = {"plan": "p", "payload": {"event": "tests_failed", "story_key": "S-2"}}
    assert render_notification(rec).subject == "[pipeline] story failed: p/S-2"


def test_plan_completed_with_key_omits_key():
    r = render_notification(_record("plan_completed", key="S-9"))
    assert "S-9" not in r.subject


def test_payload_null_uses_top_level_message():
    r = render_notification({"plan": "p", "payload": None, "message": "flat"})
    assert r.body == "flat"
    assert r.subject == "[pipeline] plan complete: p"


def test_legacy_flat_record():
    r = render_notification({"plan": "p", "message": "legacy"})
    assert (r.subject, r.body) == ("[pipeline] plan complete: p", "legacy")


def test_payload_message_wins_over_top_level():
    rec = {"plan": "p", "message": "top", "payload": {"message": "inner"}}
    assert render_notification(rec).body == "inner"


def test_rendered_fields_and_frozen():
    r = render_notification(_record("story_parked", key="S-1"))
    assert (r.event, r.plan, r.body) == ("story_parked", "p", "hello")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.subject = "x"
    assert isinstance(r, RenderedNotification)


def test_non_dict_payload_raises():
    with pytest.raises(AttributeError):
        render_notification({"plan": "p", "payload": "oops"})


def test_story_level_events_subset_of_labels():
    assert STORY_LEVEL_EVENTS <= set(SUBJECT_LABELS)
