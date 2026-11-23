"""Plan guidance: a plan that needs a conversation before it can be approved.

A ticket too unclear to plan is planned with a guidance record on the manifest::

    manifest["guidance"] = {
        "status": "needed" | "resolved",
        "questions": [str, ...],        # 1..5, each 1..300 characters
        "source": {"kind": "jira" | "github", "ref": str},
        "request_excerpt": str,         # capped at 2000 characters
        "raised_at": str,               # ISO-8601 UTC
        # set on resolution only:
        "resolved_by": str, "resolved_at": str, "note": str,
    }

``raise_guidance`` writes the record needed and sends one notice; the approval
gate in ``pipeline.plan_approval`` refuses an approval while it is needed.
``resolve_guidance`` settles it. The record is not part of the approval digest,
and ``ingest_plan`` carries unknown top-level manifest keys over a re-ingest, so
the record survives a revised plan.

The questions and the excerpt are untrusted text derived from an outside
ticket. They are never logged, never audited, never put in a notice and never
echoed in a refusal: the audit and the notice carry identifiers and the question
count only, and a refusal carries a fixed reason token.
"""

from __future__ import annotations

import copy
import logging
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from . import bound_records, notification_links
from .actor import current_principal
from .audit import record_audit
from .parsers import _validate_key
from .service import _ServerRef

logger = logging.getLogger(__name__)

GUIDANCE_KEY = "guidance"
NEEDED = "needed"
RESOLVED = "resolved"

NEEDS_GUIDANCE_EVENT = "plan_needs_guidance"
GUIDANCE_RESOLVED_EVENT = "plan_guidance_resolved"
RAISED_ACTION = "plan.guidance_raised"
RESOLVED_ACTION = "plan.guidance_resolved"

MAX_QUESTIONS = 5
MAX_QUESTION_CHARS = 300
MAX_EXCERPT_CHARS = 2000
MAX_NOTE_CHARS = 500
SOURCE_KINDS = frozenset({"jira", "github"})
_SOURCE_REF_RE = re.compile(r"[A-Za-z0-9._/#-]{1,120}")

_notify_user = _ServerRef("_notify_user")


def _refused(reason: str) -> dict[str, Any]:
    """Return a refusal carrying only a fixed reason token."""
    return {"ok": False, "error": reason}


def _strip_control(text: str) -> str:
    """Return ``text`` without Unicode control characters (category ``Cc``)."""
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cc")


def _clean_questions(questions: object) -> list[str] | None:
    """Return the cleaned questions, or ``None`` when the list is not acceptable."""
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUESTIONS:
        return None
    cleaned: list[str] = []
    for question in questions:
        if not isinstance(question, str):
            return None
        text = _strip_control(question).strip()
        if not 1 <= len(text) <= MAX_QUESTION_CHARS:
            return None
        cleaned.append(text)
    return cleaned


def _review_link(plan_name: str) -> str | None:
    """Return the plan's review link, or ``None`` when no dashboard is configured."""
    origin = notification_links._origin()
    path = notification_links._link_path()
    if origin is None or path is None:
        return None
    return f"{origin}{path}?plan={quote(plan_name, safe='')}"


def _notify_needs_guidance(plan_name: str, question_count: int, raised_at: str) -> None:
    """Send the one notice. It names the plan and a count, never the questions."""
    message = (
        f"{plan_name} needs guidance before it can be approved "
        f"({question_count} open questions)."
    )
    link = _review_link(plan_name)
    if link:
        message = f"{message} Review: {link}"
    try:
        _notify_user(
            plan_name,
            message,
            event=NEEDS_GUIDANCE_EVENT,
            dedup_key=f"{NEEDS_GUIDANCE_EVENT}:{plan_name}:{raised_at}",
        )
    except Exception as exc:  # noqa: BLE001 - a notice must never undo the raise
        logger.warning("Failed to emit plan guidance notice (%s)", type(exc).__name__)


