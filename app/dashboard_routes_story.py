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

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse

from app.auth import refuse_chat_origin
from app.dashboard_models import StoryPatchBody, StoryStatusBody
from app.dashboard_refusal import raise_if_forbidden


def _dashboard():
    return importlib.import_module("app.dashboard")


class _DashboardServiceProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_dashboard()._service, name)


_service = _DashboardServiceProxy()


def dispatch_story_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.dispatch_story(plan_name, story_key)."""
    result = _service.dispatch_story(plan_name, story_key)
    raise_if_forbidden(result)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error during dispatch"),
        )
    return result


def start_story_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.mark_story_in_progress(plan_name, story_key)."""
    result = _service.mark_story_in_progress(plan_name, story_key)
    raise_if_forbidden(result)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error when starting story"),
        )
    return result


def interrupt_story_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.interrupt_story(plan_name, story_key)."""
    result = _service.interrupt_story(plan_name, story_key)
    raise_if_forbidden(result)
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
    raise_if_forbidden(result)
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
    raise_if_forbidden(result)
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Unknown error when patching story"),
        )
    return result


def review_story_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.review_story(plan_name, story_key)."""
    result = _service.review_story(plan_name, story_key)
    raise_if_forbidden(result)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error during review"),
        )
    return result


def approve_merge_route(
    plan_name: str,
    story_key: str,
    x_pipeline_origin: str | None = Header(default=None, alias="X-Pipeline-Origin"),
) -> dict[str, Any]:
    """Delegates to _service.approve_merge(plan_name, story_key).

    A retriable refusal (the plan stayed busy past the lock wait) is a
    conflict with the current state, not a missing story, so it answers 409.
    The chat model may propose a merge approval but never send one: the chat
    origin is refused here, so only the person's own Run click gets through.
    """
    refuse_chat_origin(x_pipeline_origin)
    result = _service.approve_merge(plan_name, story_key)
    raise_if_forbidden(result)
    if not result.get("ok"):
        status_code = 409 if result.get("retriable") is True else 404
        raise HTTPException(
            status_code=status_code,
            detail=result.get("error", "Unknown error during approve_merge"),
        )
    return result


def mark_story_done_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """Delegates to _service.mark_story_done(plan_name, story_key)."""
    result = _service.mark_story_done(plan_name, story_key)
    raise_if_forbidden(result)
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error", "Unknown error when marking story as done"),
        )
    return result


def _parked_story_or_404(plan_name: str, story_key: str) -> dict[str, Any]:
    """The story dict, or a 404; a malformed key is indistinguishable from a missing one."""
    from pipeline import parsers

    try:
        parsers._validate_key(plan_name)
        parsers._validate_key(story_key)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found") from None
    manifest = _service.get_manifest_or_none(plan_name)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"No manifest for plan '{plan_name}'")
    story = (manifest.get("stories") or {}).get(story_key)
    if not isinstance(story, dict):
        raise HTTPException(status_code=404, detail=f"No story '{story_key}' in plan '{plan_name}'")
    return story


def get_park_options_route(plan_name: str, story_key: str) -> dict[str, Any]:
    """The options prepared for a parked story, or ``preparing: true`` while none exist."""
    from pipeline.park_option_choice import latest_options_record
    from pipeline.park_options import _digest

    story = _parked_story_or_404(plan_name, story_key)
    record = latest_options_record(plan_name, story_key)
    if record is None:
        return {"ok": True, "options": [], "preparing": True}
    return {
        "ok": True,
        "options": record.get("options") or [],
        "recommended": record.get("recommended"),
        "stale": record.get("parked_reason_digest") != _digest(story.get("parked_reason")),
        "park_class": record.get("park_class"),
    }


def choose_park_option_route(plan_name: str, story_key: str, option_id: str) -> Any:
    """Carry out the chosen option. Refusals are 409; the caller must hold ``decision:answer``."""
    from pipeline.authorization import authorize
    from pipeline.park_option_choice import choose_park_option

    _parked_story_or_404(plan_name, story_key)
    if authorize("decision:answer", plan_name=plan_name) is not None:
        raise HTTPException(
            status_code=403,
            detail=f"not authorized: decision:answer on plan {plan_name!r}",
        )
    result = choose_park_option(plan_name, story_key, option_id)
    if not result.get("ok"):
        return JSONResponse(status_code=409, content={"ok": False, "error": result["error"]})
    return result
