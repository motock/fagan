"""The GitHub adapter's pipeline-side commands use the bound team's own token.

``subprocess.run`` is patched on the shared subprocess module, as in
``test_forge_github_pr.py``. A team namespace's calls carry an ``env`` with the
seam's token; a default-namespace call carries no ``env`` kwarg at all. The
credential seam is stubbed; no test uses a real token value.
"""

import subprocess

import pytest

from pipeline import forge_auth, pr
from pipeline.actor import bind_namespace
from pipeline.forge_github import GitHubForge

WORKTREE = "/some/worktree"
BRANCH = "agent/x-1"
TEAM = "acme/web"
TEAM_TOKEN = "team-token-for-tests"
URL = "https://github.com/o/r/pull/7"


class _Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def seen(monkeypatch):
    """Patch subprocess.run to succeed; return the list of (argv, kwargs)."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        stdout = URL + "\n" if cmd[:3] == ["gh", "pr", "create"] else "[]"
        return _Result(stdout=stdout)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return calls


@pytest.fixture
def team_token(monkeypatch):
    monkeypatch.setattr(forge_auth, "resolve_for_injection", lambda ref: TEAM_TOKEN)


def _find(calls, *prefix):
    matches = [kw for argv, kw in calls if argv[: len(prefix)] == list(prefix)]
    assert len(matches) == 1, f"expected one call starting {prefix}, got {len(matches)}"
    return matches[0]


def test_open_pr_push_and_create_carry_the_team_token(seen, team_token):
    with bind_namespace(TEAM):
        GitHubForge().open_pr(WORKTREE, BRANCH, "X-1", "body")

    for prefix in (("git", "push"), ("gh", "pr", "create")):
        env = _find(seen, *prefix)["env"]
        assert env["GH_TOKEN"] == TEAM_TOKEN
        assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_open_pr_in_default_namespace_passes_no_env(seen):
    with bind_namespace("default"):
        GitHubForge().open_pr(WORKTREE, BRANCH, "X-1", "body")

    for prefix in (("git", "push"), ("gh", "pr", "create")):
        assert "env" not in _find(seen, *prefix)


def test_ci_verb_carries_the_team_token(seen, team_token, monkeypatch):
    monkeypatch.setattr("pipeline.ci._scoped_repo_cwd", lambda: WORKTREE)
    with bind_namespace(TEAM):
        GitHubForge().checks_snapshot(BRANCH, "")

    env = _find(seen, "gh", "pr", "checks")["env"]
    assert env["GH_TOKEN"] == TEAM_TOKEN


class _MergeForge:
    def merge_pr(self, worktree, branch, title):
        return "merged"

    def pr_title(self, worktree, branch):
        return "X-1"


def test_merge_branch_delete_push_carries_the_team_token(seen, team_token, monkeypatch):
    monkeypatch.setattr(pr, "_repo_root", lambda: "/repo")
    monkeypatch.setattr(pr, "_resolve_story_branch", lambda worktree, key: BRANCH)
    monkeypatch.setattr(pr, "get_forge", lambda root: _MergeForge())
    with bind_namespace(TEAM):
        pr._merge_pr(WORKTREE, "X-1")

    env = _find(seen, "git", "push", "origin", "--delete")["env"]
    assert env["GH_TOKEN"] == TEAM_TOKEN
