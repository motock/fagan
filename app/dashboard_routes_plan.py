"""Plan-control (state-changing) routes for app/dashboard.py.

Pulled out of dashboard.py (which re-exports every name here and registers
each on its ``app``) purely to shrink that file. The handler bodies are unchanged.

``_service`` is resolved through ``app.dashboard`` at call time, so patching
``app.dashboard._service`` still reaches these routes; ``app.dashboard`` is
imported lazily to avoid a cycle.
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.responses import Response

from app.auth import refuse_chat_origin
from app.chat import REPO_URL_AND_WORKSPACE_ERROR
from app.dashboard_models import DecomposeRequest, IngestPlanRequest, SavePlanRequest
from app.dashboard_refusal import raise_if_forbidden
from pipeline.actor import current_correlation_id, current_namespace, current_principal
from pipeline.audit import append_host_audit
from pipeline.view_scope import can_view

logger = logging.getLogger("app.dashboard_routes_plan")

#: The plan list and every route that names a plan. Matching is deliberately
#: broad: a request this matches is only ever refused, never served differently.
PLAN_VIEW_PATH = re.compile(r"^/api/(?:config/)?plans(?:/|$)")

#: The one detail every view-scope refusal carries. The audit record says why.
VIEW_DENIED = "view_scope_denied"
_REASON_ERROR = "view_scope_error"
_AUDIT_ACTION = "plan_view:scope"
_DIGEST_CHARS = 16


def _dashboard():
    return importlib.import_module("app.dashboard")


class _DashboardServiceProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_dashboard()._service, name)


_service = _DashboardServiceProxy()


def save_plan(plan_name: str, request: SavePlanRequest):
    workspace = request.workspace if request.workspace is not None else _service.get_active_workspace()
    result = _service.save_plan(plan_name, request.plan_json, workspace)
    raise_if_forbidden(result)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result


def decompose_route(request: DecomposeRequest):
    if request.repo_url and request.workspace:
        raise HTTPException(status_code=400, detail=REPO_URL_AND_WORKSPACE_ERROR)
    if request.repo_url:
        # The repository is named by URL: the active workspace is not read.
        result = _service.decompose_plan(request.request, None, repo_url=request.repo_url)
    else:
        workspace = (
            request.workspace
            if request.workspace is not None
            else _service.get_active_workspace()
        )
        result = _service.decompose_plan(request.request, workspace)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result


def ingest_plan(plan_name: str, request: IngestPlanRequest | None = None, x_pipeline_origin: str | None = Header(default=None, alias="X-Pipeline-Origin")):
    refuse_chat_origin(x_pipeline_origin)
    if request is None:
        result = _service.ingest_plan(plan_name)
        raise_if_forbidden(result)
    else:
        result = _service.ingest_plan(plan_name, only_epics=request.only_epics, overwrite=request.overwrite)
        raise_if_forbidden(result)

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result


def advance_pipeline(plan_name: str) -> dict[str, Any]:
    """Delegates to _service.advance_pipeline(plan_name)."""
    result = _service.advance_pipeline(plan_name)
    raise_if_forbidden(result)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result


def advance_all_plans() -> dict[str, Any]:
    """Delegates to _service.advance_all_plans()."""
    result = _service.advance_all_plans()
    raise_if_forbidden(result)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result


def checkpoint(plan_name: str, story_key: str, body: dict[str, Any]) -> dict[str, Any]:
    """Delegates to _service.checkpoint(plan_name, story_key, step, summary, next_hint)."""
    step = body.get("step")
    summary = body.get("summary")
    if step is None or summary is None:
        raise HTTPException(
            status_code=400,
            detail="Both 'step' and 'summary' are required in the request body",
        )
    next_hint = body.get("next_hint", "")
    result = _service.checkpoint(plan_name, story_key, step, summary, next_hint)
    raise_if_forbidden(result)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result


def _view_denied(reason: str) -> JSONResponse:
    """Audit one view-scope refusal and answer it. The audit write cannot change the outcome."""
    subject = current_principal().subject.encode("utf-8", "surrogatepass")
    try:
        append_host_audit(
            action=_AUDIT_ACTION,
            outcome="denied",
            detail={
                "reason": reason,
                "subject": hashlib.sha256(subject).hexdigest()[:_DIGEST_CHARS],
                "correlation_id": current_correlation_id(),
            },
        )
    except Exception:  # noqa: BLE001 - an audit failure must not turn a refusal into a serve
        logger.error("view scope denial audit append failed reason=%s", reason)
    return JSONResponse(
        status_code=403,
        content={"detail": VIEW_DENIED, "correlation_id": current_correlation_id()},
    )


async def plan_view_gate(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Serve a plan route only when its namespace is in the principal's view scope.

    Plans are stored per namespace, so the request's namespace is the namespace
    every plan it reads or writes lives in. Any failure to answer the scope
    refuses the request rather than serving it.
    """
    if PLAN_VIEW_PATH.match(request.url.path) is None:
        return await call_next(request)
    try:
        visible = can_view(current_principal(), current_namespace())
    except Exception:  # noqa: BLE001 - a failing scope denies, never opens the view
        logger.error("view scope check failed reason=%s", _REASON_ERROR)
        return _view_denied(_REASON_ERROR)
    if not visible:
        return _view_denied(VIEW_DENIED)
    return await call_next(request)
