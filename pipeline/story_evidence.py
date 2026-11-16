"""Story evidence returned over the read tools: masking, and the event history.

Two things live here. The masking helpers (:func:`redact`,
:func:`is_secret_name`) are what every story-evidence read tool passes its text
through. And :func:`get_story_events` answers "what happened to this story?"
from the plan's notification history -- the only place a security
``REQUEST_CHANGES`` verdict can be recorded, since the manifest keeps the
review feedback but never the verdict itself.

Everything here is a plain read: the store is only asked for records, and the
records that come back are never mutated.
"""

from __future__ import annotations

import re
from typing import Any

from .module_ref import _ModuleRef

REDACTED = "[redacted]"

_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_TOKEN = re.compile(
    r"\b(?:gh[pos]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})"
)
_BEARER = re.compile(r"(\bBearer\s+)[^\s\"']+", re.IGNORECASE)

# The last word of a secret's name. It must be a whole word: "monkey" ends in
# "key" but the "key" is preceded by a letter, and "keyboard" starts with "key"
# but the "key" is followed by one, so neither is a secret name.
_WORDS = r"(?:TOKEN|SECRET|PASSWORD|API_KEY|KEY)"
# A hyphenated, dotted or underscored prefix such as "x-api-" or "openai_".
_PREFIX = r"[A-Za-z0-9_.\-]*?"
# Left boundary before the word, right boundary after it.
_NAME = rf"{_PREFIX}(?<![A-Za-z0-9]){_WORDS}(?![A-Za-z0-9_])"

_SECRET_NAME = re.compile(_NAME, re.IGNORECASE)
# name, then "=" / ":" (possibly quoted, as in JSON or Python literals), then value.
_NAMED = re.compile(
    rf"({_NAME})(\s*[\"']?\s*[=:]\s*[\"']?)(\"[^\"]*\"|'[^']*'|[^\s\"',}}]+)",
    re.IGNORECASE,
)


def is_secret_name(name: object) -> bool:
    """True when *name* itself names a secret, e.g. ``api_key`` or ``x-api-key``."""
    return isinstance(name, str) and _SECRET_NAME.fullmatch(name) is not None


def redact(text: str) -> str:
    """Replace secret values in *text* with ``[redacted]``, keeping names."""
    text = _PEM.sub(REDACTED, text)
    text = _TOKEN.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    return _NAMED.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)


#: The configured store. Resolved through ``pipeline.server`` at call time so
#: the suite's store patches land, and so this module never spells a plan file.
_store = _ModuleRef("pipeline.server", "_store")

#: The refusal the plan-scoped read tools answer an unknown plan/story with.
#: A denial, never an empty result: an empty list would read as "this story has
#: no events", which is a different answer from "I cannot show it to you".
NOT_FOUND = {"ok": False, "status": 404, "error": "unknown plan/story"}

#: Events returned when the caller does not say how many it wants.
EVENT_LIMIT_DEFAULT = 50
#: Bounds a caller's ``limit`` is clamped to. The floor is 1, not 0: a caller
#: that passes 0 or a negative number still gets the newest event, because an
#: empty list reads as "no events" rather than "you asked for none".
EVENT_LIMIT_MIN = 1
EVENT_LIMIT_MAX = 200
#: Longest excerpt of a story's inconclusive reviewer output returned.
OUTPUT_EXCERPT_MAX = 4000

#: Story-manifest free text returned alongside the events.
STORY_TEXT_FIELDS = ("review_feedback", "parked_reason", "failure_reason")


def _clamp_limit(limit: object) -> int:
    """Return *limit* clamped to 1..EVENT_LIMIT_MAX; a non-integer uses the default."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        return EVENT_LIMIT_DEFAULT
    return max(EVENT_LIMIT_MIN, min(limit, EVENT_LIMIT_MAX))


def _story(manifest: object, story_key: str) -> dict[str, Any] | None:
    """Return the story entry from *manifest*, or None when it is not there."""
    stories = manifest.get("stories") if isinstance(manifest, dict) else None
    if not isinstance(stories, dict):
        return None
    story = stories.get(story_key)
    return story if isinstance(story, dict) else None


def _event(seq: int, record: dict[str, Any]) -> dict[str, Any]:
    """Return one notification record as an event, its message masked."""
    message = record.get("message")
    return {
        "seq": seq,
        "ts": record.get("ts"),
        "event": record.get("event"),
        "message": redact(message) if isinstance(message, str) else None,
    }


def _story_evidence(story: dict[str, Any]) -> dict[str, Any]:
    """Return the story's own free-text fields, masked, omitting empty ones."""
    evidence = {
        field: redact(value)
        for field in STORY_TEXT_FIELDS
        if isinstance((value := story.get(field)), str) and value
    }
    # The reviewer's own output is where an inconclusive verdict lands. Mask
    # first, then bound: truncating first could slice a secret in half and
    # leave both halves unrecognisable to the masking patterns.
    excerpt = story.get("last_inconclusive_output_excerpt")
    if isinstance(excerpt, str) and excerpt:
        evidence["last_inconclusive_output_excerpt"] = redact(excerpt)[
            :OUTPUT_EXCERPT_MAX
        ]
    return evidence


def get_story_events(
    plan_name: str, story_key: str, limit: int = EVENT_LIMIT_DEFAULT
) -> dict[str, Any]:
    """
    Return the story's event history, newest first, with secret values masked.

    plan_name: the plan's name this story belongs to.
    story_key: the story's key within that plan's manifest.
    limit: how many of the newest events to return, clamped to 1..200; a
        non-integer uses the default of 50.

    The events come from the plan's notification history in the namespace
    bound for the request. Each is {"seq", "ts", "event", "message"}. The
    story's own review_feedback, parked_reason, failure_reason and
    last_inconclusive_output_excerpt (masked, at most 4000 characters) are
    returned alongside them when the story carries them.

    A story with no events is {"available": True, "events": []}. An unknown
    plan/story returns {"ok": False, "status": 404, "error": ...}.
    """
    # Imported here, not at module level: preflight imports this module and
    # must not pull in the request-scoped actor.
    from pipeline.actor import current_namespace

    story = _story(_store.get_manifest_or_none(plan_name), story_key)
    if story is None:
        return dict(NOT_FOUND)
    records, _malformed = _store.records().get_notification_history(
        current_namespace(), plan_name
    )
    # seq is the record's position in the plan's history, so it is stable
    # across calls and distinct within one answer. The records are read into a
    # new list of new dicts: nothing sorts, pops or deletes the store's own
    # list, so this read leaves the stored history exactly as it was.
    numbered = [
        (seq, record)
        for seq, record in enumerate(
            records if isinstance(records, list) else [], start=1
        )
        if isinstance(record, dict) and record.get("story_key") == story_key
    ]
    numbered.sort(key=lambda pair: pair[0], reverse=True)
    events = [_event(seq, record) for seq, record in numbered]
    return {
        "available": True,
        "events": events[: _clamp_limit(limit)],
        **_story_evidence(story),
    }
