"""Git worktree helpers for the pipeline MCP server.

Three pure leaf functions used by check_story_status / dispatch_story /
interrupt_story / checkpoint: classify an agent.log by its last non-empty
line, WIP-commit uncommitted work in a worktree (excluding agent.log), and
detect whether the agent branch has commits not on the base branch.

No module-level state, no free-variable reads of server globals. Tests patch
p._worktree_has_new_commits / p._commit_wip directly; server call sites use
the bare names, which resolve through the re-export in
pipeline_mcp_server.py to the patched attribute (Option A in
PIPELINE_MCP_DECOMPOSITION_PLAN.md §4).
"""

import re
import subprocess
from pathlib import Path

# Scratchpad files are the dispatched agent's own running notes. They must
# never be tracked by a checkpoint commit: .gitignore keeps a NEW one out, but
# ignore rules do nothing once a file is tracked, and a tracked scratchpad has
# repeatedly caused real rebase conflicts on master (2026-09-17).
_SCRATCHPAD_PATTERNS = (
    ".agent_scratchpad.md",
    ".agent_scratchpad*.md",
    "*agent_scratchpad*.md",
)

# The same patterns as git pathspec magic, for excluding them from a diff.
_SCRATCHPAD_EXCLUDES = tuple(f":(exclude){p}" for p in _SCRATCHPAD_PATTERNS)


