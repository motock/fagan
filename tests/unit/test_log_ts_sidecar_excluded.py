"""Regression tests for excluding every *.log.ts sidecar from git.

pipeline/execution.py's spawn_local writes a per-line timestamp sidecar next
to EVERY dispatch/review log it opens - agent.log, review.log,
test_author.log, rework_test_author.log and grading.log - but the repo
.gitignore's "*.log" pattern does NOT match "foo.log.ts", and naming only
"agent.log.ts" in pipeline.paths._WORKTREE_LOG_EXCLUDES left the other four
untracked-and-unignored, so _commit_wip's `git add -A` swept them into story
commits (a stray test_author.log.ts reached agent/chatreload-1 on
2026-09-11).

These tests pin the fix:
  1. _WORKTREE_LOG_EXCLUDES carries the "*.log.ts" glob - REPLACING, not
     joining, the old "agent.log.ts" literal - and the glob genuinely
     matches the sidecar of every log spawn_local opens.
  2. The repo's own .gitignore carries a "*.log.ts" rule line.
  3. The regression's root cause is pinned: "*.log" does NOT match
     "agent.log.ts", so the new rule is not redundant and nobody may
     'simplify' it away.
  4. No *.log.ts file is tracked by git (an ignore rule does not untrack an
     already-tracked file), and `git check-ignore` reports the sidecars as
     ignored from the repo root.

RED until the implementation lands (the paths.py glob + the .gitignore
rule); test_no_log_ts_file_is_tracked_by_git and
test_plain_log_glob_does_not_match_the_sidecar are green from the start
because they pin invariants that already hold.
"""

import fnmatch
import subprocess
from pathlib import Path

from pipeline.paths import _WORKTREE_LOG_EXCLUDES

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GITIGNORE = REPO_ROOT / ".gitignore"
PATHS_PY = REPO_ROOT / "pipeline" / "paths.py"

# Every log basename spawn_local opens a timestamp sidecar for (see
# pipeline/execution.py's spawn_local; the 2026-09-11 stray
# test_author.log.ts that reached agent/chatreload-1 is the regression).
SPAWNED_LOG_BASENAMES = (
    "agent.log",
    "review.log",
    "test_author.log",
    "rework_test_author.log",
    "grading.log",
)

