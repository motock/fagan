"""``POST /api/plans/{plan_name}/guidance/resolve``: settle a plan's guidance.

The route maps the service's result onto a status code and writes nothing
itself: :func:`pipeline.plan_guidance.resolve_guidance` owns the authorization,
the record change, the audit record and the bus event. The body is a fixed
detail per refusal reason, so no question, excerpt, principal or exception text
reaches a response.

Mounted under ``/api`` by :mod:`app.dashboard_mount`, so the dashboard's
authentication middleware covers it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from app import dashboard_refusal
from pipeline import parsers, plan_guidance

router = APIRouter()

_NOT_FOUND_DETAIL = "plan not found"

#: Status and fixed detail for each refusal reason the service returns. An
#: unrecognised reason is answered as the one opaque 404.
_REFUSALS: dict[str, tuple[int, str]] = {
    "no_such_plan": (404, _NOT_FOUND_DETAIL),
    "no_guidance": (409, "plan has no guidance to resolve"),
    "already_resolved": (409, "plan guidance is already resolved"),
    "invalid_note": (422, "note must be text of at most 500 characters"),
    "locked": (409, "plan is busy; retry"),
}


class ResolveBody(BaseModel):
    """The resolve request body: an optional note on the answer."""

    model_config = ConfigDict(extra="forbid")

    note: str = ""


@router.post("/plans/{plan_name}/guidance/resolve")
def post_plan_guidance_resolve(plan_name: str, body: ResolveBody) -> dict[str, Any]:
    """Settle the guidance a plan needs answered before it can be approved."""
    # The key flows into a filesystem path: reject it before any I/O, as the
    # same fixed 404 a missing plan gets.
    try:
        parsers._validate_key(plan_name)
    except ValueError:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL) from None

    result = plan_guidance.resolve_guidance(plan_name, body.note)
    dashboard_refusal.raise_if_forbidden(result)
    if result.get("ok") is not True:
        status, detail = _REFUSALS.get(result.get("error") or "", (404, _NOT_FOUND_DETAIL))
        raise HTTPException(status_code=status, detail=detail)

    return {
        "status": result.get("status"),
        "resolved_by": result.get("resolved_by"),
        "resolved_at": result.get("resolved_at"),
    }