def _last_nonempty_line(path: Path) -> str:
    """Return the last stripped-non-empty line of `path`, or ``""`` if the file
    has no non-empty lines (or doesn't exist — caller should check).

    Used by check_story_status to classify the agent's terminal exit by the
    tail of agent.log. We must NOT substring-match the whole file: a resumed
    agent appends to the log, so an earlier step-cap marker from a prior
    tick may still be present when the resumed run completes successfully.
    Only the final terminal line classifies the current run.

    Iterates line by line so we don't materialize a multi-MB log into memory
    just to grab the last line; the file is read in binary mode and decoded
    per-line so a partial trailing line (no newline) is still considered."""
    last = ""
    with open(path, "rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                last = line
    return last


def _commit_wip(worktree: str, story_key: str, step: str,
                 guard_against_deletion: bool = False) -> str:
    """Commit any uncommitted work in the worktree as a WIP checkpoint.

    External boundary: spawns ``git``. Tests mock subprocess.run. If there is
    nothing to commit (the agent already committed its own work), this is
    not an error — the existing HEAD sha is returned so the journal still
    records a checkpoint marker.

    Excludes ``agent.log``: it's the dispatcher's own session-narration file
    written into the worktree root, not project code, and must never be
    swept into a commit. We stage everything, then unstage agent.log,
    regardless of whether ``guard_against_deletion`` is set.

    When ``guard_against_deletion=True`` (used by the dispatch watchdog
    timeout and manual interrupt paths), any staged file that has been fully
    deleted will be restored from HEAD before committing. This prevents a
    killed‑mid‑write deletion from being recorded as legitimate work.

    Parameters
    ----------
    guard_against_deletion:
        If ``True``, restore staged deletions from the previous commit
        before creating the checkpoint. Defaults to ``False`` for normal
        checkpoint calls.
    """
    # Stage all changes, including deletions.
    subprocess.run(["git", "add", "-A"], cwd=worktree,
                    check=True, capture_output=True, text=True)

    # Unstage agent.log unconditionally.
    subprocess.run(["git", "reset", "-q", "--", "agent.log"], check=False, cwd=worktree,
                    capture_output=True, text=True)

    if guard_against_deletion:
        # Detect staged deletions and restore them from HEAD before committing.
        # If there are any staged additions or modifications (e.g., rename-in-progress),
        # skip restoration to preserve real WIP changes.
        #
        # The scratchpad paths are excluded from BOTH filters. The untrack loop
        # below deliberately leaves a SYNTHETIC staged deletion for them (still
        # in HEAD, gone from the index); an unfiltered guard would misread that
        # as a real deletion and "restore" it, which re-tracks the scratchpad
        # (making the untrack a no-op on this watchdog/interrupt path) and
        # overwrites the running agent's newest notes with HEAD's older blob.
        # Excluding them from the AM filter too keeps a scratchpad modification
        # from masking a genuine deletion of some other file.
        added_mods = subprocess.run(
            ["git", "diff", "--cached", "--diff-filter=AM", "--name-only", "--",
             ".", *_SCRATCHPAD_EXCLUDES],
            check=False, cwd=worktree,
            capture_output=True,
            text=True,
        )
        if not added_mods.stdout.strip():
            diff_res = subprocess.run(
                ["git", "diff", "--cached", "--diff-filter=D", "--name-only", "--",
                 ".", *_SCRATCHPAD_EXCLUDES],
                check=False, cwd=worktree,
                capture_output=True,
                text=True,
            )
            for path in diff_res.stdout.splitlines():
                if path.strip():
                    subprocess.run(["git", "checkout", "HEAD", "--", path], cwd=worktree, check=True)
                    subprocess.run(["git", "add", "--", path], cwd=worktree, check=True)

    # Untrack any scratchpad file from the commit, even if a PRIOR commit
    # (possibly made directly by the dispatched agent's own `git add`/`git
    # commit` calls, bypassing this helper) already tracked it. `git add -A`
    # above re-stages an already-tracked file's changes regardless of
    # .gitignore -- ignore rules only stop a NEW file from being staged, they
    # do nothing once a file is tracked. `git rm --cached` removes it from
    # the index (and this commit) while leaving the working-tree file alone,
    # so a currently-running agent's own scratchpad notes are never lost --
    # only the git history of it is cleaned up. This must never fail the
    # checkpoint: --ignore-unmatch makes it a no-op when nothing matches.
    #
    # This runs AFTER the guard above, and must stay the LAST index mutation
    # before the commit: `git rm --cached` leaves a synthetic staged deletion,
    # so a later `git add -A` would re-stage the file as a brand-new one.
    # Observed live 2026-09-17: two same-day stories each committed
    # .agent_scratchpad.md directly, and a third story's merge then hit a
    # real rebase conflict in that same file, burning its triage budget and
    # sitting parked for hours before a human intervened.
    for pattern in _SCRATCHPAD_PATTERNS:
        subprocess.run(
            ["git", "rm", "-r", "--cached", "--ignore-unmatch", "-q", "--", pattern],
            check=False, cwd=worktree, capture_output=True, text=True,
        )
    commit = subprocess.run(
        ["git", "commit", "-m", f"wip({story_key}): {step}"],
        check=False, cwd=worktree, capture_output=True, text=True,
    )
    output = commit.stdout + commit.stderr
    # git has THREE distinct benign "nothing to do" messages, all on stdout:
    #   "nothing to commit, working tree clean"        - no changes at all
    #   "nothing added to commit but untracked files"  - only untracked files
    #   "no changes added to commit"                   - tracked files modified
    #                                                     but nothing staged
    # The third is one _commit_wip creates for itself: it stages everything
    # and then unstages agent.log, so a run whose only change was agent.log
    # lands here. Treating it as an error made a step-capped agent that wrote
    # nothing but log output crash instead of checkpointing.
    nothing_to_commit = (
        "nothing to commit" in output
        or "nothing added to commit" in output
        or "no changes added to commit" in output
    )
    if commit.returncode != 0 and not nothing_to_commit:
        # git writes most of its diagnostics to stdout, so an stderr-only
        # message is frequently empty and tells the operator nothing.
        detail = (commit.stderr or "").strip() or (commit.stdout or "").strip()
        raise RuntimeError(
            f"git commit failed (exit {commit.returncode}): {detail}"
        )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _worktree_has_new_commits(worktree: Path, story_key: str, base_branch: str) -> bool:
    """True iff the agent branch has any commits not on base_branch.

    ``git log <base>..HEAD --oneline`` lists commits reachable from HEAD
    that aren't reachable from <base>. For an empty branch (agent
    parked without writing code), this list is empty even though the test
    command would pass against main's untouched suite. That's the
    false-positive trap this guards against in check_story_status.

    Returns False on any git error — a broken worktree is the
    orchestrator's problem to surface elsewhere; we'd rather mark a
    real attempt failed than let a transient git hiccup silently
    re-dispatch. The branch name follows the same convention as the
    rest of the orchestrator (line 521 et seq.)."""
    branch = f"agent/{story_key.lower()}"
    r = subprocess.run(
        ["git", "log", f"{base_branch}..{branch}", "--oneline"],
        check=False, cwd=str(worktree), capture_output=True, text=True,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def _worktree_has_non_wip_commits(
    worktree: Path, story_key: str, base_branch: str,
) -> bool:
    """True iff the agent branch has at least one commit not on
    ``base_branch`` whose subject is NOT a WIP checkpoint.

    Companion to ``_worktree_has_new_commits``: that function answers "did
    anything land on the branch" -- a WIP park/checkpoint commit counts, which
    is correct for ``check_story_status``'s "did the agent do anything"
    question. This one answers "did a dispatched phase produce a REAL finished
    commit": a WIP commit made by the harness's own ``_commit_wip`` (subject
    ``wip(<story_key>): ...``) does NOT count, because it records parked /
    checkpointed / interrupted state, not completed work.

    Used by the test-author phases (``_run_test_author_phase`` /
    ``_run_rework_test_author_phase``). A phase that drifted (Mode 31
    off-task drift, live 2026-08-10 on W1a-10) can park mid-write and leave
    only a ``wip(...): parked on ...`` checkpoint commit. The old
    ``_worktree_has_new_commits`` check accepted that as success and handed a
    half-authored, unsatisfiable test file to the executor as a fixed
    must-pass oracle. This function rejects it so the phase falls open to
    monolithic dispatch instead -- the fail-open contract's intended behavior
    on any incomplete test-author outcome.

    The prefix match is case-insensitive: the branch name lower-cases the
    story key but ``_commit_wip`` writes the raw key into the message, so a
    mixed-case key (e.g. a UUID) must match either way. Returns False on any
    git error (fail open, mirroring ``_worktree_has_new_commits``).
    """
    branch = f"agent/{story_key.lower()}"
    prefix = f"wip({story_key}):".lower()
    r = subprocess.run(
        ["git", "log", f"{base_branch}..{branch}", "--format=%s"],
        check=False, cwd=str(worktree), capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False
    return any(
        bool(subject.strip()) and not subject.strip().lower().startswith(prefix)
        for subject in r.stdout.splitlines()
    )


def _test_files_added_on_branch(
    worktree: Path, base_branch: str,
) -> list[str]:
    """Repo-relative paths of ``test_*.py`` files added in commits on this
    branch that aren't on ``base_branch`` - i.e. the test-author phase's
    committed work, to ground the planner in the ACTUAL files rather than
    letting it re-derive test file/case names from the task description.

    Root-caused live 2026-07-25 on MODE40-CI-REWORK-FEEDBACK-V2's THIRD reset:
    the prohibition-only ``_TEST_AUTHOR_ALREADY_RAN_CLAUSE`` told the planner
    not to emit a write-the-test-file step, but gave it no concrete grounding
    in WHICH file/tests exist on the branch - so even sonnet, planning from
    the story's own ``agent_instructions`` (which still describe the pre-split
    TDD flow verbatim), re-derived a "Write test_ci_rework_feedback.py" step
    with INVENTED test-case names that did not match the ones the test-author
    had committed. The caller pairs this list with ``_test_names_in_file`` and
    passes the result into ``_run_planner`` as ``authored_test_files``.

    Returns ``[]`` on any git error or non-git cwd (fail open) - the planner
    then degrades to the prohibition-only clause rather than crashing
    dispatch. ``--diff-filter=A`` restricts to files ADDED on the branch (a
    test file present on the base branch that the branch merely modified is
    not the test-author's new work and is excluded)."""
    r = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=A",
         f"{base_branch}..HEAD"],
        check=False, cwd=str(worktree), capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    return [
        line for line in r.stdout.splitlines()
        if line.startswith("test_") and line.endswith(".py")
    ]


def _test_names_in_file(worktree: Path, rel_path: str) -> list[str]:
    """Top-level ``def test_*`` function names declared in the test file at
    ``rel_path`` within ``worktree``. Used to ground the planner with the
    exact test-case names so it references them instead of inventing them
    (see ``_test_files_added_on_branch``'s root-cause note).

    Only column-0 ``def`` lines match: a nested ``def test_inner`` inside a
    test function is not its own test case. Returns ``[]`` on a missing file
    or read/decode error (fail open) so one unparseable file never blocks the
    planner call."""
    abs_path = worktree / rel_path
    try:
        text = abs_path.read_text()
    except (OSError, UnicodeDecodeError):
        return []
    return re.findall(r"^def (test_\w+)\b", text, re.MULTILINE)


def _untrack_scratchpad(worktree: str, story_key: str) -> dict:
    """Untrack every scratchpad path tracked in `worktree`, as a commit.

    Companion to the untrack loop inside ``_commit_wip``. That one only cleans
    the index for the commit the harness itself is about to make, so a
    scratchpad a dispatched agent tracked with its own ``git add -f`` /
    ``git commit`` -- bypassing every harness commit path -- survives until
    the next harness commit. A tracked scratchpad is not cosmetic: the repo's
    guard tests fail on it, the tick-side grade then rejects a story that was
    otherwise fine, and the file travels into the eventual merge, where a
    rebase against another story that tracked it conflicts (2026-09-17).

    External boundary: spawns ``git``, and never raises - a git failure is
    reported in the returned dict so a caller inside the scheduler tick keeps
    grading. Returns ``{"paths": [...], "sha": ...}``: the sorted tracked
    paths found, and the commit's sha, or ``""`` when nothing was tracked or
    the untrack left nothing to record (a scratchpad that was staged but never
    committed disappears with its index entry, leaving no deletion to commit),
    plus an ``"error"`` key when git itself failed.
    """
    listed: list[str] = []
    try:
        probe = subprocess.run(
            ["git", "ls-files", "--", *_SCRATCHPAD_PATTERNS],
            check=False, cwd=worktree, capture_output=True, text=True,
        )
        listed = (getattr(probe, "stdout", "") or "").split()
        if not listed:
            return {"paths": [], "sha": ""}
        for pattern in _SCRATCHPAD_PATTERNS:
            subprocess.run(
                ["git", "rm", "-r", "--cached", "--ignore-unmatch", "-q", "--", pattern],
                check=False, cwd=worktree, capture_output=True, text=True,
            )
        commit = subprocess.run(
            ["git", "commit", "-m", f"chore({story_key}): untrack agent scratchpad"],
            check=False, cwd=worktree, capture_output=True, text=True,
        )
        # getattr-defended: some test doubles for subprocess.run's return
        # value define no stdout/stderr at all.
        output = (getattr(commit, "stdout", "") or "") + (
            getattr(commit, "stderr", "") or ""
        )
        nothing_to_commit = (
            "nothing to commit" in output
            or "nothing added to commit" in output
            or "no changes added to commit" in output
        )
        if commit.returncode != 0 and not nothing_to_commit:
            return {"paths": sorted(listed), "sha": "", "error": output.strip()}
        if commit.returncode != 0:
            return {"paths": sorted(listed), "sha": ""}
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False, cwd=worktree, capture_output=True, text=True,
        )
        sha = (getattr(head, "stdout", "") or "").strip()
    except OSError as e:
        return {"paths": sorted(listed), "sha": "", "error": str(e)}
    return {"paths": sorted(listed), "sha": sha}
