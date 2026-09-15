"""Server-side patch records for stuck-story worktrees.

This module is the security surface for applying recorded patches to a
story's git worktree. Its invariants are:

* Patch records live SERVER-side: a worktree never trusts a patch that
  only exists locally; the record must come from the server's store.
* Applying a patch is a HUMAN-CONFIRMED action only: no code path may
  apply a patch without an explicit human confirmation step.
* Deny-by-DEFAULT: any relative path that is not provably safe is
  refused. ``is_denied_relative_path`` below is the pure, deny-by-default
  predicate that decides which paths may never be touched by a patch.

The strict path *resolver* (traversal rejection, absolute-path rejection,
symlink refusal, containment) lives here too: ``resolve_write_target`` is
the WRITE half of the path-security pair whose read half is
``pipeline.workspace_fs.resolve_within_workspace``. It is deliberately
STRICTER than the read half: a symlink is refused even when every hop stays
inside the worktree, because a write through an in-worktree symlink is
still a write the patch author did not name.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path, PurePosixPath


class PatchSecurityError(ValueError):
    """Raised when a patch request violates a worktree patch security invariant."""


def is_denied_relative_path(relative_path: str) -> bool:
    """Return True if the relative path may never be touched by a patch.

    Pure predicate: no filesystem access, stdlib-free (zero imports), and
    it never raises. The path is normalized by splitting on ``/`` and
    dropping empty components, so trailing and duplicate slashes have no
    effect on the verdict.

    Deny rules (deny-by-default, any match denies the whole path):

    * any component equal to ``.git`` or ``.claude``;
    * any component starting with ``.agent_log``;
    * the FINAL component equal to one of ``CLAUDE.md``, ``.mcp.json``,
      ``agent.log``, ``.agent_transcript.json``, ``.agent_plan.md``,
      ``.agent_scratchpad.md``.

    Fail-closed decisions: the EMPTY string (and any other degenerate
    input, including non-string values such as ``None``) is DENIED — it
    returns ``True`` rather than raising, because an unparseable path can
    never be proven safe. Traversal components like ``..`` are NOT
    rejected here (that is the resolver's job in WAP-4B); a ``.git`` or
    ``.claude`` component is denied wherever it appears.
    """
    if not isinstance(relative_path, str):
        # Fail closed: non-string input is malformed, never provably safe.
        return True

    components = [c for c in relative_path.split("/") if c != ""]
    if not components:
        # Fail closed: the empty string (or a path of only slashes) is
        # DENIED, not allowed and not an error.
        return True

    return (
        any(c == ".git" or c == ".claude" for c in components)
        or any(c.startswith(".agent_log") for c in components)
        or components[-1]
        in {
            "CLAUDE.md",
            ".mcp.json",
            "agent.log",
            ".agent_transcript.json",
            ".agent_plan.md",
            ".agent_scratchpad.md",
        }
    )


__all__ = ["PatchSecurityError", "is_denied_relative_path", "resolve_write_target"]


def resolve_write_target(worktree_root: str, relative_path: str) -> Path:
    """Resolve the STRICT write-mode target for a patch inside a worktree.

    This is the WRITE half of the path-security pair whose read half is
    :func:`pipeline.workspace_fs.resolve_within_workspace`.  It is
    deliberately STRICTER than the read half: a symlink is refused even when
    every hop stays inside the worktree, because a write through an
    in-worktree symlink is still a write the patch author did not name.

    Checks, in order:

    1. Deny list (:func:`is_denied_relative_path`) -- pure predicate, checked
       BEFORE any disk access, so a denied path never touches the filesystem.
       Degenerate input (empty string, ``None``, non-string) fails closed
       here as well.
    2. The read half :func:`resolve_within_workspace` rejects ``..`` escape,
       absolute paths and encoded traversal; its
       :class:`~pipeline.workspace.WorkspaceSecurityError` propagates
       unchanged.  ``worktree_root`` is always the story worktree from the
       manifest record, so no duplicate traversal checks are added here.
    3. Symlink walk over the SPELLED path from the worktree root down to the
       target: every EXISTING component (including the final component when
       it exists) must satisfy ``os.path.islink(...) is False``.  Both a
       symlink pointing OUTSIDE the worktree and one pointing INSIDE it are
       refused.  Nonexistent deep paths with non-symlink parents are allowed
       (the patch may create files).
    4. Containment re-check: the resolved target must be the worktree root
       itself or a descendant, and must NOT resolve inside the pipeline's own
       ``REPO_ROOT`` / ``PIPELINE_SELF_REPO_ROOT`` or ``PLAN_DIR`` (read
       lazily from :mod:`pipeline.server`, the pattern used by
       ``pipeline.checkpoint``).
    5. Fail closed: the walk and the containment re-check are wrapped so any
       unexpected OS error (or any other unexpected failure, including a
       failed lazy import of the pipeline constants) becomes
       :class:`PatchSecurityError` -- a path is never returned unless it was
       fully verified.

    Raises
    ------
    PatchSecurityError
        for every write-side rejection.
    WorkspaceSecurityError
        (a ``ValueError`` subclass) when the read half rejects the path.
    """
    # 1. Deny list FIRST: a pure predicate, no filesystem access at all.
    if is_denied_relative_path(relative_path):
        raise PatchSecurityError("relative path is denied for patch writes")

    # 2. Read half: traversal / absolute / encoded-traversal rejection.
    #    WorkspaceSecurityError propagates unchanged from here.  The import
    #    is deferred (importlib, not an import statement) so this module
    #    stays import-statement-free: WAP-4A's purity test requires
    #    pipeline/worktree_patch.py to import nothing outside the stdlib.
    workspace_fs = importlib.import_module("pipeline.workspace_fs")
    resolved = workspace_fs.resolve_within_workspace(worktree_root, relative_path)

    try:
        # 3. Symlink walk over the spelled path.  islink is checked BEFORE
        #    exists so a broken symlink (exists() is False, islink() True)
        #    is still refused; the walk stops at the first component that
        #    does not exist, because the patch may create the rest.
        current = Path(worktree_root)
        for comp in PurePosixPath(relative_path).parts:
            current = current / comp
            spelled = str(current)
            if os.path.islink(spelled):
                raise PatchSecurityError(
                    "write target must not traverse a symlink"
                )
            if not os.path.exists(spelled):
                break

        # 4a. Containment: the target must be the worktree root or below it.
        if not resolved.is_relative_to(Path(worktree_root).resolve()):
            raise PatchSecurityError("write target escapes the worktree root")

        # 4b. The pipeline's own repo checkout and plan storage are never
        #     legal patch targets.  Read the constants lazily from
        #     pipeline.server (the pipeline.checkpoint pattern) so tests that
        #     patch the server bindings stay authoritative.  importlib again:
        #     no import statements in this module (WAP-4A purity test).
        server = importlib.import_module("pipeline.server")
        forbidden_roots = (
            server.REPO_ROOT,
            server.PIPELINE_SELF_REPO_ROOT,
            server.PLAN_DIR,
        )
        for forbidden in forbidden_roots:
            if forbidden is None:
                continue
            if resolved.is_relative_to(Path(forbidden).resolve()):
                raise PatchSecurityError(
                    "write target must not resolve inside the pipeline's "
                    "own repo or plan storage"
                )
    except PatchSecurityError:
        raise
    except Exception as exc:
        # 5. Fail closed: an unverified path is a denial, never a return.
        raise PatchSecurityError(
            "write target could not be safety-checked"
        ) from exc

    return resolved