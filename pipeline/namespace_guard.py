"""Resolve and check the namespace a child advance or review job runs under.

A plan's own manifest declares the namespace it belongs to (``namespace``);
absent means the default namespace. A process that is handed a namespace
explicitly -- as an argument or a request field set by its parent -- may run
only under that same namespace. A mismatch, or a malformed requested value,
is refused with a reason token and audited. Nothing is advanced on a refusal.

Identifiers (plan names, namespace values) never appear in the audit detail or
the log line: only the reason token and the correlation id.
"""

from __future__ import annotations

import logging

from pipeline.actor import current_correlation_id
from pipeline.audit import append_host_audit
from pipeline.store_records import InvalidRecordKeyError, validate_namespace

logger = logging.getLogger("pipeline.namespace_guard")

#: Reason tokens. A refusal carries exactly one of these.
NAMESPACE_MISMATCH = "namespace_mismatch"
NAMESPACE_MALFORMED = "namespace_malformed"

_AUDIT_ACTION = "namespace:advance"


def resolve_child_namespace(
    manifest: dict, requested: object, stored_in: str
) -> tuple[str, str | None]:
    """Return ``(namespace to bind, refusal reason)`` for one plan.

    *stored_in* is the namespace the manifest was read from; a manifest that
    names no namespace belongs to that one. *requested* is ``None`` when the
    parent named nothing, which binds the plan's own namespace. A malformed
    manifest namespace raises :class:`InvalidRecordKeyError`, as it always has.
    """
    named = manifest.get("namespace") or None
    declared = stored_in if named is None else validate_namespace(named)
    if requested is None:
        return declared, None
    if not isinstance(requested, str):
        return declared, NAMESPACE_MALFORMED
    try:
        requested = validate_namespace(requested)
    except InvalidRecordKeyError:
        return declared, NAMESPACE_MALFORMED
    if requested != declared:
        return declared, NAMESPACE_MISMATCH
    return declared, None


def audit_refusal(reason: str) -> None:
    """Record one refused advance. Must run outside any namespace binding.

    The host chain is the only chain a refusal is written to, so the caller
    unbinds first. An audit failure is logged and never changes the refusal.
    """
    try:
        append_host_audit(
            action=_AUDIT_ACTION,
            outcome="denied",
            detail={"reason": reason, "correlation_id": current_correlation_id()},
        )
    except Exception:  # noqa: BLE001 - an audit failure must not turn a refusal into a run
        logger.error("namespace refusal audit append failed reason=%s", reason)
