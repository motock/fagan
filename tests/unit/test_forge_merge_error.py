"""``merge_pr`` must report WHY gh refused, not just the argv it ran.

Both 2026-11-08 incidents (PR #1618 / c1bfcfbf, PR #1621 / e0163dbc) recorded
a ``merge_error`` of exactly::

    Command '['gh', 'pr', 'merge', '1618', '--squash', ...]' returned
    non-zero exit status 1.

That is gh's own message with the *cause* thrown away, so each incident needed
a manual investigation to diagnose (and each merged first try by hand). The
refusal text gh printed on stderr - "X Pull request #1618 is not mergeable: the
merge commit cannot be cleanly created." - is the one thing that tells an
operator whether to retry, rework, or intervene.
"""

from __future__ import annotations

import subprocess

import pytest

from pipeline.forge_github import GitHubForge

BRANCH = "agent/s10-2"
CWD = "/tmp/worktree"
SUBJECT = "s10: land the thing"
STDERR = (
    b"X Pull request #1618 is not mergeable: the merge commit cannot be "
    b"cleanly created.\n"
)


def _raise_on_run(monkeypatch, exc):
    def _run(self, argv, cwd=None, **kw):
        raise exc

    monkeypatch.setattr(GitHubForge, "_run", _run)


def test_merge_pr_failure_message_carries_the_gh_stderr_tail(monkeypatch):
    argv = ["gh", "pr", "merge", BRANCH, "--squash", "--subject", SUBJECT]
    _raise_on_run(
        monkeypatch,
        subprocess.CalledProcessError(
            returncode=1, cmd=argv, stderr=STDERR, output=None
        ),
    )

    with pytest.raises(Exception) as excinfo:
        GitHubForge().merge_pr(CWD, BRANCH, SUBJECT)

    message = str(excinfo.value)
    assert "not mergeable" in message, (
        "the refusal must carry gh's stderr: the 2026-11-08 incidents recorded "
        f"only the bare argv, which needed a manual investigation. Got: {message!r}"
    )
    assert "X Pull request #1618 is not mergeable" in message, message
    assert "gh pr merge" in message and "--squash" in message, (
        f"the argv must stay in the message so the attempted command is "
        f"visible. Got: {message!r}"
    )
    assert "1" in message, f"the exit status must be reported. Got: {message!r}"


def test_merge_pr_failure_keeps_the_called_process_error_contract(monkeypatch):
    """Callers that catch ``subprocess.CalledProcessError`` must keep working."""
    argv = ["gh", "pr", "merge", BRANCH, "--squash"]
    _raise_on_run(
        monkeypatch,
        subprocess.CalledProcessError(returncode=1, cmd=argv, stderr=STDERR),
    )

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        GitHubForge().merge_pr(CWD, BRANCH, "")

    assert excinfo.value.returncode == 1
    assert excinfo.value.cmd == argv


def test_merge_pr_failure_without_stderr_still_names_the_command(monkeypatch):
    argv = ["gh", "pr", "merge", BRANCH, "--squash"]
    _raise_on_run(
        monkeypatch,
        subprocess.CalledProcessError(returncode=1, cmd=argv, stderr=None),
    )

    with pytest.raises(Exception) as excinfo:
        GitHubForge().merge_pr(CWD, BRANCH, "")

    message = str(excinfo.value)
    assert "gh pr merge" in message, message


def test_merge_pr_success_path_is_unchanged(monkeypatch):
    class _Proc:
        stdout = "  merged  \n"
        returncode = 0
        stderr = ""

    seen = []

    def _run(self, argv, cwd=None, **kw):
        seen.append(list(argv))
        assert kw.get("check") is True, "merge_pr must still fail on a refusal"
        assert kw.get("capture_output") is True, (
            "capture_output must stay on: without it CalledProcessError.stderr "
            "is None and the message fix silently reports nothing"
        )
        return _Proc()

    monkeypatch.setattr(GitHubForge, "_run", _run)

    assert GitHubForge().merge_pr(CWD, BRANCH, SUBJECT) == "merged"
    assert seen[0] == ["gh", "pr", "merge", BRANCH, "--squash",
                       "--subject", SUBJECT]
