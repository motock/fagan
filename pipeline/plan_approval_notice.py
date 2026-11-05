"""Human-readable notices for the three plan-approval events.

``approval_notice_message`` is pure: it renders one sentence per event from
the plan manifest and, when an operator configured a dashboard, appends a
review link. It is the only place those sentences are worded, so the
ingest path (plan pending approval) and the decision path (approved /
changes requested) cannot drift apart.

Data minimization is an invariant here: the message carries counts plus the
approval digest, the decider and (for a pending plan) the warning total --
never story bodies, agent instructions, file lists, the manifest's
``repo_root``/``role_config``/``context``, nor the reviewer's ``reason``
text. The reviewer reads the reason in the review UI; a notice is delivered
over channels an operator may not control, so it names the plan and nothing
more. The link carries only the plan name as ``?plan=`` and never a token:
approval can never happen by following a link.

Both environment variables are read on every call (never cached at import),
because the service reads its configuration the same way and an operator
must be able to fix a bad value without restarting. A rejected value is
logged by variable name only -- the value itself never reaches a log.

``_notify_user`` is resolved through ``_ServerRef`` (the pattern used by
``pipeline/ingest.py`` and ``pipeline/service.py``) so this module never
imports ``pipeline.server``: ingest, plan_approval and the service modules
all import this one, and a direct server import would form a cycle.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote, urlsplit

from . import plan_review
from .service import _ServerRef

logger = logging.getLogger(__name__)

# The events that carry a plan-level approval notice. Anything else is a
# programming error at the emit site, not a notification to word differently.
APPROVAL_EVENTS = (
    "plan_pending_approval",
    "plan_approved",
    "plan_changes_requested",
)

PUBLIC_URL_ENV = "PIPELINE_DASHBOARD_PUBLIC_URL"
LINK_PATH_ENV = "PIPELINE_APPROVAL_LINK_PATH"

# Core ships no review page, so the default points at the dashboard root; an
# operator who supplies a review UI through the dashboard extension mount
# sets LINK_PATH_ENV to that UI's path.
_DEFAULT_LINK_PATH = "/"

# A link path is a plain path segment sequence: no scheme, no query, no
# traversal (``..``) and no empty segment (``//``).
_LINK_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")
_HEX_PREFIX_CHARS = 12
_RISKS = ("high", "medium", "low", "unset")

_notify_user = _ServerRef("_notify_user")


def _approval(manifest: dict) -> dict:
    """Return the manifest's ``approval`` block, or ``{}`` for a bad shape."""
    if not isinstance(manifest, dict):
        return {}
    approval = manifest.get("approval")
    return approval if isinstance(approval, dict) else {}


def _digest(manifest: dict) -> str:
    """Return the first 12 characters of ``approval.digest`` (``""`` if unset)."""
    digest = _approval(manifest).get("digest")
    return digest[:_HEX_PREFIX_CHARS] if isinstance(digest, str) else ""


def _risk_counts(manifest: dict) -> dict[str, int]:
    """Count the manifest's stories per risk level; a missing risk is unset."""
    counts = dict.fromkeys(_RISKS, 0)
    stories = manifest.get("stories") if isinstance(manifest, dict) else None
    if not isinstance(stories, dict):
        return counts
    for story in stories.values():
        if not isinstance(story, dict):
            continue
        risk = story.get("risk")
        counts[risk if risk in {"high", "medium", "low"} else "unset"] += 1
    return counts


def _warning_count(manifest: dict) -> int:
    """Return the total number of advisory warnings across every story."""
    total = 0
    for story_warnings in plan_review.review_warnings(manifest).values():
        if isinstance(story_warnings, list):
            total += len(story_warnings)
    return total


def _origin() -> str | None:
    """Return the validated dashboard origin, or ``None`` when unusable."""
    raw = os.environ.get(PUBLIC_URL_ENV, "")
    if not raw.strip():
        return None
    parts = urlsplit(raw.strip())
    if (
        parts.scheme != "https"
        or not parts.hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        logger.warning(
            "Ignoring %s: it must be a bare https origin (no path, credentials, "
            "query or fragment); omitting the approval review link",
            PUBLIC_URL_ENV,
        )
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _link_path() -> str | None:
    """Return the validated review-UI path, or ``None`` when unusable."""
    raw = os.environ.get(LINK_PATH_ENV, "")
    if not raw:
        return _DEFAULT_LINK_PATH
    if not _LINK_PATH_RE.match(raw) or ".." in raw or "//" in raw:
        logger.warning(
            "Ignoring %s: it must be a plain absolute path with no traversal "
            "or empty segment; omitting the approval review link",
            LINK_PATH_ENV,
        )
        return None
    return raw


def _review_link(plan_name: str) -> str | None:
    """Return the review-link URL for ``plan_name``, or ``None`` when unusable."""
    origin = _origin()
    if origin is None:
        return None
    path = _link_path()
    if path is None:
        return None
    return f"{origin}{path}?plan={quote(plan_name, safe='')}"


def approval_notice_message(plan_name: str, manifest: dict, event: str) -> str:
    """Render the notice sentence for one plan-approval ``event``.

    Raises ``ValueError`` naming ``event`` when it is not one of
    ``APPROVAL_EVENTS``. A review link is appended only when both
    environment settings yield a usable URL.
    """
    if event not in APPROVAL_EVENTS:
        raise ValueError(f"unknown plan approval event: {event}")

    approval = _approval(manifest)
    digest = _digest(manifest)
    if event == "plan_pending_approval":
        counts = _risk_counts(manifest)
        stories = sum(counts.values())
        message = (
            f"Plan {plan_name} is awaiting approval: {stories} stories "
            f"(risk: {counts['high']} high, {counts['medium']} medium, "
            f"{counts['low']} low, {counts['unset']} unset), "
            f"{_warning_count(manifest)} warnings, digest {digest}."
        )
    elif event == "plan_approved":
        decided_by = approval.get("decided_by") or "unknown"
        self_approved = "yes" if approval.get("self_approved") is True else "no"
        message = (
            f"Plan {plan_name} was approved by {decided_by} "
            f"(self-approved: {self_approved}), digest {digest}."
        )
    else:
        decided_by = approval.get("decided_by") or "unknown"
        message = (
            f"Changes were requested on plan {plan_name} by {decided_by}, "
            f"digest {digest}."
        )

    link = _review_link(plan_name)
    return f"{message} Review: {link}" if link else message


def notify_approval_event(plan_name: str, manifest: dict, event: str) -> None:
    """Deliver the notice for ``event``; never raise into the caller.

    The dedup key pins one notice per (event, plan, digest), so a retried
    ingest or decision does not notify twice. Failures are logged by
    exception type only -- the manifest may hold operator content.
    """
    try:
        message = approval_notice_message(plan_name, manifest, event)
        dedup_key = f"{event}:{plan_name}:{_digest(manifest)}"
        _notify_user(plan_name, message, event=event, dedup_key=dedup_key)
    except Exception as exc:  # noqa: BLE001 - notification must never break approval
        logger.warning(
            "Failed to emit plan approval notice (%s)", type(exc).__name__
        )
