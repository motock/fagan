"""Plan-control (state-changing) routes for app/dashboard.py.

Pulled out of dashboard.py (which re-exports every name here and registers
each on its ``app``) purely to shrink that file. The handler bodies are unchanged.

``_service`` is resolved through ``app.dashboard`` at call time, so patching
``app.dashboard._service`` still reaches these routes; ``app.dashboard`` is
imported lazily to avoid a cycle.
"""
from __future__ import annotations

import importlib
from typing import Any

from fastapi import Header, HTTPException

from app.auth import refuse_chat_origin
from app.dashboard_models import DecomposeRequest, IngestPlanRequest, SavePlanRequest
from app.dashboard_refusal import raise_if_forbidden


def _dashboard():
    return importlib.import_module("app.dashboard")


class _DashboardServiceProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_dashboard()._service, name)


_service = _DashboardServiceProxy()


def save_plan(plan_name: str, request: SavePlanRequest):
    workspace = request.workspace if request.workspace is not None else _service.get_active_workspace()
    result = _service.save_plan(plan_name, request.plan_json, workspace)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result


def decompose_route(request: DecomposeRequest):
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
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result


def advance_all_plans() -> dict[str, Any]:
    """Delegates to _service.advance_all_plans()."""
    result = _service.advance_all_plans()
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
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
    return result
