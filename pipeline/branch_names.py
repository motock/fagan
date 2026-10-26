"""Single definition of the story branch naming convention."""

import os
import re

NAMESPACE_ENV = "PIPELINE_BRANCH_NAMESPACE"
WORKTREE_NAMESPACE_ENV = "PIPELINE_WORKTREE_NAMESPACE"
_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def recorded_kwargs(story: dict) -> dict:
    """``{"recorded": branch}`` when the story has a dispatched branch, else
    ``{}`` so callers of branch resolvers stay unchanged for legacy stories."""
    branch = story.get("branch")
    return {"recorded": branch} if branch else {}


def record_story_branch(story: dict, story_key: str) -> str:
    """Set ``story["branch"]`` at dispatch (keeping an earlier record) and
    return it."""
    story["branch"] = story_branch(story_key, story.get("branch"))
    return story["branch"]


def worktree_dir_name(story_key: str) -> str:
    """Directory name for a story's worktree, relative to ``WORKTREE_ROOT``.

    Unset or empty ``PIPELINE_WORKTREE_NAMESPACE`` returns ``story_key``
    unchanged (the historical layout). When set, the result is
    ``<namespace>/<story_key>``. An invalid namespace raises.

    The key's case is preserved: git refs are case-insensitive on macOS, so
    ``story_branch`` lowercases, but directory names are not and dispatch must
    keep the exact key spelling it has always used.
    """
    namespace = os.environ.get(WORKTREE_NAMESPACE_ENV, "")
    if not namespace:
        return story_key
    if not _NAMESPACE_RE.fullmatch(namespace):
        raise ValueError(
            f"{WORKTREE_NAMESPACE_ENV} must match {_NAMESPACE_RE.pattern}"
        )
    return f"{namespace}/{story_key}"


def story_branch(story_key: str, recorded: str | None = None) -> str:
    """Branch for a story: the name recorded at dispatch, else the convention.

    The convention is ``agent/<key>``, or ``agent/<ns>/<key>`` when
    ``PIPELINE_BRANCH_NAMESPACE`` is set. An invalid namespace, or a key equal
    to it (git cannot hold both ``agent/x`` and ``agent/x/y``), raises.
    """
    if recorded:
        return recorded
    key = story_key.lower()
    namespace = os.environ.get(NAMESPACE_ENV, "")
    if not namespace:
        return f"agent/{key}"
    if not _NAMESPACE_RE.fullmatch(namespace):
        raise ValueError(
            f"{NAMESPACE_ENV} must match {_NAMESPACE_RE.pattern}"
        )
    if key == namespace:
        raise ValueError(
            f"story key {story_key!r} equals {NAMESPACE_ENV} {namespace!r}; "
            "git cannot hold both agent/<ns> and agent/<ns>/<key>"
        )
    return f"agent/{namespace}/{key}"
