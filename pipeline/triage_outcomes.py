"""Record the human resolution beside the overlord's ruling on a parked story.

The overlord's ``failure triage`` ruling says what the machine decided about a
parked story. When an operator later unparks that story, the operator has made
a decision of their own: which action they took, and whether they widened the
story's scope to get it moving. This module writes that answer as its own
decision record, linked to the latest ruling for the same story, so a reader of
the decisions stream can see both the call and the response to it.

The record is appended to the same legacy decisions log the triage ruling uses
(``<plan>.decisions.json``), through :func:`pipeline.persistence._append_decision`,
so the ruling lookup and the record share one stream. The record carries
identifiers, actions and timestamps only -- never a prompt, a payload, or the
text of a file's contents.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from . import persistence

__all__ = ["HUMAN_ACTIONS", "latest_failure_ruling", "record_human_resolution"]

#: What a human did to a parked story. A fixed set, so a resolution can be
#: counted and compared across plans; anything else is refused before a write.
HUMAN_ACTIONS: frozenset[str] = frozenset(
    {
        "amend_scope",
        "resume_review",
        "rebrief",
        "approve_test_edit",
        "merge_held",
        "park",
        "retry",
        "other",
    }
)

_RULING_QUESTION = "failure triage"


def _read_decisions(plan_name: str) -> list[dict[str, Any]]:
    path = persistence._decisions_path(plan_name)
    return json.loads(path.read_text()) if path.exists() else []


def latest_failure_ruling(plan_name: str, story_key: str) -> dict[str, Any] | None:
    """Return the most recent ``failure triage`` ruling for one story, or ``None``.

    Execution records (``failure triage execution``) are not rulings and are
    skipped. Records are read in append order, so the last match is the latest.
    """
    latest: dict[str, Any] | None = None
    for record in _read_decisions(plan_name):
        if (
            record.get("story_key") == story_key
            and record.get("question") == _RULING_QUESTION
        ):
            latest = record
    return latest


def record_human_resolution(
    plan_name: str,
    story_key: str,
    *,
    human_action: str,
    files_added: tuple[str, ...] | list[str] = (),
    new_status: str,
    principal: str,
    note: str = "",
) -> dict[str, Any]:
    """Append a ``human resolution`` record for one story and return it.

    ``ruling_ref`` is the ``decided_at`` of the latest ``failure triage`` ruling
    for the story, and ``ruling_action`` / ``park_class`` are copied from that
    ruling; all three are ``None`` when the story has no ruling on record.

    Raises:
        ValueError: ``human_action`` is not in :data:`HUMAN_ACTIONS`. Raised
            before the ruling is read, so nothing is written.
        OSError: the decisions log could not be written. Callers that must not
            fail on a record write (the operator unpark) catch this.
    """
    if human_action not in HUMAN_ACTIONS:
        raise ValueError(
            f"invalid human action {human_action!r}: must be one of {sorted(HUMAN_ACTIONS)}"
        )

    ruling = latest_failure_ruling(plan_name, story_key) or {}
    record: dict[str, Any] = {
        "question": "human resolution",
        "story_key": story_key,
        "ruling_ref": ruling.get("decided_at"),
        "ruling_action": ruling.get("action"),
        "park_class": ruling.get("park_class"),
        "human_action": human_action,
        "files_added": list(files_added),
        "new_status": new_status,
        "decided_by": principal,
        "decided_at": datetime.now(UTC).isoformat(),
    }
    if note:
        record["note"] = note
    persistence._append_decision(plan_name, record)
    return record
