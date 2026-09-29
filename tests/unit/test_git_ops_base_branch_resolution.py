"""Tests for base-branch resolution in ``pipeline.git_ops``' new-commits probe.

Live failure 2026-09-29 (plan ``language-agnostic-gates``): the scheduler ran
the triage sweep with a sentinel ``REPO_ROOT``, so ``_default_branch()``
answered ``main`` while the worktree's own repo (fagan) has ``master``. The
probe ran ``git log main..agent/lag-2``, git exited non-zero, and the helper
reported "no new commits" for a branch carrying five -- a false fact the
overlord then ruled on. The same helper backs ``story_status``'s
``empty_agent_branch`` verdict.

The fix resolves the base inside the worktree's own repo and makes "no commits"
distinguishable from "no idea". These tests build a real throwaway repo under
``tmp_path`` with ``git init`` (an accepted external-boundary fixture, same
pattern as ``tests/unit/test_triage_evidence_changed_files.py``). No existing
test file is touched.

Helpers are reached through the module (``git_ops._rev_parses``) rather than
imported by name: the new names do not exist yet, and a module-level
``from ... import`` would make bare ``pytest --collect-only`` exit non-zero,
breaking unrelated collection tests. Attribute access keeps the failure
localised to this file.
"""
import inspect
import subprocess

import pytest

from pipeline import git_ops


def _git(repo, *args):
    """Run git in ``repo``; raise on failure (fixture setup only)."""
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    )


def _init_repo(tmp_path):
    """A throwaway repo with one base commit on ``master`` (its default branch)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    # Pin the branch name: `git init` may pick `main` on newer gits, and this
    # story is precisely about master-vs-main confusion.
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/master")
    _git(repo, "config", "user.email", "git-ops-test@example.test")
    _git(repo, "config", "user.name", "Git Ops Test")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _add_origin_head(repo, target="refs/remotes/origin/master"):
    """Model a real clone: ``origin/master`` plus ``origin/HEAD`` -> it."""
    _git(repo, "update-ref", "refs/remotes/origin/master", "master")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", target)


def _agent_branch(repo, commit=True):
    """Check out ``agent/s1``; optionally land one commit on it."""
    _git(repo, "checkout", "-qb", "agent/s1")
    if commit:
        (repo / "work.txt").write_text("work\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "work")


# 1. passed base resolves here, branch has a commit.
def test_passed_base_resolves_and_branch_has_commit(tmp_path):
    repo = _init_repo(tmp_path)
    _add_origin_head(repo)
    _agent_branch(repo, commit=True)

    assert git_ops._worktree_new_commits(repo, "s1", "master") == ("yes", "master")
    assert git_ops._worktree_has_new_commits(repo, "s1", "master") is True


# 2. passed base ABSENT here -> fall back to the worktree's own origin/HEAD.
#    The live bug: on master today the bool is False.
def test_absent_base_falls_back_to_worktree_origin_head(tmp_path):
    repo = _init_repo(tmp_path)
    _add_origin_head(repo)
    _agent_branch(repo, commit=True)

    assert git_ops._worktree_new_commits(repo, "s1", "main") == ("yes", "master")
    assert git_ops._worktree_has_new_commits(repo, "s1", "main") is True


# 3. resolvable base, branch at base's tip -> a genuine "no".
def test_branch_at_base_tip_is_a_genuine_no(tmp_path):
    repo = _init_repo(tmp_path)
    _add_origin_head(repo)
    _agent_branch(repo, commit=False)

    assert git_ops._worktree_new_commits(repo, "s1", "master") == ("no", "master")
    assert git_ops._worktree_has_new_commits(repo, "s1", "master") is False


# 4. no origin/HEAD and the passed base is absent -> "unknown", not "no".
def test_no_origin_head_and_absent_base_is_unknown(tmp_path):
    repo = _init_repo(tmp_path)
    _agent_branch(repo, commit=True)

    assert git_ops._worktree_new_commits(repo, "s1", "main") == ("unknown", "")
    assert git_ops._worktree_has_new_commits(repo, "s1", "main") is False


# 5. base resolves but the agent branch does not exist -> "unknown", base kept.
def test_missing_agent_branch_is_unknown_with_base(tmp_path):
    repo = _init_repo(tmp_path)
    _add_origin_head(repo)

    assert git_ops._worktree_new_commits(repo, "s1", "master") == ("unknown", "master")
    assert git_ops._worktree_has_new_commits(repo, "s1", "master") is False


# 6. empty rev never spawns git and is not a commit.
def test_empty_rev_is_false_without_spawning_git(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("git must not be spawned for an empty rev")

    monkeypatch.setattr(git_ops.subprocess, "run", _boom)
    assert git_ops._rev_parses(repo, "") is False


# 7. an unresolvable base is "unknown", never "no".
def test_unresolvable_base_is_unknown_not_no(tmp_path):
    repo = _init_repo(tmp_path)
    _agent_branch(repo, commit=True)

    state, _base = git_ops._worktree_new_commits(repo, "s1", "main")
    assert state == "unknown"


# 8. the returned base names the branch actually compared.
def test_returned_base_is_the_one_actually_compared(tmp_path):
    repo = _init_repo(tmp_path)
    _add_origin_head(repo)
    _agent_branch(repo, commit=True)

    state, base = git_ops._worktree_new_commits(repo, "s1", "main")
    assert state == "yes"
    assert base == "master"
    assert base != "main"


# _resolve_base_branch: preference, fallback, and the OSError contract.
def test_resolve_base_branch_prefers_the_passed_base(tmp_path):
    repo = _init_repo(tmp_path)
    _add_origin_head(repo)

    assert git_ops._resolve_base_branch(repo, "master") == "master"


def test_resolve_base_branch_falls_back_to_origin_head(tmp_path):
    repo = _init_repo(tmp_path)
    _add_origin_head(repo)

    assert git_ops._resolve_base_branch(repo, "main") == "master"


def test_resolve_base_branch_strips_only_the_origin_prefix(tmp_path):
    # origin/HEAD may point at a local ref; the branch name must come back bare.
    repo = _init_repo(tmp_path)
    _add_origin_head(repo, target="refs/heads/master")

    assert git_ops._resolve_base_branch(repo, "main") == "master"


def test_resolve_base_branch_returns_empty_when_nothing_resolves(tmp_path):
    repo = _init_repo(tmp_path)

    assert git_ops._resolve_base_branch(repo, "main") == ""


def test_resolve_base_branch_returns_passed_base_when_git_unconsultable(tmp_path):
    # A cwd that does not exist makes subprocess raise OSError (FileNotFoundError).
    missing = tmp_path / "not-a-repo"

    assert git_ops._resolve_base_branch(missing, "main") == "main"


def test_rev_parses_raises_oserror_when_git_unconsultable(tmp_path):
    missing = tmp_path / "not-a-repo"

    with pytest.raises(OSError):
        git_ops._rev_parses(missing, "master")


# The bool wrapper's name/signature are load-bearing (~60 tests patch it).
def test_has_new_commits_signature_is_unchanged():
    sig = inspect.signature(git_ops._worktree_has_new_commits)

    assert list(sig.parameters) == ["worktree", "story_key", "base_branch"]
    assert sig.return_annotation in (bool, "bool")


def test_has_new_commits_stays_reachable_through_triage():
    # triage_story_actions resolves it via _ModuleRef("pipeline.triage", ...),
    # so the name must survive the rewrite and still be the same function.
    from pipeline import triage

    assert triage._worktree_has_new_commits is git_ops._worktree_has_new_commits
