"""Plan ownership records (ADR 0006, ADR 0009).

An owner is one named principal; delegates are additional principals the owner
named. Both are stored on the manifest at ingest and default to today's
behaviour: the ingester owns the plan and delegates nothing.
"""

from __future__ import annotations

from typing import Any

from app.identity import LOCAL_OPERATOR

from .audit import record_audit


def ownership_from_plan(plan: dict[str, Any], ingested_by: str) -> dict[str, Any]:
    """Return the ``owner``/``delegates`` pair to persist for *plan*.

    ``owner`` defaults to whoever ingested the plan, so an existing plan that
    carries neither field behaves exactly as it does today. A plan that names
    an owner keeps it across re-ingests unless the plan names a different one.
    ``delegates`` is a list of subjects; a malformed value is a rejection rather
    than a silent empty list, because a dropped delegate silently removes
    someone's authority.
    """
    owner = plan.get("owner") or ingested_by
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("invalid owner: must be a non-empty string")
    delegates = plan.get("delegates") or []
    if not isinstance(delegates, list):
        # ValueError, not TypeError: the ingest path turns this into its
        # structured {"ok": False} refusal, which only catches ValueError.
        raise ValueError(  # noqa: TRY004 - ingest's refusal path catches ValueError
            "invalid delegates: must be a list of subjects"
        )
    cleaned: list[str] = []
    for delegate in delegates:
        if not isinstance(delegate, str) or not delegate.strip():
            raise ValueError("invalid delegates: every entry must be a subject")
        cleaned.append(delegate)
    return {"owner": owner, "delegates": cleaned}


def resolve_ownership(
    plan_name: str, plan: dict[str, Any], prior: dict[str, Any], actor: str
) -> dict[str, Any]:
    """Return the ``owner``/``delegates`` to persist for this ingest.

    Ownership is authority -- an owner may approve, reject and merge -- so the
    plan file, which ``save_plan`` writes for any caller, cannot be what grants
    it. On a first ingest (no prior manifest) the plan may name an owner and
    delegates, the owner defaulting to the ingester. On every later ingest the
    manifest's ownership stands unless the plan names a different one, and that
    change is allowed only for the current owner or the local operator. Any
    other caller gets a ``ValueError``, which ingest returns as its structured
    refusal. Every attempted change is audited, allowed or denied.
    """
    requested = ownership_from_plan(plan, actor)
    # Normalise the owner: a whitespace variant would produce a manifest no
    # principal can match, and once an ownership is set only the owner or the
    # local operator can repair it. ``ownership_from_plan`` keeps validating
    # the shape; normalisation belongs here, on the way to the manifest.
    requested["owner"] = requested["owner"].strip()
    if not prior:
        return requested
    current = {
        "owner": str(prior.get("owner") or ""),
        "delegates": list(prior.get("delegates") or []),
    }
    plan_owner = plan.get("owner")
    proposed = {
        "owner": str(plan_owner).strip() if plan_owner else current["owner"],
        "delegates": requested["delegates"]
        if "delegates" in plan
        else current["delegates"],
    }
    if proposed == current:
        return current
    allowed = actor == LOCAL_OPERATOR.subject or (
        bool(current["owner"]) and actor == current["owner"]
    )
    record_audit(
        plan_name,
        action="plan:ownership_change",
        outcome="allowed" if allowed else "denied",
        detail={
            "from_owner": current["owner"],
            "to_owner": proposed["owner"],
            "delegates_changed": proposed["delegates"] != current["delegates"],
        },
    )
    if not allowed:
        raise ValueError(
            "changing a plan's owner or delegates requires its current owner"
        )
    return proposed
