"""``GET /api/operator/dispatch``: the operator-only read of the dispatch hold.

Mounted under ``/api`` (see :func:`app.dashboard_mount.mount_routes`), so the
dashboard middleware authenticates the caller. The handler then requires an
operator: the local operator principal, or a principal holding
:data:`app.identity.ROLE_ADMIN`. Every other caller gets the same fixed 403
that the metrics route gives.

The body is the hold, the number of stories whose agent process is running,
and which stories they are. ``scope`` says whether the count covers every
namespace (``all_namespaces``) or only the default one (``default_namespace``)
when the store cannot list its namespaces. Setting or clearing the hold is not
here: it is written through the audited ``set_setting`` tool.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.identity import LOCAL_OPERATOR, ROLE_ADMIN
from pipeline import dispatch_hold
from pipeline.store import build_store

router = APIRouter()

_NOT_AUTHORIZED_DETAIL = "not authorized: operator"


def _is_operator(principal) -> bool:
    """Deny by default: only the local operator or an admin reads the dispatch state."""
    if principal is None:
        return False
    if principal == LOCAL_OPERATOR:
        return True
    return ROLE_ADMIN in getattr(principal, "roles", frozenset())


def dispatch_snapshot(store) -> dict:
    """The hold, the running-agent count and the running stories, read from *store*."""
    running = dispatch_hold.running_agents(store)
    stories = running["stories"]
    return {
        "hold": dispatch_hold.dispatch_held(store),
        "running": len(stories),
        "stories": stories,
        "scope": running["scope"],
    }


@router.get("/operator/dispatch")
def get_operator_dispatch(request: Request) -> dict:
    """Return the dispatch hold and running agents to an operator, or a fixed 403."""
    if not _is_operator(getattr(request.state, "principal", None)):
        raise HTTPException(status_code=403, detail=_NOT_AUTHORIZED_DETAIL)
    return dispatch_snapshot(build_store())
