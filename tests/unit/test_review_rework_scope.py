"""Rework-review diff scoping (D-A): when the branch was rebased or had the
trunk merged in since the last review, `git diff {since_sha}..HEAD` picks up
the trunk's own changes and makes the reviewer flag work the story never
touched. These tests build real temporary git repositories (only the remote
is faked, as a local bare repo) and assert the diff handed to the reviewer
contains the story's own files and NOT the trunk's other files.
"""
import shlex
import subprocess

import pytest

from pipeline import persona as pper
from pipeline import review


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


def _init_repo(repo):
    _git("init", "-b", "master", cwd=repo)
    _git("config", "user.email", "a@b.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)


def _commit(repo, filename, content, message):
    (repo / filename).write_text(content)
    _git("add", filename, cwd=repo)
    _git("commit", "-m", message, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo).strip()


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


class _FakeDriver:
    def __init__(self):
        self.captured = {}

    def complete(self, prompt, **kwargs):
        self.captured["prompt"] = prompt
        return "VERDICT: APPROVE"


def _capture_prompt(monkeypatch, worktree, branch, since_sha):
    driver = _FakeDriver()
    monkeypatch.setattr(
        review.backend, "get_backend", lambda role, name=None: driver,
    )
    review._run_reviewer(str(worktree), branch, since_sha=since_sha)
    return driver.captured["prompt"]

def _story_repo(tmp_path):
    """master: m0 -> m1; branch agent/story-1 last reviewed at c1; the trunk
    then advances (other_story_file.py) while the story pushes its own new
    commit (story2.py). Returns the repo and the reviewed commit c1."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "master", str(origin)],
        check=True, capture_output=True, text=True, cwd=str(tmp_path),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _commit(repo, "base.py", "base = 0\n", "m0")
    _commit(repo, "base.py", "base = 1\n", "m1")
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-u", "origin", "master", cwd=repo)

    _git("checkout", "-b", "agent/story-1", cwd=repo)
    c1 = _commit(repo, "story1.py", "story_one = 1\n", "c1")
    _git("push", "-u", "origin", "agent/story-1", cwd=repo)

    _git("checkout", "master", cwd=repo)
    _commit(repo, "other_story_file.py", "other_story = True\n", "m2 other story")
    _git("push", "origin", "master", cwd=repo)

    _git("checkout", "agent/story-1", cwd=repo)
    _commit(repo, "story2.py", "story_two = 1\n", "c2 story change")
    return repo, c1

def test_rebased_branch_rework_diff_excludes_trunk_changes(
    agents_dir, tmp_path, monkeypatch,
):
    repo, c1 = _story_repo(tmp_path)
    _git("rebase", "master", cwd=repo)

    prompt = _capture_prompt(monkeypatch, repo, "agent/story-1", c1)

    diff_section = prompt.split("--- end diff ---")[0]
    assert "story2" in diff_section
    assert "other_story_file" not in diff_section


def test_merged_trunk_rework_diff_excludes_trunk_changes(
    agents_dir, tmp_path, monkeypatch,
):
    repo, c1 = _story_repo(tmp_path)
    _git("merge", "--no-edit", "master", cwd=repo)

    prompt = _capture_prompt(monkeypatch, repo, "agent/story-1", c1)

    diff_section = prompt.split("--- end diff ---")[0]
    assert "story2" in diff_section
    assert "other_story_file" not in diff_section


def _repo_with_trunk(tmp_path, trunk="master"):
    """A plain repo: trunk m0; branch agent/story-1 with one own commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", trunk, cwd=repo)
    _git("config", "user.email", "a@b.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    m0 = _commit(repo, "base.py", "base = 0\n", "m0")
    _git("checkout", "-b", "agent/story-1", cwd=repo)
    c1 = _commit(repo, "story2.py", "story_two = 1\n", "c1")
    return repo, m0, c1


def test_no_drift_returns_none_and_keeps_todays_prompt(
    agents_dir, tmp_path, monkeypatch,
):
    from pipeline.review_rework_scope import scoped_rework_diff

    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "master", cwd=repo)
    _git("config", "user.email", "a@b.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    since_sha = _commit(repo, "base.py", "base = 0\n", "m0")
    _git("checkout", "-b", "agent/story-1", cwd=repo)
    _commit(repo, "story1.py", "one = 1\n", "c1")
    _commit(repo, "story2.py", "two = 2\n", "c2")
    _commit(repo, "story3.py", "three = 3\n", "c3")

    assert scoped_rework_diff(str(repo), since_sha, None) is None

    prompt = _capture_prompt(monkeypatch, repo, "agent/story-1", since_sha)

    inline_diff = review._materialize_review_diff(str(repo), since_sha)
    today_lead = (
        f"Review ONLY the new changes pushed since your last review "
        f"(commit {shlex.quote(since_sha)}). The implementer addressed "
        f"your prior feedback; do NOT re-review files that were already "
        f"approved and have not changed since. The full diff since your "
        f"last review is included below - do NOT re-run `git diff` to "
        f"fetch it again; Bash/view_file remain available for anything "
        f"not shown here (e.g. surrounding lines in a file, or a related "
        f"file the diff doesn't touch).\n\n"
        f"--- git diff {since_sha}..HEAD ---\n{inline_diff}\n"
        f"--- end diff ---\n\n"
    )
    assert prompt.startswith(today_lead)
    assert "Review ONLY the story's own changes" not in prompt
    assert "story files only" not in prompt


def test_orphaned_since_sha_is_drift_and_returns_a_scoped_diff(tmp_path):
    from pipeline.review_rework_scope import scoped_rework_diff

    repo, _m0, c1 = _repo_with_trunk(tmp_path)
    _git("checkout", "master", cwd=repo)
    _commit(repo, "other_story_file.py", "other = True\n", "m1 other story")
    _git("checkout", "agent/story-1", cwd=repo)
    _git("rebase", "master", cwd=repo)

    # since_sha still exists as an object, but is no longer an ancestor.
    assert _git("cat-file", "-t", c1, cwd=repo).strip() == "commit"
    check = subprocess.run(
        ["git", "merge-base", "--is-ancestor", c1, "HEAD"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert check.returncode == 1

    scoped = scoped_rework_diff(str(repo), c1, None)

    assert scoped is not None
    base, diff = scoped
    assert "story2" in diff
    assert "other_story_file" not in diff
    assert base


def test_empty_own_new_commit_list_then_sees_a_new_commit(tmp_path):
    from pipeline.review_rework_scope import scoped_rework_diff

    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "master", cwd=repo)
    _git("config", "user.email", "a@b.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    _commit(repo, "base.py", "base = 0\n", "m0")
    m1 = _commit(repo, "base.py", "base = 1\n", "m1")
    # The branch starts at m1 with no commits of its own; the trunk advances
    # and the branch merely merges it in.
    _git("checkout", "-b", "agent/story-1", cwd=repo)
    _git("checkout", "master", cwd=repo)
    _commit(repo, "other_story_file.py", "other = True\n", "m2 other story")
    _git("checkout", "agent/story-1", cwd=repo)
    _git("merge", "--no-edit", "master", cwd=repo)

    assert scoped_rework_diff(str(repo), m1, None) == ("", "")

    _commit(repo, "story2.py", "story_two = 2\n", "c2 own change")
    scoped = scoped_rework_diff(str(repo), m1, None)

    assert scoped is not None
    _base, diff = scoped
    assert "story_two = 2" in diff
    assert "other_story_file" not in diff
    assert "story_two = 2" in diff
    assert "other_story_file" not in diff


def test_oversize_diff_returns_none_and_boundary_is_inclusive(
    tmp_path, monkeypatch,
):
    from pipeline import review_rework_scope

    repo, m0, _c1 = _repo_with_trunk(tmp_path)
    _git("checkout", "master", cwd=repo)
    _commit(repo, "other_story_file.py", "other = True\n", "m1 other story")
    _git("checkout", "agent/story-1", cwd=repo)
    _git("rebase", "master", cwd=repo)

    scoped = review_rework_scope.scoped_rework_diff(str(repo), m0, None)
    assert scoped is not None
    base, diff = scoped

    monkeypatch.setattr(
        review_rework_scope, "REVIEWER_INLINE_DIFF_MAX_CHARS", len(diff) - 1,
    )
    assert review_rework_scope.scoped_rework_diff(str(repo), m0, None) is None

    monkeypatch.setattr(
        review_rework_scope, "REVIEWER_INLINE_DIFF_MAX_CHARS", len(diff),
    )
    assert review_rework_scope.scoped_rework_diff(str(repo), m0, None) == (
        base, diff,
    )


def test_non_repo_directory_returns_none_without_raising(tmp_path):
    from pipeline.review_rework_scope import scoped_rework_diff

    assert scoped_rework_diff(str(tmp_path), "deadbeef", None) is None


def test_fetch_failure_is_ignored_and_local_trunk_still_resolves(tmp_path):
    from pipeline.review_rework_scope import scoped_rework_diff

    repo, m0, _c1 = _repo_with_trunk(tmp_path, trunk="main")
    _git("remote", "add", "origin", str(tmp_path / "no-such-remote.git"), cwd=repo)
    _git("checkout", "main", cwd=repo)
    _commit(repo, "other_story_file.py", "other = True\n", "m1 other story")
    _git("checkout", "agent/story-1", cwd=repo)
    _git("rebase", "main", cwd=repo)

    scoped = scoped_rework_diff(str(repo), m0, None)

    assert scoped is not None
    _base, diff = scoped
    assert "story2" in diff
    assert "other_story_file" not in diff


def test_story_file_also_changed_on_trunk_shows_own_change_only(tmp_path):
    from pipeline.review_rework_scope import scoped_rework_diff

    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "master", cwd=repo)
    _git("config", "user.email", "a@b.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    _commit(repo, "story2.py", "line0 = 0\nline1 = 0\nline2 = 0\n", "m0")
    m1 = _commit(repo, "base.py", "base = 1\n", "m1")
    _git("checkout", "-b", "agent/story-1", cwd=repo)
    _commit(repo, "story2.py", "line0 = 0\nline1 = 0\nline2 = story_own\n", "c1")

    _git("checkout", "master", cwd=repo)
    _commit(
        repo, "story2.py",
        "line0 = master_changed\nline1 = 0\nline2 = 0\n", "m2 story2 on trunk",
    )
    _commit(repo, "other_story_file.py", "other = True\n", "m3 other story")
    _git("checkout", "agent/story-1", cwd=repo)
    _git("rebase", "master", cwd=repo)

    scoped = scoped_rework_diff(str(repo), m1, None)

    assert scoped is not None
    _base, diff = scoped
    assert "story_own" in diff
    assert "other_story_file" not in diff
