"""Plan-conflict classification for a red test suite (LD90 W1b).

A *plan conflict* is a red suite whose failing test files are ALL pre-existing
at the merge base and untouched by the branch: the branch cannot have broken
them, so the failures belong to the plan's rework path rather than to the
story under review.

The classifier is deliberately conservative: any ambiguous input -- an empty
failure list, a failing file the branch changed, a failing file that does not
exist at base -- yields ``None`` ("not a conflict"), so the existing rework
path stays the default. The git adapter fails open the same way: if the two
file sets cannot be read (non-zero exit, missing git, timeout), the result is
``None`` and the caller keeps today's behavior.
"""

from __future__ import annotations

import subprocess

__all__ = ["branch_file_sets", "classify_plan_conflict", "failing_test_files"]

# Wall-clock cap for each git invocation; a hung git must not stall the caller.
_GIT_TIMEOUT_SECONDS = 30


def failing_test_files(node_ids: list[str]) -> list[str]:
    """Map pytest node ids to their test-file parts, de-duplicated.

    The file part is everything before the first ``::``. Entries whose file
    part does not end in ``.py`` (and empty strings) are ignored. First-seen
    order is preserved so callers get a stable, readable list.
    """
    files: list[str] = []
    seen: set[str] = set()
    for node_id in node_ids:
        file_part = node_id.split("::", 1)[0]
        if not file_part.endswith(".py"):
            continue
        if file_part in seen:
            continue
        seen.add(file_part)
        files.append(file_part)
    return files


def classify_plan_conflict(
    failing_files: list[str],
    files_at_base: set[str],
    files_changed_on_branch: set[str],
) -> list[str] | None:
    """Return ``sorted(failing_files)`` iff the failures are a plan conflict.

    Pure. A plan conflict requires failing_files to be non-empty, every
    failing file to exist at the merge base, and none to be changed on the
    branch. Any other combination returns ``None``: an ambiguous input is
    never classified as a conflict, so the existing rework path stays the
    default.
    """
    if not failing_files:
        return None
    for path in failing_files:
        if path not in files_at_base:
            return None
        if path in files_changed_on_branch:
            return None
    return sorted(failing_files)


def branch_file_sets(
    worktree: str, base_ref: str
) -> tuple[set[str], set[str]] | None:
    """Read the files at ``base_ref`` and the files the branch changed.

    Runs ``git ls-tree -r --name-only <base_ref>`` (files at base) and
    ``git diff --name-only --no-renames <base_ref>..HEAD`` (files changed on
    the branch) inside ``worktree``. Returns ``(files_at_base,
    files_changed_on_branch)`` parsed from stdout (blank lines ignored), or
    ``None`` on any non-zero exit, ``OSError`` (e.g. git missing), or timeout
    -- fail open, because ``None`` means "not a plan conflict" and the caller
    keeps today's path.
    """
    ls_tree_argv = ["git", "ls-tree", "-r", "--name-only", base_ref]
    diff_argv = ["git", "diff", "--name-only", "--no-renames", f"{base_ref}..HEAD"]
    run_kwargs: dict = {
        "cwd": worktree,
        "capture_output": True,
        "text": True,
        "timeout": _GIT_TIMEOUT_SECONDS,
    }
    try:
        ls_tree = subprocess.run(ls_tree_argv, check=False, **run_kwargs)
        diff = subprocess.run(diff_argv, check=False, **run_kwargs)
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None
    if ls_tree.returncode != 0 or diff.returncode != 0:
        return None
    files_at_base = {line for line in ls_tree.stdout.splitlines() if line}
    files_changed = {line for line in diff.stdout.splitlines() if line}
    return files_at_base, files_changed