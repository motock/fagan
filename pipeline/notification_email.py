"""SMTP sender for spooled plan-completion notifications (PLANNOTIFY-05).

``send_notification_email`` sends ONE spooled outbox record as an email over
SMTP.  It is the first code in this repo that ships content off the machine and
it handles a credential, so it is built fail-closed:

* Disabled by default (``PIPELINE_NOTIFY_EMAIL_ENABLED`` must be exactly ``1``);
  no socket is ever opened while disabled.
* Partial configuration (missing host, recipient, or sender) is an ERROR and a
  ``False`` return -- never a send attempt.
* The configured password never reaches a log record, an exception message that
  escapes, or a return value.  Failures are logged as the exception TYPE only.
* Message payloads (body, subject, recipients) are never logged at INFO or
  below; only the outcome is.
* The function never raises: any failure returns ``False``.  This holds
  for a malformed record too -- the outbox drain re-reads records from
  a JSONL file, so an unrenderable record fails closed like any other.

All configuration is read from ``os.environ`` at CALL time (never at import
time) so callers and tests can change the environment between calls.  Only the
standard library is used.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger(__name__)

# Subject labels for the structured event carried at
# record["payload"]["event"] (NOTIFYLC-1).  The subject is DERIVED from the
# record, never configured: there is deliberately no subject knob.  An event
# missing from this map (including a missing/None event) falls back to the
# legacy "plan complete" subject so records spooled before the event= stamp
# still render identically.  "story_parked" is forward-looking: no production
# caller emits event="story_parked" yet -- the park notification in
# pipeline/advance.py calls _notify_user without event=.
SUBJECT_LABELS = {
    "plan_completed": "plan complete",
    "story_parked": "story parked",
    "dispatch_failed": "story failed",
    "tests_failed": "story failed",
    "agent_gave_up": "story failed",
}

# Only these events name a single story; "plan_completed" is plan-level, so a
# story key must never be appended to its subject even when the record carries
# one (the plan-completion record is emitted with the last story's key set).
STORY_LEVEL_EVENTS = frozenset({
    "story_parked",
    "dispatch_failed",
    "tests_failed",
    "agent_gave_up",
})

# Defaults are documented here for the provenance catalog only; every knob is
# re-read from os.environ on each call so runtime changes take effect.
_DEFAULTS = {
    "PIPELINE_NOTIFY_EMAIL_ENABLED": "0",
    "PIPELINE_NOTIFY_EMAIL_HOST": "",
    "PIPELINE_NOTIFY_EMAIL_PORT": "587",
    "PIPELINE_NOTIFY_EMAIL_USER": "",
    "PIPELINE_NOTIFY_EMAIL_PASSWORD": "",
    "PIPELINE_NOTIFY_EMAIL_FROM": "",
    "PIPELINE_NOTIFY_EMAIL_TO": "",
    "PIPELINE_NOTIFY_EMAIL_TIMEOUT": "20",
}


def send_notification_email(record: dict) -> bool:
    """Send one spooled outbox ``record`` as an email over SMTP.

    Returns ``True`` on a successful send and ``False`` on any failure.
    Never raises.
    """
    # Enabled gate first: unset, "0", or anything other than exactly "1"
    # means no-op, with zero construction and zero network activity.
    if os.environ.get("PIPELINE_NOTIFY_EMAIL_ENABLED", "0") != "1":
        return False

    host = os.environ.get("PIPELINE_NOTIFY_EMAIL_HOST", "")
    to_addr = os.environ.get("PIPELINE_NOTIFY_EMAIL_TO", "")
    user = os.environ.get("PIPELINE_NOTIFY_EMAIL_USER", "")
    password = os.environ.get("PIPELINE_NOTIFY_EMAIL_PASSWORD", "")
    from_addr = os.environ.get("PIPELINE_NOTIFY_EMAIL_FROM", "") or user

    # Guarded numeric parsing: a malformed knob fails closed instead of
    # letting smtplib/socket raise from a bad value.
    try:
        port = int(os.environ.get("PIPELINE_NOTIFY_EMAIL_PORT", "587"))
        timeout = float(os.environ.get("PIPELINE_NOTIFY_EMAIL_TIMEOUT", "20"))
    except (TypeError, ValueError):
        log.error(
            "notification email not sent: PIPELINE_NOTIFY_EMAIL_PORT and "
            "PIPELINE_NOTIFY_EMAIL_TIMEOUT must be numeric"
        )
        return False

    # Fail closed on partial configuration -- never attempt a send with a
    # partial config.  Log the missing variable NAMES, never their values.
    missing = [
        name
        for name, value in (
            ("PIPELINE_NOTIFY_EMAIL_HOST", host),
            ("PIPELINE_NOTIFY_EMAIL_TO", to_addr),
            ("PIPELINE_NOTIFY_EMAIL_FROM/USER", from_addr),
        )
        if not value
    ]
    if missing:
        log.error(
            "notification email not sent: missing required configuration: %s",
            ", ".join(missing),
        )
        return False

    # Rendering the record is inside a try because the record is untrusted
    # input: it is re-read from the outbox JSONL file by the drain, so a
    # truncated or hand-edited line can carry a non-string body or a CR/LF in
    # the plan name -- both of which make EmailMessage raise.  Failing closed
    # here (before any connection is opened) keeps the documented "never
    # raises" contract true for the drain loop that calls this per record.
    try:
        # The spooled record is the bus-level event (the
        # pipeline.events.make_event shape that notification_outbox.py
        # persists verbatim via dict(event)), so the message lives at
        # record["payload"]["message"].  The top-level "message" fallback
        # keeps legacy flat {"plan", "message"} records working; ``or {}``
        # (rather than record.get("payload", {})) also guards a spooled
        # "payload": null.
        payload = record.get("payload") or {}
        event = payload.get("event")
        label = SUBJECT_LABELS.get(event)
        if label is None:
            # Legacy fallback: records spooled before the event= stamp carry
            # no event at all, so a missing/None/unrecognized event must
            # still render the original "plan complete" subject.
            subject = f"[pipeline] plan complete: {record.get('plan')}"
        else:
            story_key = record.get("story_key") or payload.get("story_key")
            if story_key and event in STORY_LEVEL_EVENTS:
                subject = f"[pipeline] {label}: {record.get('plan')}/{story_key}"
            else:
                subject = f"[pipeline] {label}: {record.get('plan')}"
        body = payload.get("message", "") or record.get("message", "")
        message = EmailMessage()
        message["From"] = from_addr
        message["To"] = to_addr
        message["Subject"] = subject
        message.set_content(body)
    except Exception as exc:  # noqa: BLE001 - never raise out of a sender
        # Exception TYPE only: the record's own content is a payload and the
        # environment holds the credential, so neither may reach the log.
        log.error(
            "notification email not sent: malformed record (%s)",
            type(exc).__name__,
        )
        return False

    # One try block covers construction through send so the connection is
    # closed on every path (the context manager runs __exit__ even when the
    # constructor, starttls, login, or send_message raises) and no exception
    # object -- whose text may embed the password -- ever escapes.  Only the
    # exception TYPE is logged, never its message or traceback.
    try:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            # Certificate verification is mandatory: a bare starttls() makes
            # smtplib supply the unverified stdlib context (CERT_NONE, no
            # hostname check), and the password crosses this channel on the
            # very next line.  With a verifying context an active MITM's
            # certificate fails verification and the send fails closed before
            # the credential is transmitted.
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(user, password)
            smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001 - never raise out of a sender
        log.error("notification email send failed: %s", type(exc).__name__)
        return False

    log.info("notification email sent")
    return True
