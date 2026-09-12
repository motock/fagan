"""Subject-line tests for ``pipeline.notification_email.send_notification_email``.

Contract under test (PLANNOTIFY-08):

* The e-mail subject is DERIVED from the spooled record's structured event
  name -- ``(record.get("payload") or {}).get("event")`` -- never from the
  message text and never from an environment knob.
* A module-level dict maps structured event name -> subject label:

      "plan_completed"  -> "plan complete"
      "story_parked"    -> "story parked"
      "dispatch_failed" -> "story failed"
      "tests_failed"    -> "story failed"
      "agent_gave_up"   -> "story failed"

* Subject shapes:

  - ``plan_completed`` and ANY unrecognized/missing event (the fallback):
        ``[pipeline] plan complete: <plan>``
  - story-level events (story_parked, dispatch_failed, tests_failed,
    agent_gave_up):
        ``[pipeline] <label>: <plan>/<story_key>``
    where ``story_key = record.get("story_key") or payload.get("story_key")``.
    A missing or empty story_key degrades to ``[pipeline] <label>: <plan>``.

Two hard constraints this file also grades:

A. The fallback subject for a missing/None/unknown event stays EXACTLY
   ``[pipeline] plan complete: <plan>`` -- records spooled before this change
   carry no event key and are still in flight (delivery is at-least-once).
B. No ninth ``os.environ.get("PIPELINE_NOTIFY_EMAIL_...")`` call is added to
   the module: the subject is derived, never configured.

Every test mocks at the external boundary ONLY (``smtplib.SMTP``); no test
opens a socket.  This file is self-contained: it does not import helpers from
any other test module.
"""

from __future__ import annotations

import email.message
import logging
import re
import smtplib
from pathlib import Path
from typing import ClassVar

import pytest

from pipeline import notification_email
from pipeline.notification_email import send_notification_email

ENABLED = "PIPELINE_NOTIFY_EMAIL_ENABLED"
HOST = "PIPELINE_NOTIFY_EMAIL_HOST"
PORT = "PIPELINE_NOTIFY_EMAIL_PORT"
USER = "PIPELINE_NOTIFY_EMAIL_USER"
PASSWORD = "PIPELINE_NOTIFY_EMAIL_PASSWORD"
FROM = "PIPELINE_NOTIFY_EMAIL_FROM"
TO = "PIPELINE_NOTIFY_EMAIL_TO"
TIMEOUT = "PIPELINE_NOTIFY_EMAIL_TIMEOUT"

ALL_VARS = (ENABLED, HOST, PORT, USER, PASSWORD, FROM, TO, TIMEOUT)

BASE_ENV = {
    ENABLED: "1",
    HOST: "smtp.example.test",
    PORT: "2525",
    USER: "mailer@example.test",
    PASSWORD: "pw-sentinel-value",
    FROM: "pipeline@example.test",
    TO: "sink-recipient@example.test",
    TIMEOUT: "7",
}

# The label table this story must add (event name -> subject label).
EXPECTED_LABELS = {
    "plan_completed": "plan complete",
    "story_parked": "story parked",
    "dispatch_failed": "story failed",
    "tests_failed": "story failed",
    "agent_gave_up": "story failed",
}

STORY_LEVEL_EVENTS = (
    "story_parked",
    "dispatch_failed",
    "tests_failed",
    "agent_gave_up",
)


class FakeSMTP:
    """Recording stand-in for ``smtplib.SMTP``; never touches a socket."""

    instances: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        self.init_args = args
        self.init_kwargs = kwargs
        self.send_message_calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self, *args, **kwargs):
        return (220, b"go ahead")

    def login(self, *args, **kwargs):
        return (235, b"ok")

    def send_message(self, message, *args, **kwargs):
        self.send_message_calls.append((message, args, kwargs))
        return {}

    def quit(self):
        return (221, b"bye")


def _set_env(monkeypatch):
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)


