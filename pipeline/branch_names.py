"""Single definition of the story branch naming convention."""

import os
import re

NAMESPACE_ENV = "PIPELINE_BRANCH_NAMESPACE"
WORKTREE_NAMESPACE_ENV = "PIPELINE_WORKTREE_NAMESPACE"
_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
#: Subtree holding worktrees of non-default namespaces; a default-namespace key
#: equal to it is refused so the two cannot share a directory.
RESERVED_DIR = "_ns"


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


def worktree_dir_name(story_key: str, namespace: str | None = None) -> str:
    """Directory name for a story's worktree, relative to ``WORKTREE_ROOT``.

    Unset or empty ``PIPELINE_WORKTREE_NAMESPACE`` returns ``story_key``
    unchanged (the historical layout). When set, the result is
    ``<namespace>/<story_key>``; ``namespace`` is ignored, so per-team
    instances keep their layout. An invalid namespace raises.

    With the variable unset, a default or absent ``namespace`` returns
    ``story_key`` unchanged, and any other valid namespace returns
    ``_ns/<namespace with '/' as '@'>/<story_key>`` so two namespaces that
    hold the same key never share a directory. A key equal to
    ``RESERVED_DIR`` is refused in the default namespace, where it would
    collide with that subtree.

    The key's case is preserved: git refs are case-insensitive on macOS, so
    ``story_branch`` lowercases, but directory names are not and dispatch must
    keep the exact key spelling it has always used.
    """
    from pipeline.store_records import DEFAULT_NAMESPACE

    env_namespace = os.environ.get(WORKTREE_NAMESPACE_ENV, "")
    if env_namespace:
        if not _NAMESPACE_RE.fullmatch(env_namespace):
            raise ValueError(
                f"{WORKTREE_NAMESPACE_ENV} must match {_NAMESPACE_RE.pattern}"
            )
        return f"{env_namespace}/{story_key}"
    if namespace is None or namespace == DEFAULT_NAMESPACE:
        if story_key.lower() == RESERVED_DIR:
            raise ValueError("story key is reserved")
        return story_key
    return f"{RESERVED_DIR}/{_encoded_namespace(namespace)}/{story_key}"


def _encoded_namespace(namespace: str) -> str:
    """``namespace`` as one directory segment; raises on an invalid value.

    Imported lazily: this module imports only ``os`` and ``re`` at load time.
    """
    from pipeline.store_records import validate_namespace

    validate_namespace(namespace)
    return namespace.replace("/", "@")


def story_worktree_path(root, story: dict, story_key: str, namespace: str | None):
    """Where a story's worktree lives under ``root``.

    A worktree recorded on the story resumes where its work is, provided it
    still exists and resolves inside ``root``; anything else falls back to the
    derived path from ``worktree_dir_name``.
    """
    from pathlib import Path

    recorded = story.get("worktree")
    if isinstance(recorded, str) and recorded:
        candidate = Path(recorded)
        if candidate.is_dir() and candidate.resolve().is_relative_to(Path(root).resolve()):
            return candidate
    return Path(root) / worktree_dir_name(story_key, namespace)


def worktree_parent(root, namespace: str | None):
    """The directory holding ``namespace``'s worktrees.

    ``root`` itself for the default namespace, an absent namespace, and an
    unset-or-set ``PIPELINE_WORKTREE_NAMESPACE`` (which keeps the historical
    layout); ``root/_ns/<encoded>`` for any other namespace.
    """
    from pathlib import Path

    from pipeline.store_records import DEFAULT_NAMESPACE

    if os.environ.get(WORKTREE_NAMESPACE_ENV, "") or namespace in (None, DEFAULT_NAMESPACE):
        return Path(root)
    return Path(root) / RESERVED_DIR / _encoded_namespace(namespace)


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
