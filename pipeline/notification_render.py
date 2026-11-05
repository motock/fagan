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
# present but not listed here renders its own name, humanized, so a newly
# routed event never masquerades as plan completion.  Only a record with NO
# "event" key at all -- one spooled before the event= stamp -- falls back to
# the legacy "plan complete" subject, so those records render identically; an
# event that is present but None, empty, blank or not a string is a notice
# emitted without an event and renders the neutral UNSTAMPED_LABEL subject.
SUBJECT_LABELS = {
    "plan_completed": "plan complete",
    "story_parked": "story parked",
    "dispatch_failed": "story failed",
    "tests_failed": "story failed",
    "agent_gave_up": "story failed",
    "story_stalled": "story stalled",
    "merge_conflict_rework": "merge rework",
    "usage_paused": "usage paused",
    "usage_resumed": "usage resumed",
    # Plan-approval events are plan-level: they carry the plan's name and
    # never a story key, so none of them belongs in STORY_LEVEL_EVENTS.
    "plan_pending_approval": "plan awaiting approval",
    "plan_approved": "plan approved",
    "plan_changes_requested": "plan changes requested",
}

# Neutral label for a notification emitted without an event: the record
# carries the "event" key (so it is not a pre-stamp legacy record) but the
# value is not a usable event name.  Kept OUT of SUBJECT_LABELS on purpose --
# it is not an event, it is the absence of one.
UNSTAMPED_LABEL = 'notice'


# Only these events name a single story; every other labelled event is
# plan-level, so a story key must never be appended to its subject even when
# the record carries one (the plan-completion record is emitted with the last
# story's key set).
STORY_LEVEL_EVENTS = frozenset({
    "story_parked",
    "dispatch_failed",
    "tests_failed",
    "agent_gave_up",
    "story_stalled",
    "merge_conflict_rework",
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
    story_key = record.get("story_key") or payload.get("story_key")
    if "event" not in payload:
        # Legacy fallback: only a record with no "event" key at all was
        # spooled before the event= stamp, so it alone renders the original
        # "plan complete" subject.
        subject = f"[pipeline] plan complete: {record.get('plan')}"
    elif not isinstance(event, str) or not event.strip():
        # The event key is present but carries no usable event name (None,
        # empty, whitespace-only or not a string): the notice was emitted
        # without an event, so it renders a neutral subject -- never plan
        # completion.
        subject = (
            f"[pipeline] {UNSTAMPED_LABEL}: {record.get('plan')}/{story_key}"
            if story_key else
            f"[pipeline] {UNSTAMPED_LABEL}: {record.get('plan')}"
        )
    else:
        # A present event we have no explicit label for still gets a
        # readable subject derived from its name, never the plan-complete
        # fallback that only pre-stamp records need.
        label = SUBJECT_LABELS.get(event) or event.replace("_", " ").strip()
        story_level = event in STORY_LEVEL_EVENTS or event not in SUBJECT_LABELS
        if story_key and story_level:
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
