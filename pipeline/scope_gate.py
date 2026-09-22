"""Story ``files`` scope gate: keep production changes inside declared scope.

The scope gate grades a branch's changed paths against the story's declared
``files`` list.  It only applies when a story declares ``files``: a story
whose brief names no ``files`` scope is never gated.

Two live incidents motivate the gate:

* a stray edit to ``scripts/pipeline-env.sh`` while the story's ``files``
  named only ``pipeline/foo.py``;
* a whole new top-level ``httpx/`` package (``httpx/__init__.py`` +
  ``httpx/_client.py``) vendored into the repo root, invisible to a per-file
  ``files`` list because the *directory* was new.

The module is a pure classifier plus a thin git adapter:

* ``is_test_path`` -- test paths are always allowed to change.
* ``scope_violations`` -- pure; returns the sorted, de-duplicated violation
  lines (``[]`` when clean) and never mutates its arguments.
* ``check_branch_scope`` -- runs ``git diff --name-only --no-renames
  <base_ref>..HEAD`` and ``git ls-tree --name-only <base_ref>`` in a
  worktree and delegates to ``scope_violations``.  It fails open: any git
  failure, ``OSError`` or timeout yields ``[]`` so the reviewer still runs.
"""

from __future__ import annotations

import subprocess

SCOPE_GATE_HEADER = (
    "SCOPE GATE: this branch changes production paths outside the story's "
    "declared `files` scope."
)

# Upper bound for each git invocation; the gate fails open on timeout.
_GIT_TIMEOUT_SECONDS = 30


def is_test_path(path: str) -> bool:
    """True for test paths: under ``tests/`` or a test/conftest basename."""
    parts = path.split("/")
    if parts[0] == "tests":
        return True
    base = parts[-1]
    return (
        (base.startswith("test_") and base.endswith(".py"))
        or base.endswith("_test.py")
        or base == "conftest.py"
    )


def scope_violations(
    changed_paths: list[str],
    allowed: list[str],
    base_top_level: set[str],
) -> list[str]:
    """Grade changed paths against the story's declared ``files`` scope.

    Pure: builds new collections, never mutates the arguments and never
    shells out.  Returns sorted, de-duplicated violation lines; ``[]`` when
    clean.
    """
    lines = [
        f"{p}: outside this story's `files` scope"
        for p in changed_paths
        if not is_test_path(p) and p not in allowed
    ]

    top_dirs = {p.split("/")[0] for p in changed_paths if "/" in p}
    for top in top_dirs:
        if top in base_top_level:
            continue
        if f"{top}/__init__.py" not in changed_paths:
            continue
        if any(entry.startswith(f"{top}/") for entry in allowed):
            continue
        lines.append(f"{top}/: new top-level package not present at the branch base")

    return sorted(set(lines))


def check_branch_scope(
    worktree: str,
    base_ref: str | None,
    allowed: list[str],
) -> list[str]:
    """Grade the branch's changed paths in ``worktree`` against ``allowed``.

    ``base_ref is None`` means there is nothing to compare against, so the
    gate passes without running git.  Any git failure, ``OSError`` or
    timeout fails open (``[]``) so the reviewer still runs.
    """
    if base_ref is None:
        return []

    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", f"{base_ref}..HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if diff.returncode != 0:
            return []
        tree = subprocess.run(
            ["git", "ls-tree", "--name-only", base_ref],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if tree.returncode != 0:
            return []
    except (OSError, subprocess.SubprocessError):
        return []

    changed = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    base_top_level = {line.strip() for line in tree.stdout.splitlines() if line.strip()}
    return scope_violations(changed, allowed, base_top_level)