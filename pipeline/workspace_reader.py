"""Workspace reads as a seam (ADR 0012, decision 12).

The dashboard and the service only ever need two reads out of the workspace:
the tail of a story's log file and one file out of a story's worktree. Both
used to live on ``FileStore``; they are moved here verbatim so the store stops
owning filesystem paths and every caller goes through one seam that can be
faked in tests.

Every containment check is part of the contract, not an implementation
detail:

* the story log must resolve inside the plans directory (an absolute log path
  in the manifest, or a path that resolves outside it, is unavailable),
* the worktree file must resolve inside ``WORKTREE_ROOT`` (a relative
  worktree, a traversal filename, or a target that resolves - via ``..`` or a
  symlink - outside the root is unavailable),
* any ``OSError`` degrades to the unavailable body instead of raising.

Both reads are pure functions of their arguments plus the two roots, so they
are ``staticmethod``s: the protocol describes what a reader must answer, not
what state it carries, and a fake can be substituted at class or instance
level without a binding dance.

The roots are read through ``LiveRef`` bindings to ``pipeline.server`` so
``monkeypatch.setattr(pipeline.server, "PLAN_DIR", ...)`` still lands (a
module-load copy would freeze the real ``~/.claude/plans`` path into every
test run). Once HostState lands these bindings point at its story-log root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pipeline.live_ref import LiveRef

__all__ = ["LocalWorkspaceReader", "WorkspaceReader"]

WORKTREE_ROOT = LiveRef("WORKTREE_ROOT")

STORY_LOG_TAIL_DEFAULT = 200
STORY_LOG_TAIL_MAX = 500


def _story_log_root():
    """The live story-log root: the plans directory until HostState owns it.

    Resolved per call, never captured at module load: the file store owns the
    binding (``pipeline.store.STORY_LOG_ROOT``, itself a live reference onto
    ``pipeline.server``), so a test that monkeypatches the server's plans
    directory is seen here too. This module does not name that directory
    itself - the ADR 0012 plan-directory allowlist has no entry for it.
    """
    from pipeline.store import STORY_LOG_ROOT

    return STORY_LOG_ROOT


class WorkspaceReader(Protocol):
    """The two workspace reads the dashboard and the service perform."""

    @staticmethod
    def read_story_log(
        plan_name: str, story_key: str, manifest: dict[str, Any], lines: int = 200
    ) -> dict[str, Any]: ...

    @staticmethod
    def read_worktree_file(
        story: dict[str, Any], filename: str
    ) -> dict[str, Any]: ...


class LocalWorkspaceReader:
    """Reads the local filesystem, fail-closed on anything outside the roots.

    ``plan_name`` is accepted for interface parity (the story-log root is the
    plans directory today); the containment decision is made solely from the
    resolved path, never from the caller's claim about it.
    """

    @staticmethod
    def read_story_log(
        plan_name: str, story_key: str, manifest: dict[str, Any], lines: int = 200
    ) -> dict[str, Any]:
        empty = {"available": False, "lines": []}
        if not isinstance(lines, int) or lines < 1:
            lines = STORY_LOG_TAIL_DEFAULT
        lines = min(lines, STORY_LOG_TAIL_MAX)
        stories = manifest.get("stories") if isinstance(manifest, dict) else None
        if not isinstance(stories, dict):
            return empty
        story = stories.get(story_key)
        if not isinstance(story, dict):
            return empty
        raw_log = story.get("log")
        if not isinstance(raw_log, str) or not raw_log:
            return empty
        log_path = Path(raw_log)
        if log_path.is_absolute():
            # Manifests store log paths relative to the story-log root; an
            # absolute path is a corrupt/hand-edited manifest we fail closed
            # on (resolving an absolute path under a CWD we don't control
            # would be unsafe).
            return empty
        root = _story_log_root()
        log_path = root / raw_log
        try:
            log_path = log_path.resolve(strict=False)
            root_resolved = root.resolve()
            if log_path != root_resolved and not log_path.is_relative_to(root_resolved):
                return empty
        except OSError:
            return empty
        if not log_path.exists() or not log_path.is_file():
            return empty
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return empty
        all_lines = text.splitlines()
        tail = all_lines[-lines:]
        return {"available": True, "lines": tail}

    @staticmethod
    def read_worktree_file(story: dict[str, Any], filename: str) -> dict[str, Any]:
        empty = {"available": False, "text": ""}
        if not isinstance(story, dict):
            return empty
        if not isinstance(filename, str) or not filename:
            return empty
        if "/" in filename or "\\" in filename or filename == ".." or filename == ".":
            return empty
        raw_wt = story.get("worktree")
        if not isinstance(raw_wt, str) or not raw_wt:
            return empty
        wt_path = Path(raw_wt)
        if not wt_path.is_absolute():
            return empty
        target = wt_path / filename
        try:
            target = target.resolve(strict=False)
            root = WORKTREE_ROOT.resolve()
            if target != root and not target.is_relative_to(root):
                return empty
        except OSError:
            return empty
        if not target.exists() or not target.is_file():
            return empty
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return empty
        return {"available": True, "text": text}


# The one reader every caller shares. Callers depend on the object, not the
# class, so a different implementation can be injected at the seam.
local_workspace_reader: WorkspaceReader = LocalWorkspaceReader()
