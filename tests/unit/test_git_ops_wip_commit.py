"""Regression tests for _commit_wip's benign "nothing to do" guard (WIPCOMMIT-1).

ROOT CAUSE: ``_commit_wip`` does ``git add -A`` then ``git reset -q -- agent.log``.
When agent.log is the ONLY modified file, nothing is left staged but a tracked
file is still modified, and git fails the commit with exit 1 while printing its
diagnostic to STDOUT (stderr is EMPTY):

    no changes added to commit (use "git add" and/or "git commit -a")

That string matched neither of the two benign substrings the old guard knew
("nothing to commit", "nothing added to commit"), so a step-capped agent that
wrote nothing but log output crashed with ``RuntimeError: git commit failed: ``
instead of checkpointing. The same stderr-only error message hid the real
diagnostic for every other commit failure, because git writes most of its
diagnostics to stdout.

These tests exercise the REAL git boundary (no subprocess mocking) — the bug
lives in git's actual stdout/stderr split, which a fake CompletedProcess
cannot reproduce.
"""

import subprocess
from pathlib import Path

import pytest

from pipeline.git_ops import _commit_wip

# ---------- helpers ----------

def _git(repo: Path, *args: str) -> str:
    """Run a git command in `repo`, check it succeeded, return its stdout."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _init_repo(tmp_path: Path) -> Path:
    """Create a real git repo with an initial commit containing a tracked
    agent.log and one code file. Configures the committer identity LOCALLY
    in the repo (CI runners may have no global git identity)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test Runner")
    (repo / "agent.log").write_text("run 1\n")
    (repo / "code.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


# ---------- tests ----------

def test_agent_log_only_change_is_not_an_error(tmp_path: Path):
    """WIPCOMMIT-1 regression: agent.log as the ONLY modified file must not
    raise. git stages it via `git add -A`, _commit_wip unstages it again with
    `git reset -q -- agent.log`, leaving nothing staged but a tracked file
    modified — git exits 1 printing "no changes added to commit ..." on
    STDOUT with empty stderr. That is benign here: nothing new is committed
    and the current HEAD sha is returned unchanged."""
    repo = _init_repo(tmp_path)
    before = _git(repo, "rev-parse", "HEAD").strip()

    (repo / "agent.log").write_text("run 2\n")  # modify ONLY agent.log
    sha = _commit_wip(str(repo), "S1", "checkpoint")

    assert sha == before  # full sha: nothing new was committed
    assert _git(repo, "rev-parse", "HEAD").strip() == before
    assert _git(repo, "rev-list", "--count", "HEAD").strip() == "1"


def test_real_code_change_still_commits(tmp_path: Path):
    """Positive case: a real code change must still produce a NEW commit
    (the widened benign guard must not turn every failure benign)."""
    repo = _init_repo(tmp_path)
    before = _git(repo, "rev-parse", "HEAD").strip()

    (repo / "code.py").write_text("x = 2\n")  # modify only the code file
    sha = _commit_wip(str(repo), "S1", "checkpoint")

    assert sha != before
    assert _git(repo, "rev-parse", "HEAD").strip() == sha
    subject = _git(repo, "log", "-1", "--pretty=%s").strip()
    assert subject.startswith("wip(S1): ")


def test_agent_log_is_never_swept_into_the_commit(tmp_path: Path):
    """agent.log must stay excluded even alongside a real code change: the
    commit contains code.py and NOT agent.log (agent.log remains dirty in
    the working tree afterwards — that is expected, do not assert a clean
    git status)."""
    repo = _init_repo(tmp_path)
    before = _git(repo, "rev-parse", "HEAD").strip()

    (repo / "code.py").write_text("x = 2\n")
    (repo / "agent.log").write_text("run 2\n")
    sha = _commit_wip(str(repo), "S1", "checkpoint")

    assert sha != before
    shown = _git(repo, "show", "--stat", "--name-only", "HEAD")
    assert "code.py" in shown
    assert "agent.log" not in shown


def test_error_message_includes_stdout_when_stderr_is_empty(tmp_path: Path):
    """Failure exercised: a FAILING PRE-COMMIT HOOK at the commit step (NOT
    the "path is not a git repository" route — that dies earlier at
    `git add -A`, which runs with check=True, as CalledProcessError and never
    reaches the commit block). The hook echoes to stdout and exits 1, so the
    commit fails with exit 1, stdout "hook says no", stderr empty. The raised
    RuntimeError must carry that stdout diagnostic: with the old
    f"git commit failed: {commit.stderr}" the message was empty after the
    colon, which is exactly what this test catches."""
    repo = _init_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'hook says no'\nexit 1\n")
    hook.chmod(0o755)

    (repo / "code.py").write_text("x = 2\n")
    with pytest.raises(RuntimeError) as excinfo:
        _commit_wip(str(repo), "S1", "checkpoint")

    message = str(excinfo.value)
    assert message.startswith("git commit failed")
    assert "hook says no" in message


# ---------- scratchpad never enters a checkpoint commit ----------
#
# ROOT CAUSE: `git add -A` re-stages an ALREADY-TRACKED file's changes no
# matter what .gitignore says — ignore rules only stop a NEW file from being
# staged. `git reset -- <path>` (the pattern used for agent.log) only unstages
# the diff; it never removes the tracked index entry, so a scratchpad file that
# a PRIOR commit tracked (e.g. the dispatched agent ran its own `git add` /
# `git commit` via its Bash tool, bypassing _commit_wip) rides along in every
# future checkpoint commit forever. Observed live 2026-09-17: two same-day
# stories each committed .agent_scratchpad.md directly, and a third story's
# merge then hit a real rebase conflict in that same file, burning its triage
# budget and sitting parked for hours before a human intervened.
#
# The fix is `git rm -r --cached --ignore-unmatch` (index-only removal, so the
# working-tree file survives untouched) applied AFTER `git add -A`, the last
# index mutation before the commit.

def test_commit_wip_untracks_previously_committed_scratchpad(tmp_path: Path):
    """Live reproduction: .agent_scratchpad.md was already committed in a PRIOR
    commit (made directly, bypassing _commit_wip, exactly as an agent's own
    Bash `git commit` would). The NEXT checkpoint must not carry it, and the
    on-disk file must survive with its modified content intact."""
    repo = _init_repo(tmp_path)
    scratch = repo / ".agent_scratchpad.md"
    scratch.write_text("v1\n")
    (repo / "app.py").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "prior direct commit")

    # Precondition: the scratchpad really is tracked in HEAD.
    assert ".agent_scratchpad.md" in _git(repo, "ls-tree", "-r", "HEAD", "--name-only")

    scratch.write_text("v2\n")
    (repo / "app.py").write_text("b\n")
    before = _git(repo, "rev-parse", "HEAD").strip()
    _commit_wip(str(repo), "S1", "checkpoint")
    after = _git(repo, "rev-parse", "HEAD").strip()

    assert after != before
    tree = _git(repo, "ls-tree", "-r", after, "--name-only")
    assert ".agent_scratchpad.md" not in tree
    assert "app.py" in tree
    # Not merely unstaged: gone from the index entirely.
    assert _git(repo, "ls-files", "--", ".agent_scratchpad.md").strip() == ""
    # The working-tree file is never deleted — only its git history is cleaned.
    assert scratch.exists()
    assert scratch.read_text() == "v2\n"


