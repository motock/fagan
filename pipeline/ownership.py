"""Plan ownership records (ADR 0006, ADR 0009).

An owner is one named principal; delegates are additional principals the owner
named. Both are stored on the manifest at ingest and default to today's
behaviour: the ingester owns the plan and delegates nothing.
"""

from __future__ import annotations

from typing import Any


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
