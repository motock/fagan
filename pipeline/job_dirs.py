"""Directory layout for detached job state: merge re-verify, local checks, review.

Story keys are unique per namespace only, so a job directory names its
namespace the way a worktree does: the default namespace keeps ``<story_key>``
under the job's base directory, and a team namespace nests under
``_ns/<namespace with '/' as '@'>/``. Unlike worktrees,
``PIPELINE_WORKTREE_NAMESPACE`` does not move job state.
"""

from __future__ import annotations

import os
from pathlib import Path

from pipeline.branch_names import RESERVED_DIR, WorktreeRefused, _encoded_namespace


def job_dir_name(story_key: str, namespace: str | None = None) -> str:
    """Path of a job directory relative to its base directory.

    A default or absent ``namespace`` returns ``story_key`` unchanged. Any other
    valid namespace returns ``_ns/<encoded>/<story_key>``. A key made only of
    dots (including the empty key) is refused in every namespace, since it
    would resolve to the base or its parent; a key equal to ``RESERVED_DIR`` is
    refused in the default namespace, where it would collide with that subtree.
    """
    from app.credentials import is_default_namespace

    if set(story_key) <= {"."}:
        raise WorktreeRefused("invalid story key")
    if namespace is None or is_default_namespace(namespace):
        if story_key.lower() == RESERVED_DIR:
            raise WorktreeRefused("story key is reserved")
        return story_key
    return f"{RESERVED_DIR}/{_encoded_namespace(namespace)}/{story_key}"


def job_dir_under(base: Path | str, story_key: str, namespace: str | None) -> Path:
    """Resolved job directory for *story_key* under *base*, refused if it escapes.

    Creates nothing. The directory must lie strictly inside *base*. Raises
    ``ValueError("invalid story_key")``; the message never names a path.
    """
    base = Path(base).resolve()
    try:
        name = job_dir_name(story_key, namespace)
    except WorktreeRefused:
        raise ValueError("invalid story_key") from None
    job_dir = (base / name).resolve()
    if job_dir == base or not job_dir.is_relative_to(base):
        raise ValueError("invalid story_key")
    return job_dir


def make_job_dir(base: Path | str, job_dir: Path) -> None:
    """Create *job_dir* and any namespace levels below *base*, each mode 0o700.

    ``mkdir``'s mode is filtered by the process umask and does not re-tighten an
    existing directory, so each level is chmodded explicitly: the job's result
    and log carry story paths and test output, and a team's ``_ns/<ns>`` level
    would otherwise list its story keys to other users. *base* itself is left
    alone; it is shared by every key.
    """
    base = Path(base).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    for level in (job_dir, *job_dir.parents):
        if level == base:
            break
        os.chmod(level, 0o700)