def guidance_view(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Return a copy of the manifest's guidance record, or ``None`` when absent."""
    record = manifest.get(GUIDANCE_KEY)
    return copy.deepcopy(record) if isinstance(record, dict) else None


def needs_guidance(manifest: dict[str, Any]) -> bool:
    """Return whether the plan may not be approved until its guidance is resolved.

    Fails closed: any record that is not explicitly ``resolved`` (a malformed
    value included) blocks approval. A plan with no record is not gated.
    """
    if GUIDANCE_KEY not in manifest:
        return False
    record = manifest[GUIDANCE_KEY]
    return not (isinstance(record, dict) and record.get("status") == RESOLVED)


def raise_guidance(
    plan_name: str,
    questions: Any,
    source_kind: Any,
    source_ref: Any,
    request_excerpt: Any,
) -> dict[str, Any]:
    """Mark ``plan_name`` as needing guidance, audit it and notify the team.

    In-process only: the intake scheduler calls it, never a transport. Every
    input is validated at this boundary and a refusal writes nothing. The
    excerpt is truncated to ``MAX_EXCERPT_CHARS`` rather than refused.
    """
    _validate_key(plan_name)
    cleaned = _clean_questions(questions)
    if cleaned is None:
        return _refused("invalid_questions")
    if not isinstance(source_kind, str) or source_kind not in SOURCE_KINDS:
        return _refused("invalid_source")
    if not isinstance(source_ref, str) or _SOURCE_REF_RE.fullmatch(source_ref) is None:
        return _refused("invalid_source")
    if not isinstance(request_excerpt, str):
        return _refused("invalid_excerpt")
    excerpt = _strip_control(request_excerpt)[:MAX_EXCERPT_CHARS]

    from .concurrency import command_lock_wait_seconds
    from .server import _store

    raised_at = datetime.now(UTC).isoformat()
    with _store.transaction(plan_name, command_lock_wait_seconds()) as acquired:
        if not acquired:
            return {"ok": False, "error": "locked"}
        if plan_name not in bound_records.list_plans(_store):
            return _refused("no_such_plan")
        manifest = _store.get_manifest(plan_name)
        if needs_guidance(manifest):
            return _refused("already_needed")
        manifest[GUIDANCE_KEY] = {
            "status": NEEDED,
            "questions": cleaned,
            "source": {"kind": source_kind, "ref": source_ref},
            "request_excerpt": excerpt,
            "raised_at": raised_at,
        }
        _store.save_manifest(plan_name, manifest)
        record_audit(
            plan_name,
            action=RAISED_ACTION,
            outcome="allowed",
            target={"plan": plan_name},
            detail={
                "question_count": len(cleaned),
                "source_kind": source_kind,
                "source_ref": source_ref,
            },
        )
        _notify_needs_guidance(plan_name, len(cleaned), raised_at)
    return {"ok": True, "status": NEEDED, "question_count": len(cleaned)}


def _refuse_resolve(plan_name: str, reason: str) -> dict[str, Any]:
    record_audit(
        plan_name,
        action=RESOLVED_ACTION,
        outcome="refused",
        target={"plan": plan_name},
        detail={"reason": reason},
    )
    return _refused(reason)


def _publish_resolved(plan_name: str) -> None:
    from .event_wiring import get_bus
    from .events import make_event

    get_bus().publish(
        make_event(GUIDANCE_RESOLVED_EVENT, plan_name, payload={"status": RESOLVED})
    )


def resolve_guidance(plan_name: str, note: str = "") -> dict[str, Any]:
    """Settle ``plan_name``'s guidance record so the plan can be approved.

    Authorized with ``plan:ingest``: the action a plan author holds to re-save
    the plan, so whoever may revise the plan may settle its open questions.
    Approval itself stays with whoever approves today. Refuses, with a reason
    token, a plan with no record and a record already resolved.
    """
    _validate_key(plan_name)

    from .authorization import authorize

    refusal = authorize("plan:ingest", plan_name=plan_name)
    if refusal is not None:
        return refusal
    if not isinstance(note, str):
        return _refused("invalid_note")
    cleaned_note = _strip_control(note).strip()
    if len(cleaned_note) > MAX_NOTE_CHARS:
        return _refused("invalid_note")

    from .concurrency import command_lock_wait_seconds
    from .server import _store

    with _store.transaction(plan_name, command_lock_wait_seconds()) as acquired:
        if not acquired:
            return {"ok": False, "error": "locked"}
        if plan_name not in bound_records.list_plans(_store):
            return _refused("no_such_plan")
        manifest = _store.get_manifest(plan_name)
        record = manifest.get(GUIDANCE_KEY)
        if not isinstance(record, dict):
            return _refuse_resolve(plan_name, "no_guidance")
        if record.get("status") != NEEDED:
            return _refuse_resolve(plan_name, "already_resolved")

        principal = current_principal()
        resolved_at = datetime.now(UTC).isoformat()
        manifest[GUIDANCE_KEY] = {
            **record,
            "status": RESOLVED,
            "resolved_by": principal.subject,
            "resolved_at": resolved_at,
            "note": cleaned_note,
        }
        _store.save_manifest(plan_name, manifest)
        record_audit(
            plan_name,
            action=RESOLVED_ACTION,
            outcome="allowed",
            target={"plan": plan_name},
            detail={"resolved_by": principal.subject},
        )
        _publish_resolved(plan_name)
    return {
        "ok": True,
        "status": RESOLVED,
        "resolved_by": principal.subject,
        "resolved_at": resolved_at,
    }
