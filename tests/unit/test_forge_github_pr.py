"""Tests for ``pipeline/forge_github.GitHubForge``.

The adapter owns every ``gh``/``git`` invocation for the PR verbs, all routed
through one runner seam (``_run``) that calls ``subprocess.run``. These tests
patch ``subprocess.run`` on the shared subprocess module - the same target the
pre-existing ``pipeline.pr`` tests use - so the seam is exercised for real.
"""

import subprocess

import pytest

from pipeline.forge_github import GitHubForge

WORKTREE = "/some/worktree"
BRANCH = "agent/x-1"
TITLE = "X-1: Do the thing"
BODY = "Automated PR for X-1."
SUBJECT = "X-1: Do the thing"
URL = "https://github.com/o/r/pull/7"


class _Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def forge():
    return GitHubForge()


def _patch_run(monkeypatch, handler):
    """Patch subprocess.run; returns the list of (argv, kwargs) seen."""
    seen = []

    def _fake_run(cmd, **kwargs):
        seen.append((list(cmd), kwargs))
        return handler(list(cmd), kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return seen


def _gh_verbs(seen):
    return [argv for argv, _ in seen if argv[:2] == ["gh", "pr"]]


# ---------------------------------------------------------------------------
# open_pr
# ---------------------------------------------------------------------------

def test_open_pr_returns_url(monkeypatch, forge):
    def handler(cmd, kwargs):
        if cmd[:2] == ["gh", "pr"] and cmd[2] == "create":
            return _Result(stdout=URL + "\n")
        return _Result()

    seen = _patch_run(monkeypatch, handler)

    assert forge.open_pr(WORKTREE, BRANCH, TITLE, BODY) == URL

    create = [a for a in _gh_verbs(seen) if a[2] == "create"]
    assert create == [[
        "gh", "pr", "create", "--title", TITLE, "--body", BODY,
        "--head", BRANCH,
    ]]
    # The branch is pushed first, force-with-lease, from the worktree.
    push = [argv for argv, _ in seen if argv[:2] == ["git", "push"]]
    assert push == [[
        "git", "push", "--force-with-lease", "-u", "origin", BRANCH,
    ]]
    assert seen[0][1]["cwd"] == WORKTREE


def test_open_pr_already_exists_recovers_url_and_refreshes_title(monkeypatch, forge):
    def handler(cmd, kwargs):
        if cmd[:3] == ["gh", "pr", "create"]:
            raise subprocess.CalledProcessError(
                1, cmd, output="", stderr="a pull request already exists",
            )
        if cmd[:3] == ["gh", "pr", "view"]:
            return _Result(stdout=URL + "\n")
        return _Result()

    seen = _patch_run(monkeypatch, handler)

    assert forge.open_pr(WORKTREE, BRANCH, TITLE, BODY) == URL

    view = [a for a in _gh_verbs(seen) if a[2] == "view"]
    assert view == [[
        "gh", "pr", "view", BRANCH, "--json", "url", "-q", ".url",
    ]]
    # The recovery path refreshes the title so a rescoped story's new title
    # lands on the existing PR.
    edit = [a for a in _gh_verbs(seen) if a[2] == "edit"]
    assert edit == [["gh", "pr", "edit", BRANCH, "--title", TITLE]]


def test_open_pr_other_failure_propagates(monkeypatch, forge):
    def handler(cmd, kwargs):
        if cmd[:3] == ["gh", "pr", "create"]:
            raise subprocess.CalledProcessError(
                1, cmd, output="", stderr="gh: not logged in",
            )
        return _Result()

    _patch_run(monkeypatch, handler)

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        forge.open_pr(WORKTREE, BRANCH, TITLE, BODY)

    assert "not logged in" in (excinfo.value.stderr or "")


def test_open_pr_does_not_call_gh_pr_edit(monkeypatch, forge):
    """The create path must never issue `gh pr edit`."""
    def handler(cmd, kwargs):
        if cmd[:3] == ["gh", "pr", "create"]:
            return _Result(stdout=URL + "\n")
        return _Result()

    seen = _patch_run(monkeypatch, handler)

    forge.open_pr(WORKTREE, BRANCH, TITLE, BODY)

    assert [a for a in _gh_verbs(seen) if a[2] == "edit"] == []


# ---------------------------------------------------------------------------
# update_pr_title / comment_pr
# ---------------------------------------------------------------------------

def test_update_pr_title_swallows_cli_failure(monkeypatch, forge):
    def handler(cmd, kwargs):
        raise subprocess.CalledProcessError(1, cmd, output="", stderr="boom")

    seen = _patch_run(monkeypatch, handler)

    # Must not raise: a failed title refresh never fails the pipeline tick.
    assert forge.update_pr_title(WORKTREE, BRANCH, TITLE) is None

    assert [argv for argv, _ in seen] == [
        ["gh", "pr", "edit", BRANCH, "--title", TITLE],
    ]


def test_comment_pr_propagates_failure(monkeypatch, forge):
    def handler(cmd, kwargs):
        raise subprocess.CalledProcessError(
            1, cmd, output="", stderr="gh: not logged in",
        )

    _patch_run(monkeypatch, handler)

    with pytest.raises(subprocess.CalledProcessError):
        forge.comment_pr(WORKTREE, BODY)


def test_comment_pr_posts_body(monkeypatch, forge):
    seen = _patch_run(monkeypatch, lambda cmd, kwargs: _Result())

    assert forge.comment_pr(WORKTREE, BODY) is None

    assert [argv for argv, _ in seen] == [
        ["gh", "pr", "comment", "--body", BODY],
    ]
    assert seen[0][1]["cwd"] == WORKTREE


# ---------------------------------------------------------------------------
# merge_pr
# ---------------------------------------------------------------------------

def test_merge_pr_passes_subject_and_does_not_delete_branch(monkeypatch, forge):
    def handler(cmd, kwargs):
        if cmd[:3] == ["gh", "pr", "view"]:
            return _Result(stdout=SUBJECT + "\n")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return _Result(stdout="merged\n")
        return _Result()

    seen = _patch_run(monkeypatch, handler)

    assert forge.merge_pr(WORKTREE, BRANCH, SUBJECT) == "merged"

    merge = [a for a in _gh_verbs(seen) if a[2] == "merge"]
    assert merge == [[
        "gh", "pr", "merge", BRANCH, "--squash", "--subject", SUBJECT,
    ]]
    # --delete-branch must never be passed: the branch is checked out in its
    # own worktree, so gh cannot switch away from it.
    assert all("--delete-branch" not in argv for argv, _ in seen)


# ---------------------------------------------------------------------------
# CI verbs delegate to pipeline.ci (no reimplementation)
# ---------------------------------------------------------------------------

def test_ci_verbs_delegate_to_pipeline_ci(monkeypatch, forge):
    from pipeline import ci as ci_mod

    calls = []

    def _status_once(branch, *, sha):
        calls.append(("status", branch, sha))
        return {"state": "pass", "error": ""}

    def _excerpt(branch):
        calls.append(("excerpt", branch))
        return "boom"

    def _rerun(sha):
        calls.append(("rerun", sha))
        return True

    def _has_ci():
        calls.append(("has_ci",))
        return True

    monkeypatch.setattr(ci_mod, "_ci_status_once", _status_once)
    monkeypatch.setattr(ci_mod, "_fetch_ci_failure_excerpt", _excerpt)
    monkeypatch.setattr(ci_mod, "_ci_rerun", _rerun)
    monkeypatch.setattr(ci_mod, "_repo_has_ci_configured", _has_ci)

    assert forge.checks_status(BRANCH, "abc123") == {"state": "pass", "error": ""}
    assert forge.failure_excerpt(BRANCH) == "boom"
    assert forge.rerun_failed("abc123") is True
    assert forge.has_ci_config("/repo") is True

    assert calls == [
        ("status", BRANCH, "abc123"),
        ("excerpt", BRANCH),
        ("rerun", "abc123"),
        ("has_ci",),
    ]


def test_capability_flags(forge):
    assert forge.validates_pre_merge is True
    assert forge.supports_comments is True
