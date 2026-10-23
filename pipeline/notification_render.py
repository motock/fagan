"""Channel-neutral rendering of a spooled notification record.

``render_notification`` turns one outbox record into a
``RenderedNotification`` that every channel type formats for its own
transport.  It may raise on a malformed record: the outbox drain re-reads
records from JSONL, and callers wrap this in a fail-closed try.
"""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class RenderedNotification:
    subject: str
    body: str
    event: str | None
    plan: str | None
    story_key: str | None


def render_notification(record: dict) -> RenderedNotification:
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
    story_key = record.get("story_key") or payload.get("story_key")
    if label is None:
        # Legacy fallback: records spooled before the event= stamp carry
        # no event at all, so a missing/None/unrecognized event must
        # still render the original "plan complete" subject.
        subject = f"[pipeline] plan complete: {record.get('plan')}"
    elif story_key and event in STORY_LEVEL_EVENTS:
        subject = f"[pipeline] {label}: {record.get('plan')}/{story_key}"
    else:
        subject = f"[pipeline] {label}: {record.get('plan')}"
    body = payload.get("message", "") or record.get("message", "")
    return RenderedNotification(
        subject=subject,
        body=body,
        event=event,
        plan=record.get("plan"),
        story_key=story_key,
    )
