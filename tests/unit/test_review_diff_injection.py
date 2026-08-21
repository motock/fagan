"""Diff pre-materialization: _run_reviewer embeds `git diff` directly in the
review prompt instead of making the reviewer discover it turn-by-turn via
`git diff --stat` -> per-file `git diff` -> `view_file`. On a backend with no
prompt caching, each of those turns re-bills the whole growing transcript
from scratch (see REVIEWER_INLINE_DIFF_MAX_CHARS's config.py comment for the
measured production cost this addresses). A diff that can't be safely
materialized (no repo, no common base, or over budget) must fall back to the
exact prior explore-yourself instructions, unchanged.
"""
import subprocess

import pytest

from pipeline import persona as pper
from pipeline import review


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo_with_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "a@b.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "foo.py").write_text("def foo():\n    return 1\n")
    _git("add", "foo.py", cwd=repo)
    _git("commit", "-m", "base", cwd=repo)
    _git("checkout", "-b", "agent/story-1", cwd=repo)
    (repo / "foo.py").write_text("def foo():\n    return 2\n")
    _git("add", "foo.py", cwd=repo)
    _git("commit", "-m", "change", cwd=repo)
    return repo


class _FakeDriver:
    def __init__(self):
        self.captured = {}

    def complete(self, prompt, **kwargs):
        self.captured["prompt"] = prompt
        return "VERDICT: APPROVE"


def test_first_review_embeds_diff_and_skips_discovery(
    agents_dir, git_repo_with_diff, monkeypatch,
):
    driver = _FakeDriver()
    monkeypatch.setattr(review.backend, "get_backend", lambda role, name=None: driver)
    review._run_reviewer(str(git_repo_with_diff), "agent/story-1")
    prompt = driver.captured["prompt"]
    assert "--- git diff" in prompt
    assert "return 2" in prompt
    assert "do NOT re-run `git diff`" in prompt


def test_since_sha_review_embeds_only_the_scoped_diff(
    agents_dir, git_repo_with_diff, monkeypatch,
):
    since_sha = subprocess.run(
        ["git", "rev-parse", "main"], cwd=git_repo_with_diff,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    driver = _FakeDriver()
    monkeypatch.setattr(review.backend, "get_backend", lambda role, name=None: driver)
    review._run_reviewer(str(git_repo_with_diff), "agent/story-1", since_sha=since_sha)
    prompt = driver.captured["prompt"]
    assert f"--- git diff {since_sha}..HEAD ---" in prompt
    assert "return 2" in prompt


def test_diff_over_budget_falls_back_to_discovery_instructions(
    agents_dir, git_repo_with_diff, monkeypatch,
):
    monkeypatch.setattr(review, "REVIEWER_INLINE_DIFF_MAX_CHARS", 5)
    driver = _FakeDriver()
    monkeypatch.setattr(review.backend, "get_backend", lambda role, name=None: driver)
    review._run_reviewer(str(git_repo_with_diff), "agent/story-1")
    prompt = driver.captured["prompt"]
    assert "--- git diff" not in prompt
    assert "git diff --stat" in prompt


def test_no_git_repo_falls_back_to_discovery_instructions(
    agents_dir, tmp_path, monkeypatch,
):
    driver = _FakeDriver()
    monkeypatch.setattr(review.backend, "get_backend", lambda role, name=None: driver)
    review._run_reviewer(str(tmp_path / "does-not-exist"), "agent/story-1")
    prompt = driver.captured["prompt"]
    assert "--- git diff" not in prompt
    assert "git diff --stat" in prompt
