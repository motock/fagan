"""Story-control (state-changing) routes for app/dashboard.py.

Pulled out of dashboard.py (which re-exports every name here and registers
each on its ``app``) purely to shrink that file. The handler bodies are unchanged.

``_service`` is resolved through ``app.dashboard`` at call time, so patching
``app.dashboard._service`` still reaches these routes; ``app.dashboard`` is
imported lazily to avoid a cycle.
"""
from __future__ import annotations

import importlib
from typing import Any

from fastapi import HTTPException

from app.dashboard_models import StoryPatchBody, StoryStatusBody


def _dashboard():
    return importlib.import_module("app.dashboard")


class _DashboardServiceProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_dashboard()._service, name)


_service = _DashboardServiceProxy()


def dispatch_story_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.dispatch_story(plan_name, story_key)."""
    result = _service.dispatch_story(plan_name, story_key)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error during dispatch"),
        )
    return result


def start_story_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.mark_story_in_progress(plan_name, story_key)."""
    result = _service.mark_story_in_progress(plan_name, story_key)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error when starting story"),
        )
    return result


def interrupt_story_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.interrupt_story(plan_name, story_key)."""
    result = _service.interrupt_story(plan_name, story_key)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error when interrupting story"),
        )
    return result


def set_story_status_route(
    plan_name: str, story_key: str, body: StoryStatusBody
) -> dict[str, Any]:
    """Delegates to _service.set_story_status(plan_name, story_key, status)."""
    result = _service.set_story_status(plan_name, story_key, body.status)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error when setting story status"),
        )
    return result


def patch_story_route(
    plan_name: str, story_key: str, body: StoryPatchBody
) -> dict[str, Any]:
    """Delegates to _service.patch_story(plan_name, story_key, fields=...)."""
    fields = body.model_dump(exclude_unset=True)
    result = _service.patch_story(plan_name, story_key, fields=fields)
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Unknown error when patching story"),
        )
    return result


def review_story_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.review_story(plan_name, story_key)."""
    result = _service.review_story(plan_name, story_key)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error during review"),
        )
    return result


def approve_merge_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.approve_merge(plan_name, story_key)."""
    result = _service.approve_merge(plan_name, story_key)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error during approve_merge"),
        )
    return result


def mark_story_done_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.mark_story_done(plan_name, story_key)."""
    result = _service.mark_story_done(plan_name, story_key)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error when marking story as done"),
        )
    return result
