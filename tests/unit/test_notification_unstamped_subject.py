"""Regression tests: a notification emitted without an event is a notice.

Notifications published through ``pipeline.persistence._notify_user`` always
carry an ``event`` key in the bus payload, but the value is ``None`` whenever
the caller passes no ``event=``.  That is *not* a pre-stamp legacy record: the
legacy fallback (``[pipeline] plan complete: <plan>``) is reserved for records
with no ``event`` key at all.  A present-but-None/empty/blank/non-string event
renders the neutral ``[pipeline] notice: <plan>[/<story_key>]`` subject.

These tests call ``render_notification`` on hand-built records -- no mocks.
Both channels (Slack and email) format this subject, so asserting on the
rendered notification covers both.
"""

from __future__ import annotations

from pipeline.events import make_event
from pipeline.notification_render import render_notification

PLAN = "p"
LEGACY_SUBJECT = f"[pipeline] plan complete: {PLAN}"
NOTICE_SUBJECT = f"[pipeline] notice: {PLAN}"


# --- The legacy path: only an absent "event" key keeps plan completion -------

def test_payload_without_event_key_keeps_the_legacy_subject():
    record = {"plan": PLAN, "payload": {"message": "hi"}}

    rendered = render_notification(record)

    assert rendered.subject == LEGACY_SUBJECT


def test_null_payload_keeps_the_legacy_subject():
    record = {"plan": PLAN, "payload": None}

    rendered = render_notification(record)

    assert rendered.subject == LEGACY_SUBJECT


# --- A present but unstamped event renders the neutral notice ----------------

def test_event_none_renders_a_neutral_notice_subject():
    record = {"plan": PLAN, "payload": {"event": None}}

    assert render_notification(record).subject == NOTICE_SUBJECT


def test_event_none_uses_the_record_story_key():
    record = {
        "plan": PLAN,
        "story_key": "S-1",
        "payload": {"event": None},
    }

    assert render_notification(record).subject == f"[pipeline] notice: {PLAN}/S-1"


def test_event_none_falls_back_to_the_payload_story_key():
    record = {"plan": PLAN, "payload": {"event": None, "story_key": "S-1"}}

    assert render_notification(record).subject == f"[pipeline] notice: {PLAN}/S-1"


def test_event_empty_string_renders_a_neutral_notice_subject():
    record = {"plan": PLAN, "payload": {"event": ""}}

    assert render_notification(record).subject == NOTICE_SUBJECT


def test_event_whitespace_only_renders_a_neutral_notice_subject():
    record = {"plan": PLAN, "payload": {"event": "   "}}

    subject = render_notification(record).subject

    assert subject == NOTICE_SUBJECT
    # A blank event must never leak into the "[pipeline] <label>: <plan>" shape.
    assert subject != f"[pipeline] : {PLAN}"


def test_event_non_string_renders_a_neutral_notice_subject():
    record = {"plan": PLAN, "payload": {"event": 123}}

    assert render_notification(record).subject == NOTICE_SUBJECT


def test_unstamped_events_never_render_as_plan_completion():
    for value in (None, "", "   ", 123):
        record = {"plan": PLAN, "payload": {"event": value}}

        subject = render_notification(record).subject

        assert subject != LEGACY_SUBJECT, value


def test_unstamped_record_reports_no_event():
    record = {"plan": PLAN, "payload": {"event": None}}

    assert render_notification(record).event is None


# --- Labelled events are unchanged ------------------------------------------

def test_labelled_plan_completion_still_renders_plan_complete():
    record = {"plan": PLAN, "payload": {"event": "plan_completed"}}

    assert render_notification(record).subject == LEGACY_SUBJECT


def test_labelled_story_parked_still_renders_its_own_subject():
    record = {
        "plan": PLAN,
        "payload": {"event": "story_parked", "story_key": "S-1"},
    }

    assert render_notification(record).subject == f"[pipeline] story parked: {PLAN}/S-1"


# --- End to end: the exact payload _notify_user publishes -------------------

def test_notify_user_payload_shape_renders_a_neutral_notice_subject():
    # Keys copied verbatim from pipeline.persistence._notify_user's bus
    # payload for a call that passes no event= and no story key.
    payload = {
        "message": "hi",
        "story_key": None,
        "severity": "info",
        "event": None,
        "dedup_key": None,
        "ts": "2026-01-01T00:00:00+00:00",
    }
    evt = make_event("notification", PLAN, story_key=None, payload=payload)

    assert render_notification(evt).subject == NOTICE_SUBJECT
