"""Remote branch guard exercised against real git: a bare origin and two clones.

Run with ``pytest -s tests/unit/test_branch_guard_two_clone.py`` to see the
measured refusal reason and the untouched SHA printed by the first test.
"""

import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from pipeline import branch_guard, dispatch

BRANCH = "agent/setup-01"
STORY_KEY = "SETUP-01"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _clone(origin: Path, dest: Path) -> Path:
    _git(dest.parent, "clone", str(origin), str(dest))
    _git(dest, "config", "user.name", "Test")
    _git(dest, "config", "user.email", "test@example.com")
    return dest


def _commit(repo: Path, filename: str) -> None:
    (repo / filename).write_text(filename)
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", f"add {filename}")


def _remote_sha(repo: Path, branch: str) -> str:
    return _git(repo, "ls-remote", "origin", branch).split()[0]


@pytest.fixture
def clones(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    clone_a = _clone(origin, tmp_path / "clone_a")
    _git(clone_a, "checkout", "-b", "main")
    _commit(clone_a, "README")
    _git(clone_a, "push", "origin", "main")
    clone_b = _clone(origin, tmp_path / "clone_b")
    _git(clone_a, "checkout", "-b", BRANCH)
    _commit(clone_a, "setup")
    _git(clone_a, "push", "origin", BRANCH)
    return clone_a, clone_b


@pytest.fixture
def guard(monkeypatch, clones):
    """Patch only the module-boundary collaborators; returns the call recorder."""
    _, clone_b = clones
    notifications = []

    @contextmanager
    def scoped_repo_root(_plan_name):
        yield clone_b

    # tests/unit/conftest.py stubs the probe for every module not named
    # test_branch_guard; restore the real one so git is actually queried.
    monkeypatch.setattr(
        dispatch, "remote_branch_exists", branch_guard.remote_branch_exists
    )
    monkeypatch.setattr(dispatch, "_scoped_repo_root", scoped_repo_root)
    monkeypatch.setattr(dispatch, "_atomic_write_json", lambda *_a, **_k: None)
    monkeypatch.setattr(
        dispatch, "_notify_user", lambda *a, **k: notifications.append((a, k))
    )
    return notifications


def _call(story_key: str, branch: str):
    story: dict = {}
    manifest = {"stories": {story_key: story}}
    result = dispatch._refuse_if_remote_branch_taken(
        "plan", story_key, story, branch, Path("manifest.json"), manifest
    )
    return result, story


def test_should_refuse_when_origin_already_holds_branch_and_leave_it_untouched(
    clones, guard
):
    clone_a, clone_b = clones
    sha_before = _remote_sha(clone_a, BRANCH)

    result, story = _call(STORY_KEY, BRANCH)

    sha_after = _remote_sha(clone_b, BRANCH)
    print(f"\nrefusal reason: {story['dispatch_block_reason']}")
    print(f"{BRANCH} on origin: before={sha_before} after={sha_after}")
    assert result["ok"] is False
    assert result["outcome"] == "remote_branch_exists"
    assert BRANCH in story["dispatch_block_reason"]
    assert sha_after == sha_before
    assert len(guard) == 1


def test_should_proceed_when_branch_absent_on_origin(guard):
    result, story = _call("OTHER-02", "agent/other-02")

    assert result is None
    assert "dispatch_block_reason" not in story
    assert guard == []


def test_should_refuse_without_raising_when_origin_unreachable(clones, guard):
    _, clone_b = clones
    missing = clone_b.parent / "does-not-exist.git"
    _git(clone_b, "remote", "set-url", "origin", str(missing))

    result, story = _call(STORY_KEY, BRANCH)

    assert result["ok"] is False
    assert result["outcome"] == "origin_unreachable"
    assert str(missing) not in story["dispatch_block_reason"]
    assert str(missing) not in result["error"]
