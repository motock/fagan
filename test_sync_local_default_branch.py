"""Tests for _sync_local_default_branch: opportunistic fast-forward of
REPO_ROOT's local default branch to origin, run once per advance_pipeline
tick (Mode 10: a stale local branch makes the next worktree-creation
dispatch fail its fast-forward after any merge lands on origin).

Uses two real local git repos (a bare "origin" + a working clone), only
mocking _default_branch to avoid re-testing its own origin/HEAD-symref
detection here.
"""
import subprocess

import pytest

import pipeline.server as p


def run(args, cwd):
    return subprocess.run(args, check=False, cwd=cwd, capture_output=True, text=True)


def make_origin_and_clone(tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    run(["git", "init", "--bare"], origin)

    seed = tmp_path / "seed"
    seed.mkdir()
    run(["git", "init"], seed)
    run(["git", "config", "user.email", "a@b.c"], seed)
    run(["git", "config", "user.name", "test"], seed)
    (seed / "file.txt").write_text("initial")
    run(["git", "add", "."], seed)
    run(["git", "commit", "-m", "init"], seed)
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], seed).stdout.strip()
    run(["git", "remote", "add", "origin", str(origin)], seed)
    run(["git", "push", "origin", branch], seed)

    clone = tmp_path / "clone"
    run(["git", "clone", str(origin), str(clone)], tmp_path)
    run(["git", "config", "user.email", "a@b.c"], clone)
    run(["git", "config", "user.name", "test"], clone)
    return origin, seed, clone, branch


@pytest.fixture
def repo(tmp_path, monkeypatch):
    origin, seed, clone, branch = make_origin_and_clone(tmp_path)
    monkeypatch.setattr(p, "REPO_ROOT", clone)
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    return origin, seed, clone, branch


def push_new_commit(seed, origin, branch, filename="new.txt"):
    (seed / filename).write_text("more")
    run(["git", "add", "."], seed)
    run(["git", "commit", "-m", "more"], seed)
    run(["git", "push", "origin", branch], seed)


def test_fast_forwards_when_strictly_behind(repo):
    origin, seed, clone, branch = repo
    push_new_commit(seed, origin, branch)
    result = p._sync_local_default_branch()
    assert result == {"ok": True, "synced": True, "behind": 1}
    head = run(["git", "rev-parse", "HEAD"], clone).stdout.strip()
    origin_head = run(["git", "rev-parse", branch], origin).stdout.strip()
    assert head == origin_head


def test_noop_when_already_up_to_date(repo):
    _origin, _seed, _clone, _branch = repo
    result = p._sync_local_default_branch()
    assert result == {
        "ok": True, "synced": False, "reason": "up_to_date_or_diverged",
        "ahead": 0, "behind": 0,
    }


def test_noop_when_diverged_does_not_touch_local_commit(repo):
    origin, seed, clone, branch = repo
    push_new_commit(seed, origin, branch)
    (clone / "local_only.txt").write_text("local work")
    run(["git", "add", "."], clone)
    run(["git", "commit", "-m", "local work"], clone)
    local_head_before = run(["git", "rev-parse", "HEAD"], clone).stdout.strip()

    result = p._sync_local_default_branch()

    assert result["ok"] is True
    assert result["synced"] is False
    assert result["reason"] == "up_to_date_or_diverged"
    assert result["ahead"] == 1
    assert result["behind"] == 1
    local_head_after = run(["git", "rev-parse", "HEAD"], clone).stdout.strip()
    assert local_head_after == local_head_before


def test_noop_when_checked_out_on_a_different_branch(repo):
    origin, seed, clone, branch = repo
    push_new_commit(seed, origin, branch)
    run(["git", "checkout", "-b", "some-feature-branch"], clone)
    head_before = run(["git", "rev-parse", "HEAD"], clone).stdout.strip()

    result = p._sync_local_default_branch()

    assert result == {"ok": True, "synced": False, "reason": "not_on_default_branch"}
    current_branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], clone).stdout.strip()
    assert current_branch == "some-feature-branch"
    assert run(["git", "rev-parse", "HEAD"], clone).stdout.strip() == head_before


def test_noop_when_worktree_is_dirty(repo):
    origin, seed, clone, branch = repo
    push_new_commit(seed, origin, branch)
    (clone / "file.txt").write_text("uncommitted local edit")

    result = p._sync_local_default_branch()

    assert result == {"ok": True, "synced": False, "reason": "dirty_worktree"}
    assert (clone / "file.txt").read_text() == "uncommitted local edit"


def test_noop_when_fetch_fails(repo, monkeypatch):
    _origin, _seed, clone, _branch = repo
    run(["git", "remote", "set-url", "origin", "/nonexistent/path/does/not/exist"], clone)

    result = p._sync_local_default_branch()

    assert result == {"ok": True, "synced": False, "reason": "fetch_failed"}
