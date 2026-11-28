"""Standing approval: the scheduler records a named person's approval (sap-01).

Scheduled intake plans are otherwise left waiting for a signed-in approver.
``approve_standing`` lets the scheduler record an approval on behalf of one
named person, but only when that person may still approve for the plan's team
(the caller's ``still_valid`` answers that at the moment of use).

It is an in-process call and is deliberately not an MCP tool, HTTP route or
CLI command. It is off unless ``PIPELINE_STANDING_APPROVAL`` is exactly ``1``
and it changes nothing about the plan-approval gate, the high-risk merge hold
or the usage gate: it only moves a ``pending_approval`` record to ``approved``.

A refusal writes no manifest and no decision and records one ``refused`` audit
event whose only detail is a reason token. The approver subject, the digest
and the standing id are never placed in a refusal.

Policy for ``approver_subject`` is not evaluated here: that person is not
signed in and core holds no membership or role data for them (tenancy lives
in the enterprise overlay). Whether they may still approve for the plan's team
is therefore the caller's ``still_valid`` re-check, which is mandatory, runs
under the plan lock, and refuses on anything but exactly ``True``.
"""

import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.identity import LOCAL_OPERATOR

from .actor import actor_id, current_correlation_id, current_principal, transport_bound
from .audit import record_audit
from .plan_approval import (
    APPROVAL_DIGEST_VERSION,
    approval_enabled,
    canonical_digest,
    digest_version_is_current,
)

logger = logging.getLogger(__name__)

STANDING_ENV = "PIPELINE_STANDING_APPROVAL"

_STANDING_ID_RE = re.compile(r"[A-Za-z0-9_.:\-]{1,80}")
_APPROVER_MAX_LENGTH = 512
_ROLE = "standing"
_AUDIT_ACTION = "plan.approved"


def approve_standing(
    plan_name: str,
    *,
    standing_id: str,
    approver_subject: str,
    expected_digest: str,
    still_valid: Callable[[], Any],
) -> dict[str, Any]:
    """Record an approval of ``plan_name`` on ``approver_subject``'s behalf.

    ``expected_digest`` is required: the caller must name the digest it checked.
    ``still_valid`` is called once, inside the plan lock, and must return exactly
    ``True`` for the approval to stand; any other value or an exception refuses.
    """
    if os.environ.get(STANDING_ENV) != "1":
        return {"ok": False, "error": "standing approval is disabled"}

    if not _plan_name_is_valid(plan_name):
        return {"ok": False, "error": "invalid plan name", "reason": "bad_argument"}
    if not _gate_is_on():
        return _refuse(plan_name, "gate_off", "plan approval is disabled")
    if transport_bound() or current_principal() is not LOCAL_OPERATOR:
        return _refuse(plan_name, "not_scheduler", "only the scheduler may approve")
    if not _arguments_are_valid(
        standing_id, approver_subject, expected_digest, still_valid
    ):
        return _refuse(plan_name, "bad_argument", "invalid standing approval arguments")

    from .authorization import authorize
    from .concurrency import command_lock_wait_seconds
    from .server import _store

    # authorize records a denial itself; record_allowed=False keeps the allowed
    # case to the single plan.approved record written below.
    refusal = authorize("plan:approve", plan_name=plan_name, record_allowed=False)
    if refusal is not None:
        return {"ok": False, "error": "not authorized", "reason": "not_authorized"}

    wait_seconds = command_lock_wait_seconds()
    with _store.transaction(plan_name, wait_seconds) as acquired:
        if not acquired:
            return _refuse(plan_name, "locked", "plan is locked; retry later")
        return _decide_under_lock(
            plan_name,
            _store,
            standing_id=standing_id,
            approver_subject=approver_subject,
            expected_digest=expected_digest,
            still_valid=still_valid,
        )


