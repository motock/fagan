"""Read one story's pull-request CI result: a single snapshot, never a poll.

The merge gate reads CI through the forge adapter. This tool answers the same
question for a remote client: which checks ran on the story's pull request,
what state they are in, and -- when they failed -- a bounded excerpt of the
failing log. It is read-only and takes no branch, sha, repository or path
argument: the branch is always ``agent/<story_key>`` and the repository is the
one the plan is scoped to.

Every string returned passes through :func:`redact` before it is cut, so a
secret cannot be sliced in half and escape the masking patterns.
"""

from __future__ import annotations

import logging
from typing import Any

from .actor import current_correlation_id
from .ci import _aggregate_ci_states, _scoped_repo_cwd
from .forge import FAIL, NONE, PENDING, get_forge
from .module_ref import _ModuleRef
from .story_evidence import NOT_FOUND, redact

logger = logging.getLogger(__name__)

_store = _ModuleRef("pipeline.server", "_store")
_validate_key = _ModuleRef("pipeline.server", "_validate_key")
_scoped_repo_root = _ModuleRef("pipeline.server", "_scoped_repo_root")

#: Most checks one reply carries; ``truncated`` is set when there are more.
CHECKS_MAX = 100
#: Longest check name returned.
CHECK_NAME_MAX = 200
#: Longest failing-log excerpt returned.
EXCERPT_MAX = 4000


def _story(manifest: object, story_key: str) -> dict[str, Any] | None:
    """Return the story entry from *manifest*, or None when it is not there."""
    stories = manifest.get("stories") if isinstance(manifest, dict) else None
    if not isinstance(stories, dict):
        return None
    story = stories.get(story_key)
    return story if isinstance(story, dict) else None


def _checks(snapshot: dict[str, str]) -> tuple[list[dict[str, str]], bool]:
    """Return the snapshot as name-sorted checks, at most CHECKS_MAX, and a cut flag."""
    ordered = sorted(snapshot.items(), key=lambda item: str(item[0]))
    checks = [
        {"name": redact(str(name))[:CHECK_NAME_MAX], "state": state}
        for name, state in ordered[:CHECKS_MAX]
    ]
    return checks, len(ordered) > CHECKS_MAX


def _state(forge: Any, repo_root: str, snapshot: dict[str, str]) -> str:
    """Return the aggregate state; an empty snapshot is pending or none, as the gate reads it."""
    if not snapshot:
        return PENDING if forge.has_ci_config(repo_root) else NONE
    return _aggregate_ci_states(snapshot)


def get_story_ci(plan_name: str, story_key: str) -> dict[str, Any]:
    """
    Return the CI result of the story's pull request, with a failing log excerpt.

    plan_name: the plan's name this story belongs to.
    story_key: the story's key within that plan's manifest.

    The branch is ``agent/<story_key>`` and the repository is the plan's own.
    The reply is {"available": True, "pr_url", "state", "checks", "failure_excerpt"}
    where each check is {"name", "state"}, sorted by name, at most 100 of them
    ("truncated": true when cut). failure_excerpt is masked and cut to 4000
    characters when the state is failing, and None otherwise.

    A story with no pull request is {"available": False, "reason":
    "no_pull_request"} and the forge is not called. CI that cannot be read is
    {"available": False, "reason": "ci_unreadable", "correlation_id"}. An
    unknown plan/story returns {"ok": False, "status": 404, "error": ...}.
    An invalid plan name or story key raises ValueError before any store read.

    This reports CI even when the merge_ci_gate setting is off.
    """
    _validate_key(plan_name)
    _validate_key(story_key)
    story = _story(_store.get_manifest_or_none(plan_name), story_key)
    if story is None:
        return dict(NOT_FOUND)
    pr_url = story.get("pr_url")
    if not pr_url:
        return {"available": False, "reason": "no_pull_request"}

    branch = f"agent/{story_key}"
    try:
        with _scoped_repo_root(plan_name):
            repo_root = _scoped_repo_cwd()
            forge = get_forge(repo_root)
            snapshot = forge.checks_snapshot(branch, "")
            state = _state(forge, repo_root, snapshot)
            excerpt = None
            if state == FAIL:
                raw = forge.failure_log_excerpt(branch)
                excerpt = redact(raw)[:EXCERPT_MAX] if raw else None
    except Exception as error:  # noqa: BLE001 - any forge failure reads as unreadable CI
        # The error type only: its text can carry a URL or a token.
        logger.warning("story CI unreadable", extra={"error_type": type(error).__name__})
        return {
            "available": False,
            "reason": "ci_unreadable",
            "correlation_id": current_correlation_id(),
        }

    checks, truncated = _checks(snapshot)
    reply = {
        "available": True,
        "pr_url": redact(pr_url) if isinstance(pr_url, str) else pr_url,
        "state": state,
        "checks": checks,
        "failure_excerpt": excerpt,
    }
    if truncated:
        reply["truncated"] = True
    return reply
