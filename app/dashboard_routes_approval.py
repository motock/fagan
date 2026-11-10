"""Plan-review routes: reading a plan, and deciding on it.

``GET /api/plans/{plan_name}/review`` serves the plan-approval payload an
operator needs to review a plan before deciding on it: the stored approval
record, the plan's *current* content digest (so a caller can tell the approval
is stale), whether this viewer is the plan's author, and an explicit projection
of each story plus :func:`pipeline.plan_review.review_warnings`.

``POST .../review/approve`` and ``POST .../review/request-changes`` decide: the
caller sends the digest it was shown, and a decision made against any other
digest is refused. Those two routes require a verified login (``auth_method ==
"oidc"``) and policy enforcement, and they write nothing themselves -- the
service owns the decision record, the audit record and the notification.

The GET's load-bearing properties are deliberately not delegated:

* **No existence oracle.** An invalid plan key, a missing manifest and a plan
  the caller may not see are one and the same 404 with one and the same body.
  Answering 403 for "exists but not yours" would confirm the plan exists.
* **Read-only.** The GET handler never writes: the stored approval digest is
  reported as stored, never replaced with the recomputed one.

The manifest is projected field by field; it is never returned whole, and no
response in this module carries the principal, a filesystem path or exception
text.

``app.dashboard`` is imported inside the handlers (never at module import) for
exactly the reason ``app/dashboard_routes_plan.py`` documents: importing it
here at module level would be a circular import.
"""
from __future__ import annotations

import importlib
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import dashboard_refusal, policy
from pipeline import actor, audit, authorization, parsers, plan_approval, plan_review

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


# --------------------------------------------------------------------------- #
# Deciding a plan: POST /review/approve and POST /review/request-changes
# --------------------------------------------------------------------------- #

#: Every deciding body carries the digest the caller was shown: 64 lowercase
#: hex characters. Declared as a pydantic ``pattern`` (the Rust regex crate,
#: where ``$`` is end-of-string only), never hand-rolled: in Python
#: ``re.match(r"^[0-9a-f]{64}$", s)`` also accepts a 65th trailing newline.
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

#: The longest reason a request-changes body may carry, measured AFTER
#: stripping: the caller may pad the reason with whitespace.
_REASON_MAX = 2000

#: The one refusal that tells a shared-key or local-operator caller to use the
#: MCP ``approve_plan`` tool instead, and the gate refusal that says why no
#: decision can be authorized at all. Fixed text only: no principal, path or
#: exception text may reach a response body.
_LOGIN_REQUIRED_DETAIL = "dashboard approval requires login"
_ENFORCEMENT_DISABLED_DETAIL = "policy enforcement is disabled"

#: A decision the store could not acquire the plan for. Retryable.
_BUSY_DETAIL = "plan is busy; retry"

#: The service's fixed stale-digest text, mirrored so the body never depends on
#: whatever else the service's result happens to carry.
_STALE_DIGEST_DETAIL = "plan changed since it was displayed; reload before deciding"

#: The audit action a refusal before any decision records. It names the state a
#: decidable plan is in — ``pipeline.plan_approval`` and the spec call it
#: ``pending_approval`` — never the requested verb, because the refused request
#: may have been an approval or a change request.
_PENDING_APPROVAL_ACTION = "plan.pending_approval"


class ApproveBody(BaseModel):
    """The approve request body: the digest the approver was shown."""

    model_config = ConfigDict(extra="forbid")

    digest: str = Field(pattern=_DIGEST_PATTERN)


