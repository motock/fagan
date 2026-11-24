"""Tests for the new-commits probes when the local base branch is stale.

Live failure 2026-11-24 (story rch-02, plan ``resume-rebase-conflict-handoff``):
the worktree's local ``master`` was a long-lived ref at an older commit while
``origin/master`` had moved on. The branch carried zero commits of its own, yet
``git log master..agent/<key>`` listed the upstream commit that had merged
into ``origin/master``, so the empty-branch guard passed, the story reached
review, and the PR could not be opened.

``resolve_base`` deliberately never moves a local ref, so the probes must
exclude ``origin/<base>`` themselves. These tests use a real bare origin and a
clone, the same shape a dispatched worktree has.

Helpers are reached through the module (``git_ops._worktree_new_commits``)
rather than imported by name, so the helpers stay local to this file.
"""
import subprocess

from pipeline import git_ops


def _git(repo, *args):
    """Run git in ``repo``; raise on failure (fixture setup only)."""
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    )


def _commit(repo, name, message):
    (repo / name).write_text(f"{name}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _clone_with_stale_local_master(tmp_path):
    """A clone whose local ``master`` is at A while ``origin/master`` is at B.

    Commit A is pushed first, then commit B is pushed to origin. The clone's
    local ``master`` is reset back to A, so the push has updated the
    remote-tracking ref ``origin/master`` to B and left the local branch
    behind. Returns ``(clone, sha_a, sha_b)``.
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/master")
    _git(repo, "config", "user.email", "git-ops-test@example.test")
    _git(repo, "config", "user.name", "Git Ops Test")
    _git(repo, "remote", "add", "origin", str(origin))
    sha_a = _commit(repo, "a.txt", "commit A")
    _git(repo, "push", "-q", "origin", "master")
    sha_b = _commit(repo, "b.txt", "commit B")
    _git(repo, "push", "-q", "origin", "master")
    _git(repo, "reset", "-q", "--hard", sha_a)
    return repo, sha_a, sha_b


def _drop_origin_master(repo):
    """Delete the remote-tracking ref so only the local base remains."""
    _git(repo, "update-ref", "-d", "refs/remotes/origin/master")


# 1. Branch sits exactly on origin/master; local master is stale behind it.
def test_should_report_no_new_commits_when_local_base_is_stale_and_branch_equals_origin_base(
    tmp_path,
):
    repo, _sha_a, sha_b = _clone_with_stale_local_master(tmp_path)
    _git(repo, "checkout", "-qb", "agent/s1", sha_b)

    assert git_ops._worktree_new_commits(repo, "s1", "master") == ("no", "master")
    assert git_ops._worktree_has_new_commits(repo, "s1", "master") is False


# 2. Branch carries one commit of its own on top of origin/master.
def test_should_report_new_commits_when_the_branch_has_its_own_commit_over_a_stale_base(
    tmp_path,
):
    repo, _sha_a, sha_b = _clone_with_stale_local_master(tmp_path)
    _git(repo, "checkout", "-qb", "agent/s1", sha_b)
    _commit(repo, "c.txt", "commit C")

    assert git_ops._worktree_new_commits(repo, "s1", "master")[0] == "yes"
    assert git_ops._worktree_has_new_commits(repo, "s1", "master") is True


# 3. No origin/master here: the local base is the only reference, as before.
def test_should_keep_the_local_base_behaviour_when_origin_base_is_absent(tmp_path):
    repo, _sha_a, sha_b = _clone_with_stale_local_master(tmp_path)
    _drop_origin_master(repo)
    _git(repo, "checkout", "-qb", "agent/s1", sha_b)

    assert git_ops._worktree_new_commits(repo, "s1", "master") == ("yes", "master")
    assert git_ops._worktree_has_new_commits(repo, "s1", "master") is True


# 4. Commits already merged upstream are not the story's non-WIP work.
def test_should_not_count_merged_upstream_commits_as_non_wip_work(tmp_path):
    repo, _sha_a, sha_b = _clone_with_stale_local_master(tmp_path)
    _git(repo, "checkout", "-qb", "agent/s1", sha_b)

    assert git_ops._worktree_has_non_wip_commits(repo, "s1", "master") is False


# 4b. A real non-WIP commit over the stale base still counts.
def test_should_count_a_real_commit_over_a_stale_base_as_non_wip_work(tmp_path):
    repo, _sha_a, sha_b = _clone_with_stale_local_master(tmp_path)
    _git(repo, "checkout", "-qb", "agent/s1", sha_b)
    _commit(repo, "c.txt", "feat: add the thing")

    assert git_ops._worktree_has_non_wip_commits(repo, "s1", "master") is True


# 4c. A WIP checkpoint over the stale base does not count as non-WIP work.
def test_should_not_count_a_wip_checkpoint_over_a_stale_base_as_non_wip_work(tmp_path):
    repo, _sha_a, sha_b = _clone_with_stale_local_master(tmp_path)
    _git(repo, "checkout", "-qb", "agent/s1", sha_b)
    _commit(repo, "c.txt", "wip(s1): parked on rebase")

    assert git_ops._worktree_has_non_wip_commits(repo, "s1", "master") is False


# 5. Negative: a base that resolves nowhere stays "unknown", never "no".
def test_should_report_unknown_when_the_base_resolves_nowhere(tmp_path):
    repo, _sha_a, sha_b = _clone_with_stale_local_master(tmp_path)
    _git(repo, "checkout", "-qb", "agent/s1", sha_b)

    assert git_ops._worktree_new_commits(repo, "s1", "no-such-base") == ("unknown", "")