def _install_fake(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    # Tolerate an implementation that does ``from smtplib import SMTP``.
    monkeypatch.setattr(notification_email, "SMTP", FakeSMTP, raising=False)
    return FakeSMTP


def _sent_message(fake):
    assert fake.send_message_calls, "send_message was never called"
    message, _args, _kwargs = fake.send_message_calls[0]
    return message


def _send(monkeypatch, record):
    """Send ``record`` through a recording fake and return (result, message)."""
    _set_env(monkeypatch)
    fake_cls = _install_fake(monkeypatch)
    result = send_notification_email(record)
    message = _sent_message(fake_cls.instances[-1]) if fake_cls.instances else None
    return result, message


def _subject_for(monkeypatch, record):
    result, message = _send(monkeypatch, record)
    assert result is True, "a well-formed record must send successfully"
    assert isinstance(message, email.message.EmailMessage)
    return message["Subject"]


def _body_of(message):
    return message.get_content()


def _event_label_map():
    """Return the module-level event->label dict, whatever it is named."""
    candidates = [
        value
        for value in vars(notification_email).values()
        if isinstance(value, dict) and "story_parked" in value
    ]
    assert candidates, (
        "notification_email must define a module-level dict mapping structured "
        "event names (e.g. 'story_parked') to subject labels"
    )
    return candidates[0]


# --------------------------------------------------------------------------
# The label table itself.
# --------------------------------------------------------------------------


def test_module_level_event_label_map_covers_every_documented_event():
    labels = _event_label_map()
    for event, label in EXPECTED_LABELS.items():
        assert event in labels, f"event {event!r} missing from the label map"
        assert labels[event] == label, (
            f"event {event!r} must map to {label!r}, got {labels[event]!r}"
        )


def test_label_map_is_not_configured_from_the_environment():
    """Constraint B: the subject is derived, never configured."""
    source = Path(notification_email.__file__).read_text(encoding="utf-8")
    names = set(
        re.findall(
            r"os\.environ\.get\(\s*\"(PIPELINE_NOTIFY_EMAIL_[A-Z_]+)\"",
            source,
        )
    )
    assert names == set(ALL_VARS), (
        "the module must read exactly the eight documented "
        f"PIPELINE_NOTIFY_EMAIL_* variables, got {sorted(names)}"
    )


# --------------------------------------------------------------------------
# Positive: each documented event yields its documented subject.
# --------------------------------------------------------------------------


def test_plan_completed_event_uses_plan_complete_subject(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {
            "plan": "PLAN-42",
            "story_key": "STORY-1",
            "payload": {"event": "plan_completed", "message": "done"},
        },
    )
    assert subject == "[pipeline] plan complete: PLAN-42"


def test_story_parked_event_uses_story_parked_subject(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {
            "plan": "PLAN-42",
            "story_key": "STORY-7",
            "payload": {"event": "story_parked", "message": "parked"},
        },
    )
    assert subject == "[pipeline] story parked: PLAN-42/STORY-7"


def test_dispatch_failed_event_uses_story_failed_subject(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {
            "plan": "PLAN-42",
            "story_key": "STORY-7",
            "payload": {"event": "dispatch_failed", "message": "boom"},
        },
    )
    assert subject == "[pipeline] story failed: PLAN-42/STORY-7"


def test_tests_failed_event_uses_story_failed_subject(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {
            "plan": "PLAN-42",
            "story_key": "STORY-7",
            "payload": {"event": "tests_failed", "message": "red"},
        },
    )
    assert subject == "[pipeline] story failed: PLAN-42/STORY-7"


def test_agent_gave_up_event_uses_story_failed_subject(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {
            "plan": "PLAN-42",
            "story_key": "STORY-7",
            "payload": {"event": "agent_gave_up", "message": "stuck"},
        },
    )
    assert subject == "[pipeline] story failed: PLAN-42/STORY-7"


@pytest.mark.parametrize("event", STORY_LEVEL_EVENTS)
def test_story_level_events_render_plan_slash_story_key(monkeypatch, event):
    subject = _subject_for(
        monkeypatch,
        {
            "plan": "PLAN-42",
            "story_key": "STORY-7",
            "payload": {"event": event, "message": "x"},
        },
    )
    assert subject == f"[pipeline] {EXPECTED_LABELS[event]}: PLAN-42/STORY-7"


# --------------------------------------------------------------------------
# story_key resolution: top level, payload, precedence, degradation.
# --------------------------------------------------------------------------


def test_story_key_is_read_from_the_top_level(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {
            "plan": "PLAN-42",
            "story_key": "TOP-LEVEL",
            "payload": {"event": "story_parked", "message": "x"},
        },
    )
    assert subject == "[pipeline] story parked: PLAN-42/TOP-LEVEL"


def test_story_key_is_read_from_the_payload(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {
            "plan": "PLAN-42",
            "payload": {
                "event": "story_parked",
                "story_key": "FROM-PAYLOAD",
                "message": "x",
            },
        },
    )
    assert subject == "[pipeline] story parked: PLAN-42/FROM-PAYLOAD"


def test_top_level_story_key_wins_over_payload_story_key(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {
            "plan": "PLAN-42",
            "story_key": "TOP-LEVEL",
            "payload": {
                "event": "story_parked",
                "story_key": "FROM-PAYLOAD",
                "message": "x",
            },
        },
    )
    assert subject == "[pipeline] story parked: PLAN-42/TOP-LEVEL"


def test_empty_top_level_story_key_falls_back_to_payload(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {
            "plan": "PLAN-42",
            "story_key": "",
            "payload": {
                "event": "story_parked",
                "story_key": "FROM-PAYLOAD",
                "message": "x",
            },
        },
    )
    assert subject == "[pipeline] story parked: PLAN-42/FROM-PAYLOAD"


@pytest.mark.parametrize("event", STORY_LEVEL_EVENTS)
def test_story_level_event_without_story_key_degrades_to_plan_only(
    monkeypatch, event
):
    subject = _subject_for(
        monkeypatch,
        {"plan": "PLAN-42", "payload": {"event": event, "message": "x"}},
    )
    assert subject == f"[pipeline] {EXPECTED_LABELS[event]}: PLAN-42"


@pytest.mark.parametrize("event", STORY_LEVEL_EVENTS)
def test_story_level_event_with_empty_story_key_degrades_to_plan_only(
    monkeypatch, event
):
    subject = _subject_for(
        monkeypatch,
        {
            "plan": "PLAN-42",
            "story_key": "",
            "payload": {"event": event, "story_key": "", "message": "x"},
        },
    )
    assert subject == f"[pipeline] {EXPECTED_LABELS[event]}: PLAN-42"


# --------------------------------------------------------------------------
# Negative / boundary: the legacy fallback subject (constraint A).
# --------------------------------------------------------------------------


def test_payload_absent_keeps_legacy_subject(monkeypatch):
    subject = _subject_for(monkeypatch, {"plan": "PLAN-42", "message": "hi"})
    assert subject == "[pipeline] plan complete: PLAN-42"


def test_payload_null_keeps_legacy_subject(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {"plan": "PLAN-42", "payload": None, "message": "hi"},
    )
    assert subject == "[pipeline] plan complete: PLAN-42"


def test_payload_without_event_key_keeps_legacy_subject(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {"plan": "PLAN-42", "payload": {"message": "hi"}},
    )
    assert subject == "[pipeline] plan complete: PLAN-42"


def test_event_none_keeps_legacy_subject(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {"plan": "PLAN-42", "payload": {"event": None, "message": "hi"}},
    )
    assert subject == "[pipeline] plan complete: PLAN-42"


def test_event_empty_string_keeps_legacy_subject(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {"plan": "PLAN-42", "payload": {"event": "", "message": "hi"}},
    )
    assert subject == "[pipeline] plan complete: PLAN-42"


def test_unrecognized_event_keeps_legacy_subject(monkeypatch):
    subject = _subject_for(
        monkeypatch,
        {
            "plan": "PLAN-42",
            "story_key": "STORY-7",
            "payload": {"event": "totally_unknown_event", "message": "hi"},
        },
    )
    assert subject == "[pipeline] plan complete: PLAN-42"


def test_legacy_record_without_plan_renders_without_raising(monkeypatch):
    result, message = _send(monkeypatch, {"message": "hi"})
    assert result is True
    assert message["Subject"] == "[pipeline] plan complete: None"


def test_plan_none_renders_without_raising(monkeypatch):
    result, message = _send(monkeypatch, {"plan": None, "message": "hi"})
    assert result is True
    assert message["Subject"] == "[pipeline] plan complete: None"


def test_empty_record_renders_without_raising(monkeypatch):
    result, message = _send(monkeypatch, {})
    assert result is True
    assert message["Subject"] == "[pipeline] plan complete: None"


# --------------------------------------------------------------------------
# Preserved behaviour: body, never-raises, log hygiene.
# --------------------------------------------------------------------------


def test_body_still_comes_from_the_payload_message(monkeypatch):
    result, message = _send(
        monkeypatch,
        {
            "plan": "PLAN-42",
            "story_key": "STORY-7",
            "payload": {"event": "story_parked", "message": "parked for review"},
        },
    )
    assert result is True
    assert message["Subject"] == "[pipeline] story parked: PLAN-42/STORY-7"
    assert _body_of(message).strip() == "parked for review"


def test_malformed_plan_still_fails_closed_without_raising(monkeypatch):
    """Subject construction stays inside the try/except that guards rendering."""
    result, _message = _send(
        monkeypatch,
        {
            "plan": "PLAN-42\nBcc: attacker@example.test",
            "payload": {"event": "story_parked", "message": "x"},
        },
    )
    assert result is False


def test_non_dict_payload_never_raises(monkeypatch):
    """A malformed record fails closed (or falls back) -- it never raises."""
    _set_env(monkeypatch)
    _install_fake(monkeypatch)
    result = send_notification_email(
        {"plan": "PLAN-42", "payload": "not-a-dict", "message": "x"}
    )
    assert isinstance(result, bool)


def test_subject_and_body_never_reach_the_log(monkeypatch, caplog):
    subject_sentinel = "SENTINEL-SUBJECT-STORY-7"
    body_sentinel = "SENTINEL-BODY-STORY-7"
    with caplog.at_level(logging.DEBUG):
        result, message = _send(
            monkeypatch,
            {
                "plan": "PLAN-42",
                "story_key": subject_sentinel,
                "payload": {
                    "event": "story_parked",
                    "message": body_sentinel,
                },
            },
        )
    assert result is True
    assert message["Subject"] == f"[pipeline] story parked: PLAN-42/{subject_sentinel}"
    assert subject_sentinel not in caplog.text
    assert body_sentinel not in caplog.text
