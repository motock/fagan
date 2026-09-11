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