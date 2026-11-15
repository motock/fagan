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

from pipeline.actor import current_correlation_id, current_namespace
from pipeline.audit import append_host_audit
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    validate_namespace,
)

logger = logging.getLogger("pipeline.namespace_guard")

#: Reason tokens. A refusal carries exactly one of these.
NAMESPACE_MISMATCH = "namespace_mismatch"
NAMESPACE_MALFORMED = "namespace_malformed"
NAMESPACE_UNAVAILABLE = "namespace_unavailable"

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


def read_plan_namespace(plan_name: str, requested: object) -> tuple[str, str | None]:
    """Return ``(namespace to bind, refusal reason)``, reading the plan where *requested* says.

    Nothing requested, or the default namespace, reads the manifest exactly as
    before (``pipeline.persistence._plan_manifest``), so a default-only install
    is unchanged. Any other requested namespace reads the manifest from that
    namespace through the configured store's record view: the legacy file
    layout holds only the default namespace, so a store that cannot serve the
    requested namespace refuses rather than falling back to the default. A
    plan the requested namespace does not hold is a mismatch.
    """
    if requested is not None and not isinstance(requested, str):
        return current_namespace(), NAMESPACE_MALFORMED
    try:
        wanted = None if requested is None else validate_namespace(requested)
    except InvalidRecordKeyError:
        return current_namespace(), NAMESPACE_MALFORMED
    if wanted is None or wanted == DEFAULT_NAMESPACE:
        from pipeline.persistence import _plan_manifest

        return resolve_child_namespace(_plan_manifest(plan_name) or {}, wanted, current_namespace())
    try:
        manifest = _stored_manifest(wanted, plan_name)
    except Exception as exc:  # noqa: BLE001 - reported by class name, never the message
        logger.warning(
            "requested namespace could not be read",
            extra={"error_class": type(exc).__name__},
        )
        return current_namespace(), NAMESPACE_UNAVAILABLE
    if manifest is None:
        return current_namespace(), NAMESPACE_MISMATCH
    return resolve_child_namespace(manifest, wanted, wanted)


def _stored_manifest(namespace: str, plan_name: str) -> dict | None:
    """Return *plan_name*'s manifest as stored under *namespace*, or ``None``.

    Read through the configured store's record view, which validates the
    namespace itself and refuses one it does not serve.
    """
    from pipeline import server

    return server._store.records().get_manifest(namespace, plan_name)


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
