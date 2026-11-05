"""Read-only plan-review route: ``GET /api/plans/{plan_name}/review``.

Serves the plan-approval payload an operator needs to review a plan before
deciding on it: the stored approval record, the plan's *current* content
digest (so a caller can tell the approval is stale), whether this viewer is the
plan's author, and an explicit projection of each story plus
:func:`pipeline.plan_review.review_warnings`.

Two properties are load-bearing and deliberately not delegated:

* **No existence oracle.** An invalid plan key, a missing manifest and a plan
  the caller may not see are one and the same 404 with one and the same body.
  Answering 403 for "exists but not yours" would confirm the plan exists.
* **Read-only.** The handler never writes: the stored approval digest is
  reported as stored, never replaced with the recomputed one.

The manifest is projected field by field; it is never returned whole.

``app.dashboard`` is imported inside the handler (never at module import) for
exactly the reason ``app/dashboard_routes_plan.py`` documents: importing it
here at module level would be a circular import.
"""
from __future__ import annotations

import importlib
from typing import Any

from fastapi import APIRouter, HTTPException

from app import policy
from pipeline import actor, authorization, parsers, plan_approval, plan_review

router = APIRouter()

#: The one and only "you cannot have this" detail. Fixed on purpose: the body
#: must not differ between "does not exist" and "not visible to you".
_NOT_FOUND_DETAIL = "plan not found"

#: The per-story fields the payload carries, in order.
_STORY_FIELDS = (
    "summary",
    "agent_instructions",
    "risk",
    "persona",
    "model",
    "backend",
    "files",
    "dependencies",
    "blocked_by",
    "preflight",
)


@router.get("/plans/{plan_name}/review")
def get_plan_review(plan_name: str) -> dict[str, Any]:
    """Return the review payload for *plan_name* when the caller may see it."""
    # 1. The key flows into a filesystem path: reject it before any I/O, and
    #    as the same fixed 404 so a malformed key is indistinguishable from a
    #    missing plan.
    try:
        parsers._validate_key(plan_name)
    except ValueError:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL) from None

    # 2. Load the manifest. ``get_manifest_or_none`` returns None for both a
    #    missing and an unreadable manifest, so neither can escape as a 500.
    service = importlib.import_module("app.dashboard")._service
    manifest = service.get_manifest_or_none(plan_name)

    # 3. Visibility, via the same policy the rest of the pipeline uses. Deny by
    #    default: an unreadable resource hides the plan.
    principal = actor.current_principal()
    visible = isinstance(manifest, dict) and policy.can_view(
        principal, authorization.resource_for(manifest, plan_name, None)
    )
    if not visible:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)

    # 4. Only now -- for a plan this caller may see -- do the gate checks run.
    #    Checking the gate first would report the gate's state for a plan the
    #    caller cannot see, and would answer 409 for a plan that does not exist.
    if not plan_approval.approval_enabled():
        raise HTTPException(status_code=409, detail="plan approval is not enabled")
    if not isinstance(manifest.get("approval"), dict):
        raise HTTPException(
            status_code=409, detail="plan has no approval record; re-ingest it"
        )

    return _review_payload(plan_name, manifest, principal)


def _review_payload(plan_name: str, manifest: dict, principal: Any) -> dict[str, Any]:
    """Project *manifest* into the review response for *principal*.

    Every field is copied out by name: no sub-dict or list is returned by
    reference and nothing is written back onto *manifest*.
    """
    approval = manifest.get("approval")
    if not isinstance(approval, dict):
        approval = {}
    current_digest = plan_approval.canonical_digest(manifest)

    raw_stories = manifest.get("stories")
    if not isinstance(raw_stories, dict):
        raw_stories = {}
    stories = []
    for key, story in raw_stories.items():
        if not isinstance(story, dict):
            continue
        item: dict[str, Any] = {"key": key}
        for field in _STORY_FIELDS:
            value = story.get(field)
            if isinstance(value, (list, dict)):
                value = type(value)(value)
            item[field] = value
        stories.append(item)

    role_config = manifest.get("role_config")
    if isinstance(role_config, dict):
        role_config = dict(role_config)

    return {
        "plan": plan_name,
        "approval": {
            "status": approval.get("status"),
            "digest": approval.get("digest"),
            "decided_by": approval.get("decided_by"),
            "at": approval.get("at"),
            "self_approved": approval.get("self_approved"),
            "reason": approval.get("reason"),
        },
        "current_digest": current_digest,
        # Compares the STORED digest with the digest of the plan as it is now.
        # The stored value is read, never rewritten.
        "digest_current": approval.get("digest") == current_digest,
        # What ``_decide_plan`` records as ``self_approved``: the deciding
        # principal compared against the plan's ingester by subject.
        "viewer_is_author": principal.subject == manifest.get("ingested_by"),
        "owner": manifest.get("owner"),
        "role_config": role_config,
        "stories": stories,
        "warnings": plan_review.review_warnings(manifest),
    }