def _decide_under_lock(
    plan_name: str,
    store: Any,
    *,
    standing_id: str,
    approver_subject: str,
    expected_digest: str,
    still_valid: Callable[[], Any],
) -> dict[str, Any]:
    """Apply the approval once the plan lock is held. Every check is re-read here."""
    from .bound_records import list_plans
    from .plan_approval_notice import notify_approval_event
    from .plan_guidance import needs_guidance
    from .server import _append_decision

    if plan_name not in list_plans(store):
        return _refuse(plan_name, "no_such_plan", f"No such plan {plan_name!r}")
    manifest = store.get_manifest(plan_name)
    previous = manifest.get("approval")
    if not isinstance(previous, dict):
        previous = {}
    if previous.get("status") != "pending_approval":
        return _refuse(plan_name, "not_pending", "plan is not awaiting approval")
    if needs_guidance(manifest):
        return _refuse(
            plan_name,
            "guidance_needed",
            "plan needs guidance before it can be approved",
        )
    if not digest_version_is_current(previous):
        return _refuse(
            plan_name,
            "outdated_digest_version",
            "plan approval was recorded under an older digest version",
        )
    digest = canonical_digest(manifest)
    if previous.get("digest") != digest or expected_digest != digest:
        return _refuse(plan_name, "digest_mismatch", "plan content changed")
    if not _still_valid(still_valid):
        return _refuse(
            plan_name, "standing_invalid", "standing approval is no longer valid"
        )

    warnings_seen = previous.get("warnings_seen")
    if not isinstance(warnings_seen, list):
        warnings_seen = []
    # Same rule as PlanApprovalMixin._decide_plan, with the named approver in
    # place of the principal: a plan with no recorded ingester counts as self-approved.
    ingested_by = manifest.get("ingested_by") or approver_subject
    self_approved = approver_subject == ingested_by
    actor = actor_id()
    record: dict[str, Any] = {
        "plan": plan_name,
        "status": "approved",
        "digest": digest,
        "digest_version": APPROVAL_DIGEST_VERSION,
        "decided_by": approver_subject,
        "recorded_by": actor,
        "at": datetime.now(UTC).isoformat(),
        "correlation_id": current_correlation_id(),
        "role": _ROLE,
        "self_approved": self_approved,
        "warnings_seen": list(warnings_seen),
        "standing_id": standing_id,
    }
    manifest["approval"] = {**previous, **record}
    store.save_manifest(plan_name, manifest)
    _append_decision(plan_name, record)
    record_audit(
        plan_name,
        action=_AUDIT_ACTION,
        outcome="allowed",
        target={"plan": plan_name},
        detail={
            "digest": digest,
            "decided_by": approver_subject,
            "actor": actor,
            "role": _ROLE,
            "self_approved": self_approved,
            "standing_id": standing_id,
        },
    )
    notify_approval_event(plan_name, manifest, "plan_approved")
    return {"ok": True, **record}


def _plan_name_is_valid(plan_name: object) -> bool:
    from .parsers import _validate_key

    if not isinstance(plan_name, str):
        return False
    try:
        _validate_key(plan_name)
    except ValueError:
        return False
    return True


def _gate_is_on() -> bool:
    """Read the plan-approval gate; an unrecognised value counts as off."""
    try:
        return approval_enabled()
    except ValueError:
        return False


def _arguments_are_valid(
    standing_id: object,
    approver_subject: object,
    expected_digest: object,
    still_valid: object,
) -> bool:
    if not isinstance(standing_id, str) or not _STANDING_ID_RE.fullmatch(standing_id):
        return False
    if not isinstance(approver_subject, str) or not approver_subject.strip():
        return False
    if len(approver_subject) > _APPROVER_MAX_LENGTH:
        return False
    if not isinstance(expected_digest, str):
        return False
    return callable(still_valid)


def _still_valid(still_valid: Callable[[], Any]) -> bool:
    """Return whether the caller's re-check says exactly ``True``.

    An exception refuses. Its message is never read: it can carry anything.
    """
    try:
        return still_valid() is True
    except Exception as exc:  # noqa: BLE001 - any failure of the re-check refuses
        logger.warning(
            "standing approval re-check raised; refusing",
            extra={"error_class": type(exc).__name__},
        )
        return False


def _refuse(plan_name: str, reason: str, error: str) -> dict[str, Any]:
    """Record one refused audit event carrying only ``reason``; return the refusal."""
    record_audit(
        plan_name,
        action=_AUDIT_ACTION,
        outcome="refused",
        target={"plan": plan_name},
        detail={"reason": reason},
    )
    return {"ok": False, "error": error, "reason": reason}