class RequestChangesBody(BaseModel):
    """The request-changes body: the digest shown plus a non-empty reason."""

    model_config = ConfigDict(extra="forbid")

    digest: str = Field(pattern=_DIGEST_PATTERN)
    reason: str

    @field_validator("reason")
    @classmethod
    def _strip_and_bound(cls, value: str) -> str:
        """Strip the reason, then apply the 1..2000 character bound.

        Stripping first is load-bearing both ways: ``"   "`` is empty (so 422,
        not an empty reason stored on the decision) and a 2000-character reason
        the caller padded is still 2000 characters (so 200, not 422).
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be empty")
        if len(stripped) > _REASON_MAX:
            raise ValueError(f"reason must be at most {_REASON_MAX} characters")
        return stripped


def _decide(plan_name: str, *, status: str, digest: str, reason: str = "") -> dict[str, Any]:
    """Apply one decision for *plan_name* and return its public projection.

    The checks are ordered fail-closed, and the order is part of the contract:

    1. the principal must be a verified login (``auth_method == "oidc"``);
    2. policy enforcement must be on;
    3. only then is the plan name validated;
    4. only then does the service decide.

    Any other order would both authorize a decision nobody can be held to and
    turn a refusal into an existence oracle for a plan the caller cannot see.
    The response never carries the principal, a path or exception text, and the
    decision record, audit record and notification are the service's to write:
    duplicating them here would decide twice.
    """
    # 1. A dashboard decision needs a login. Shared-key and local-operator
    #    callers keep using the MCP ``approve_plan`` tool, where the operator is
    #    the deployment rather than a subject anyone can attribute the decision
    #    to.
    #
    #    The key is checked for *audit safety* here, not answered yet: it flows
    #    into the audit stream's filename, so an invalid key must never name a
    #    file. Refusing to audit is not observable — the response below is the
    #    same either way — and the 404 for an invalid key still comes after the
    #    principal and gate checks, in step 3.
    try:
        parsers._validate_key(plan_name)
    except ValueError:
        key_is_safe = False
    else:
        key_is_safe = True

    principal = actor.current_principal()
    if getattr(principal, "auth_method", None) != "oidc":
        if key_is_safe:
            audit.record_audit(
                plan_name,
                action=_PENDING_APPROVAL_ACTION,
                outcome="refused",
                target={"plan": plan_name},
                detail={"reason": "unverified_principal"},
            )
        raise HTTPException(status_code=403, detail=_LOGIN_REQUIRED_DETAIL)

    # 2. With enforcement off every action is allowed, so an "approval" would
    #    be a record of nothing. Refuse before the plan is even named.
    if not authorization.enforcement_enabled():
        if key_is_safe:
            audit.record_audit(
                plan_name,
                action=_PENDING_APPROVAL_ACTION,
                outcome="refused",
                target={"plan": plan_name},
                detail={"reason": "policy_enforcement_disabled"},
            )
        raise HTTPException(status_code=403, detail=_ENFORCEMENT_DISABLED_DETAIL)

    # 3. The key flows into a filesystem path: reject it before any I/O, as the
    #    same fixed 404 a missing plan gets.
    if not key_is_safe:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)

    # 4. Decide. The service owns the transition, the authorization check, the
    #    digest comparison, the decision record, the audit record and the
    #    notification; this route only maps its result onto a status code.
    service = importlib.import_module("app.dashboard")._service

    # The service authorizes before it looks for the manifest, so a missing
    # plan would surface as the same 403 a real-but-unauthorized plan gets.
    # Answer the documented 404 here instead, before that check can mask it.
    # Nothing is written and the body is this module's one fixed 404 text.
    if service.get_manifest_or_none(plan_name) is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)

    if status == "approved":
        result = service.approve_plan(plan_name, expected_digest=digest)
    else:
        result = service.reject_plan(plan_name, reason, expected_digest=digest)

    dashboard_refusal.raise_if_forbidden(result)

    if not isinstance(result, dict):
        raise HTTPException(status_code=409, detail=_STALE_DIGEST_DETAIL)

    skipped = result.get("skipped")
    if skipped:
        # No decision record was written, so retrying is safe and expected.
        detail = _BUSY_DETAIL if skipped == "locked" else "plan approval is not enabled"
        raise HTTPException(status_code=409, detail=detail)

    if result.get("ok") is not True:
        if result.get("stale_digest"):
            error = result.get("error")
            raise HTTPException(
                status_code=409,
                detail=error if isinstance(error, str) and error else _STALE_DIGEST_DETAIL,
            )
        if result.get("outdated_digest_version"):
            # The record was written under another digest version: the plan
            # needs a fresh approval, not a decision against content the
            # caller may never have been shown. 409, never the opaque 404.
            raise HTTPException(
                status_code=409,
                detail=plan_approval._OUTDATED_DIGEST_VERSION_ERROR,
            )
        if result.get("error") == plan_approval._DIGEST_MISMATCH_ERROR:
            raise HTTPException(
                status_code=409, detail=plan_approval._DIGEST_MISMATCH_ERROR
            )
        # The only remaining refusal the service produces is a missing plan: it
        # is reported as the same opaque 404 the rest of this module uses.
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)

    # The projection is copied out field by field: the service result also
    # carries the plan name, the correlation id and the decider's role.
    return {
        "status": result.get("status"),
        "digest": result.get("digest"),
        "decided_by": result.get("decided_by"),
        "at": result.get("at"),
        "self_approved": result.get("self_approved"),
    }


@router.post("/plans/{plan_name}/review/approve")
def post_plan_review_approve(plan_name: str, body: ApproveBody) -> dict[str, Any]:
    """Approve *plan_name* against the digest the caller was shown."""
    return _decide(plan_name, status="approved", digest=body.digest)


@router.post("/plans/{plan_name}/review/request-changes")
def post_plan_review_request_changes(
    plan_name: str, body: RequestChangesBody
) -> dict[str, Any]:
    """Request changes on *plan_name* against the digest the caller was shown."""
    return _decide(
        plan_name,
        status="changes_requested",
        digest=body.digest,
        reason=body.reason,
    )


# A GET on a deciding path is refused explicitly, never by omission. Without
# these routes the app's static mount (``StaticFiles(html=True)`` at ``/``)
# would answer a 404 for the path -- indistinguishable from a mistyped URL, and
# one future catch-all route away from answering 200 to a link someone clicked.
@router.get("/plans/{plan_name}/review/approve", include_in_schema=False)
@router.get("/plans/{plan_name}/review/request-changes", include_in_schema=False)
def get_plan_review_decision(plan_name: str) -> dict[str, Any]:
    """Refuse a GET on a deciding route: a link never decides a plan."""
    raise HTTPException(status_code=405, detail="method not allowed")
