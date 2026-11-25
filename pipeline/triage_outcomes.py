"""Record the human resolution beside the overlord's ruling on a parked story.

The overlord's ``failure triage`` ruling says what the machine decided about a
parked story. When an operator later unparks that story, the operator has made
a decision of their own: which action they took, and whether they widened the
story's scope to get it moving. This module writes that answer as its own
decision record, linked to the latest ruling for the same story, so a reader of
the decisions stream can see both the call and the response to it.

The record is appended to the same decisions stream the triage ruling uses, so
the ruling lookup and the record share one stream: the record store's decision
view when the configured store owns its decisions, the legacy
``<plan>.decisions.json`` file through
:func:`pipeline.persistence._append_decision` otherwise. The choice lives in
:mod:`pipeline.decision_log`, the one seam both the writers and this reader go
through. The record carries identifiers, actions and timestamps only -- never a
prompt, a payload, or the text of a file's contents.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from . import decision_log, persistence
from .story_evidence import redact

logger = logging.getLogger(__name__)

__all__ = [
    "HUMAN_ACTIONS",
    "latest_failure_ruling",
    "record_human_resolution",
    "record_resolution_if_ruled",
]

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
_ADJUDICATION_QUESTION = "high-risk merge adjudication"

#: Longest operator rationale kept on a resolution. The reasoning is short by
#: design: a resolution is a pointer to the answer, not a copy of the thread.
RATIONALE_LIMIT = 500

#: The park class a high-risk merge adjudication belongs to. The adjudication
#: record carries a ``ruling`` and no park class of its own.
HIGH_RISK_MERGE_CLASS = "high_risk_merge"


def _read_decisions(plan_name: str) -> list[dict[str, Any]]:
    return decision_log.read_decisions(plan_name)


def _is_ruling(record: dict[str, Any]) -> bool:
    """True when ``record`` is a ruling this module links a resolution to.

    A ``high-risk merge adjudication`` whose ``ruling`` is empty is the
    fail-closed record for an unparseable reply: it decided nothing a human can
    answer, so it is not a ruling.
    """
    question = record.get("question")
    if question == _RULING_QUESTION:
        return True
    return question == _ADJUDICATION_QUESTION and bool(record.get("ruling"))


def latest_failure_ruling(plan_name: str, story_key: str) -> dict[str, Any] | None:
    """Return the most recent ruling for one story, or ``None``.

    A ruling is a ``failure triage`` record or a ``high-risk merge
    adjudication`` record that carries a parseable ``ruling``. Execution records
    (``failure triage execution``) are not rulings and are skipped. Records are
    read in append order, so the last match is the latest.
    """
    latest: dict[str, Any] | None = None
    for record in _read_decisions(plan_name):
        if record.get("story_key") == story_key and _is_ruling(record):
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
    brief_changed: bool = False,
    rationale: str = "",
) -> dict[str, Any]:
    """Append a ``human resolution`` record for one story and return it.

    ``ruling_ref`` is the ``decided_at`` of the latest ruling for the story, and
    ``ruling_action`` / ``park_class`` are copied from that ruling; all three
    are ``None`` when the story has no ruling on record. For a
    ``high-risk merge adjudication`` the ruling action is its ``ruling``
    (``proceed`` or ``park``) and the park class is ``high_risk_merge``.

    ``brief_changed=True`` records that the human changed the story's brief
    (``agent_instructions``). The key is omitted otherwise; the brief's text
    is never written.

    ``rationale`` is the operator's reasoning. It is redacted with the decisions
    stream's secret redaction and capped at :data:`RATIONALE_LIMIT` characters;
    the key is omitted when it is empty.

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
    if ruling.get("question") == _ADJUDICATION_QUESTION:
        ruling_action = ruling.get("ruling")
        park_class = HIGH_RISK_MERGE_CLASS
    else:
        ruling_action = ruling.get("action")
        park_class = ruling.get("park_class")
    record: dict[str, Any] = {
        "question": "human resolution",
        "story_key": story_key,
        "ruling_ref": ruling.get("decided_at"),
        "ruling_action": ruling_action,
        "park_class": park_class,
        "human_action": human_action,
        "files_added": list(files_added),
        "new_status": new_status,
        "decided_by": principal,
        "decided_at": datetime.now(UTC).isoformat(),
    }
    if note:
        record["note"] = note
    if brief_changed:
        record["brief_changed"] = True
    if rationale.strip():
        record["rationale"] = redact(rationale.strip())[:RATIONALE_LIMIT]
    # The file store keeps the one legacy writer, which existing callers patch
    # by name; any store with a record view owns the record instead.
    if decision_log.store_owns_records():
        decision_log.append_decision(plan_name, record)
    else:
        persistence._append_decision(plan_name, record)
    return record


def _already_answered(plan_name: str, story_key: str, ruling_ref: Any) -> bool:
    """True when a resolution already links this story to this ruling."""
    for record in _read_decisions(plan_name):
        if (
            record.get("question") == "human resolution"
            and record.get("story_key") == story_key
            and record.get("ruling_ref") == ruling_ref
        ):
            return True
    return False


def record_resolution_if_ruled(
    plan_name: str,
    story_key: str,
    *,
    human_action: str,
    new_status: str,
    principal: str,
    rationale: str = "",
    brief_changed: bool = False,
) -> dict[str, Any] | None:
    """Record a ``human resolution`` for a story with an unanswered ruling.

    A person merging a story, closing a merge they made themselves, or moving a
    story off ``parked`` / ``pr_open`` has answered the overlord's ruling on that
    story. This writes that answer beside the ruling, once per ruling: the
    dedupe key is the ruling's ``decided_at``, so a new ruling for the same story
    is answered again while a repeated call for the same ruling is not.

    Returns:
        The ``human resolution`` record when one was written, otherwise
        ``None`` -- no ruling on record, or this ruling already answered.

    Never raises: a failed write is logged at ``WARNING`` with identifiers only
    (the plan and the story), because a missing audit record must not fail the
    merge or the status change the person asked for.
    """
    try:
        ruling = latest_failure_ruling(plan_name, story_key)
        if ruling is None:
            return None
        ruling_ref = ruling.get("decided_at")
        if _already_answered(plan_name, story_key, ruling_ref):
            return None
        return record_human_resolution(
            plan_name,
            story_key,
            human_action=human_action,
            new_status=new_status,
            principal=principal,
            rationale=rationale,
            brief_changed=brief_changed,
        )
    except Exception:  # noqa: BLE001 - a failed audit record must not fail the caller
        logger.warning(
            "could not record the human resolution for %s in plan %s",
            story_key,
            plan_name,
            extra={
                "plan_name": plan_name,
                "story_key": story_key,
                "human_action": human_action,
                "new_status": new_status,
                "outcome": "resolution_not_recorded",
            },
        )
        return None
