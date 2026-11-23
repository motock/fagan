"""``resolve_plan_guidance``: the MCP tool that settles a plan's guidance record.

A plan that needs a conversation before it can be approved carries a guidance
record (see :mod:`pipeline.plan_guidance`). This is the tool a plan author calls
once the open questions are answered. It is registered with the lifecycle tools
in :mod:`pipeline.server_tools_lifecycle`, like ``record_decision``; the policy
check and the record change live in :func:`pipeline.plan_guidance.resolve_guidance`.
"""

from __future__ import annotations

from typing import Any

from .plan_guidance import resolve_guidance


def resolve_plan_guidance(plan_name: str, note: str = "") -> dict[str, Any]:
    """Settle the open questions a plan needs answered before it can be approved.

    Authorized as a plan author (``plan:ingest``). Refuses a plan with no
    guidance record and a record that is already resolved. ``note`` is an
    optional summary of the answer, up to 500 characters. Approval is a separate
    decision and still needs an approver.
    """
    return resolve_guidance(plan_name, note)