# Sidecars the story's success criteria name for `git check-ignore` (plus
# agent.log.ts, the original entry the glob replaces).
CHECK_IGNORE_SIDECARS = (
    "test_author.log.ts",
    "review.log.ts",
    "grading.log.ts",
    "rework_test_author.log.ts",
    "agent.log.ts",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gitignore_rule_lines() -> list[str]:
    """Return the repo .gitignore's pattern lines: whitespace-stripped, with
    blank lines and comment lines dropped."""
    rules: list[str] = []
    for raw in GITIGNORE.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        rules.append(stripped)
    return rules


def _gitignore_comment_lines() -> list[str]:
    """Return the repo .gitignore's comment lines, whitespace-stripped."""
    return [
        stripped
        for stripped in (
            raw.strip() for raw in GITIGNORE.read_text(encoding="utf-8").splitlines()
        )
        if stripped.startswith("#")
    ]


# ---------------------------------------------------------------------------
# 1. The per-worktree exclude list uses the glob
# ---------------------------------------------------------------------------

def test_all_spawned_log_sidecars_are_excluded():
    """_WORKTREE_LOG_EXCLUDES must carry "*.log.ts" - replacing the single
    "agent.log.ts" literal - and the glob must genuinely match the sidecar
    of every log spawn_local opens."""
    assert "*.log.ts" in _WORKTREE_LOG_EXCLUDES, (
        '_WORKTREE_LOG_EXCLUDES must contain the "*.log.ts" glob: spawn_local '
        "writes a timestamp sidecar for EVERY log it opens (agent.log, "
        "review.log, test_author.log, rework_test_author.log, grading.log), "
        "and naming only agent.log.ts left the other four "
        "untracked-and-unignored for `git add -A` to sweep into story commits."
    )
    # The glob REPLACES the old entry; keeping both is noise (the brief is
    # explicit: do not keep both).
    assert "agent.log.ts" not in _WORKTREE_LOG_EXCLUDES, (
        '"agent.log.ts" must be REPLACED by "*.log.ts", not kept alongside it '
        "- the glob already covers it and a duplicate entry is noise."
    )
    # The pre-existing exclusions must survive the edit.
    for entry in (
        "agent.log",
        "review.log",
        ".agent_plan.md",
        ".agent_scratchpad.md",
        ".agent_plan_src_hash",
    ):
        assert entry in _WORKTREE_LOG_EXCLUDES, (
            f"the glob edit must not drop the pre-existing exclude {entry!r}"
        )
    # Prove the single glob genuinely covers every sidecar spawn_local can
    # produce, rather than asserting a hand-copied list of sidecar names.
    for name in SPAWNED_LOG_BASENAMES:
        assert fnmatch.fnmatch(name + ".ts", "*.log.ts") is True, (
            f'"*.log.ts" must match the sidecar {name + ".ts"!r}'
        )
    # Boundary: the glob must NOT over-match the plain logs themselves -
    # they keep their own literal entries ("agent.log", "review.log") and
    # must not be silently reclassified as sidecars.
    assert fnmatch.fnmatch("agent.log", "*.log.ts") is False
    assert fnmatch.fnmatch("review.log", "*.log.ts") is False


def test_paths_py_documents_why_the_glob_is_needed():
    """The tuple edit must carry its rationale comment in pipeline/paths.py
    (spawn_local writes a timestamp sidecar for EVERY log it opens, and
    "*.log" does not match "foo.log.ts"), so the glob is not 'simplified'
    back to a single filename later."""
    source = PATHS_PY.read_text(encoding="utf-8")
    assert '"*.log.ts" (not just "agent.log.ts")' in source, (
        "pipeline/paths.py must keep the rationale comment introducing the "
        'glob: \'# "*.log.ts" (not just "agent.log.ts")\''
    )
    assert "spawn_local" in source, (
        "pipeline/paths.py's comment must name spawn_local as the writer of "
        "the per-line timestamp sidecars"
    )
    assert "test_author.log" in source, (
        "pipeline/paths.py's comment must name the sidecar-bearing logs "
        "(test_author.log et al.) that motivated the glob"
    )


# ---------------------------------------------------------------------------
# 2. The repo .gitignore covers the sidecars
# ---------------------------------------------------------------------------

def test_gitignore_covers_log_ts_sidecars():
    """The repo's own .gitignore must ignore the sidecars: "*.log" does not
    match "foo.log.ts", so a literal "*.log.ts" rule line is required."""
    rules = _gitignore_rule_lines()
    assert "*.log.ts" in rules, (
        ".gitignore must contain a '*.log.ts' rule line (comments stripped): "
        'the existing "*.log" pattern does NOT match "foo.log.ts" sidecars.'
    )
    # The rule this story extends must still be there.
    assert "*.log" in rules, "the pre-existing '*.log' rule must remain"
    # ... and the explanatory comment must be present, so the rule is not
    # deleted later as "redundant" with "*.log".
    comments = _gitignore_comment_lines()
    assert any("spawn_local" in line for line in comments), (
        ".gitignore must keep the comment explaining that spawn_local writes "
        'a "*.log.ts" sidecar next to every dispatch/review log (so nobody '
        ' "simplifies" the rule away as redundant with "*.log").'
    )


# ---------------------------------------------------------------------------
# 3. The root cause, pinned
# ---------------------------------------------------------------------------

def test_plain_log_glob_does_not_match_the_sidecar():
    """Pin the regression's actual cause: fnmatch("agent.log.ts", "*.log")
    is False - i.e. the pre-existing "*.log" rule never covered the
    sidecars - so nobody may 'simplify' the "*.log.ts" rule away."""
    assert fnmatch.fnmatch("agent.log.ts", "*.log") is False, (
        'if "*.log" ever starts matching "agent.log.ts", the "*.log.ts" rule '
        "becomes redundant and this story's premise is void - re-check "
        "fnmatch semantics before touching this assertion"
    )


# ---------------------------------------------------------------------------
# 4. Nothing *.log.ts is tracked, and the sidecars are ignored end-to-end
# ---------------------------------------------------------------------------

def test_no_log_ts_file_is_tracked_by_git():
    """An ignore rule does NOT untrack an already-tracked file: any *.log.ts
    sidecar committed before the rule landed must be `git rm --cached`ed
    (working file stays on disk, index entry goes)."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    offenders = [path for path in tracked if path.endswith(".log.ts")]
    assert offenders == [], (
        "no *.log.ts sidecar may be tracked by git; untrack each with "
        f"`git rm --cached <path>` (the working file stays on disk): {offenders}"
    )


def test_check_ignore_reports_sidecars_as_ignored():
    """End-to-end success criterion: from the repo root, `git check-ignore
    -q <sidecar>` must exit 0 for every sidecar spawn_local can produce."""
    for sidecar in CHECK_IGNORE_SIDECARS:
        result = subprocess.run(
            ["git", "check-ignore", "-q", sidecar],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,  # exit 1 = NOT ignored = the assertion below fails
        )
        assert result.returncode == 0, (
            f"`git check-ignore -q {sidecar}` must exit 0 (ignored) from the "
            "repo root; the .gitignore '*.log.ts' rule is missing or wrong."
        )