"""A detached worktree HEAD must never swallow a story's commits.

Live 2026-09-24 (PRH-2): a resumed story's worktree ended up on a detached HEAD.
The resume-time rebase ran on the detached line and reported success, so every
later commit landed on no branch, the pushed PR branch stayed stale, and the merge
gate was blocked on old content. _rebase_onto_master must re-attach the worktree
to its branch when that loses nothing, and refuse (as a conflict, so callers park
the story for a human) when re-attaching would orphan the branch's commits.

All tests use real git repositories; nothing about git is mocked.
"""

import subprocess

import pytest

from pipeline import server as p
from pipeline.rebase import _ensure_on_branch


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _out(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _attached_ref(wt):
    result = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=wt, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


@pytest.fixture
def repo_and_worktree(tmp_path):
    """Real origin + clone + linked worktree on branch agent/s1, with origin/main
    one commit ahead of the branch's base so the rebase has real work to do."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    _git("config", "user.email", "t@e", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "README.md").write_text("seed\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-q", "-m", "seed", cwd=seed)

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(origin)],
        check=True, capture_output=True, text=True,
    )
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(repo)],
        check=True, capture_output=True, text=True,
    )
    _git("config", "user.email", "t@e", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("checkout", "-q", "-b", "agent/s1", cwd=repo)
    (repo / "story.txt").write_text("story\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "story work", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)

    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", str(wt), "agent/s1", cwd=repo)
    _git("config", "user.email", "t@e", cwd=wt)
    _git("config", "user.name", "t", cwd=wt)

    (seed / "main_only.txt").write_text("main\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-q", "-m", "advance main", cwd=seed)
    _git("push", "-q", str(origin), "main", cwd=seed)
    return repo, wt


def _use_repo(monkeypatch, repo):
    monkeypatch.setattr(p, "REPO_ROOT", str(repo))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")


def _detach_and_commit(wt, name="detached.txt"):
    _git("checkout", "-q", "--detach", cwd=wt)
    (wt / name).write_text("made while detached\n")
    _git("add", "-A", cwd=wt)
    _git("commit", "-q", "-m", "work on a detached head", cwd=wt)


def _diverge_branch(repo):
    """Give agent/s1 a commit that the worktree's detached HEAD does not contain."""
    _git("update-ref", "refs/heads/agent/s1",
         _out("commit-tree", "-p", "agent/s1", "-m", "orphan-to-be",
              _out("rev-parse", "agent/s1^{tree}", cwd=repo), cwd=repo),
         cwd=repo)


# ---------- _ensure_on_branch ----------

def test_ensure_on_branch_is_a_no_op_when_already_attached(repo_and_worktree):
    _, wt = repo_and_worktree
    before = _out("rev-parse", "agent/s1", cwd=wt)
    result = _ensure_on_branch(str(wt), "agent/s1")
    assert result == {"ok": True, "error": ""}
    assert _attached_ref(wt) == "refs/heads/agent/s1"
    assert _out("rev-parse", "agent/s1", cwd=wt) == before


def test_ensure_on_branch_reattaches_a_detached_head_that_contains_the_branch(repo_and_worktree):
    _, wt = repo_and_worktree
    _detach_and_commit(wt)
    detached_head = _out("rev-parse", "HEAD", cwd=wt)
    assert _attached_ref(wt) is None
    result = _ensure_on_branch(str(wt), "agent/s1")
    assert result["ok"] is True, result
    assert _attached_ref(wt) == "refs/heads/agent/s1"
    assert _out("rev-parse", "agent/s1", cwd=wt) == detached_head


def test_ensure_on_branch_refuses_when_the_branch_has_commits_the_head_lacks(repo_and_worktree):
    repo, wt = repo_and_worktree
    _detach_and_commit(wt)
    _diverge_branch(repo)
    branch_tip = _out("rev-parse", "agent/s1", cwd=wt)
    result = _ensure_on_branch(str(wt), "agent/s1")
    assert result["ok"] is False
    assert "detached" in result["error"]
    assert _attached_ref(wt) is None
    assert _out("rev-parse", "agent/s1", cwd=wt) == branch_tip


def test_ensure_on_branch_refuses_when_the_branch_does_not_exist(repo_and_worktree):
    _, wt = repo_and_worktree
    _git("checkout", "-q", "--detach", cwd=wt)
    result = _ensure_on_branch(str(wt), "agent/no-such-branch")
    assert result["ok"] is False
    assert "detached" in result["error"]
    assert _attached_ref(wt) is None


def test_ensure_on_branch_does_not_call_an_unrunnable_git_a_detached_head(tmp_path, monkeypatch):
    def _raise_run(*args, **kwargs):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'git'")

    monkeypatch.setattr(subprocess, "run", _raise_run)
    assert _ensure_on_branch(str(tmp_path), "agent/x") == {"ok": True, "error": ""}


# ---------- _rebase_onto_master ----------

def test_rebase_reattaches_a_detached_worktree_so_the_branch_keeps_the_work(
    repo_and_worktree, monkeypatch,
):
    repo, wt = repo_and_worktree
    _use_repo(monkeypatch, repo)
    _detach_and_commit(wt)
    result = p._rebase_onto_master(str(wt), "agent/s1")
    assert result["ok"] is True, result
    assert _attached_ref(wt) == "refs/heads/agent/s1"
    branch_files = _out("ls-tree", "-r", "--name-only", "agent/s1", cwd=wt).splitlines()
    assert "detached.txt" in branch_files
    assert "main_only.txt" in branch_files


def test_rebase_of_an_attached_worktree_is_unchanged(repo_and_worktree, monkeypatch):
    repo, wt = repo_and_worktree
    _use_repo(monkeypatch, repo)
    result = p._rebase_onto_master(str(wt), "agent/s1")
    assert result["ok"] is True, result
    assert _attached_ref(wt) == "refs/heads/agent/s1"
    assert "main_only.txt" in _out("ls-tree", "-r", "--name-only", "agent/s1", cwd=wt).splitlines()


def test_rebase_parks_as_a_conflict_when_reattaching_would_orphan_commits(
    repo_and_worktree, monkeypatch,
):
    repo, wt = repo_and_worktree
    _use_repo(monkeypatch, repo)
    _detach_and_commit(wt)
    _diverge_branch(repo)
    branch_tip = _out("rev-parse", "agent/s1", cwd=wt)
    result = p._rebase_onto_master(str(wt), "agent/s1")
    assert result["ok"] is False
    assert result["conflict"] is True
    assert "detached" in result["error"]
    assert _out("rev-parse", "agent/s1", cwd=wt) == branch_tip
    assert _attached_ref(wt) is None
    status = subprocess.run(
        ["git", "status"], cwd=wt, capture_output=True, text=True, check=False
    ).stdout
    assert "rebase in progress" not in status