def test_commit_wip_noop_when_scratchpad_never_tracked(tmp_path: Path):
    """Today's already-passing case: the scratchpad was never tracked. The new
    `git rm --cached --ignore-unmatch` must be a documented no-op (no error, no
    unexpected commit content change) — the call not raising is itself an
    assertion."""
    repo = _init_repo(tmp_path)
    scratch = repo / ".agent_scratchpad.md"
    scratch.write_text("notes\n")
    (repo / "app.py").write_text("a\n")

    _commit_wip(str(repo), "S1", "checkpoint")

    tree = _git(repo, "ls-tree", "-r", "HEAD", "--name-only")
    assert "app.py" in tree
    assert ".agent_scratchpad.md" not in tree
    assert scratch.read_text() == "notes\n"


def test_commit_wip_untracks_broad_glob_scratchpad_variant(tmp_path: Path):
    """Boundary case: `foo.agent_scratchpad.notes.md` matches only the broadest
    pattern `*agent_scratchpad*.md` — not `.agent_scratchpad.md` (different
    name) and not `.agent_scratchpad*.md` (which requires the name to START
    with `.agent_scratchpad`). It must be untracked too."""
    repo = _init_repo(tmp_path)
    scratch = repo / "foo.agent_scratchpad.notes.md"
    scratch.write_text("v1\n")
    (repo / "app.py").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "prior direct commit")

    assert "foo.agent_scratchpad.notes.md" in _git(repo, "ls-tree", "-r", "HEAD", "--name-only")

    scratch.write_text("v2\n")
    (repo / "app.py").write_text("b\n")
    before = _git(repo, "rev-parse", "HEAD").strip()
    _commit_wip(str(repo), "S1", "checkpoint")
    after = _git(repo, "rev-parse", "HEAD").strip()

    assert after != before
    tree = _git(repo, "ls-tree", "-r", after, "--name-only")
    assert "foo.agent_scratchpad.notes.md" not in tree
    assert "app.py" in tree
    assert _git(repo, "ls-files", "--", "foo.agent_scratchpad.notes.md").strip() == ""
    assert scratch.exists()
    assert scratch.read_text() == "v2\n"