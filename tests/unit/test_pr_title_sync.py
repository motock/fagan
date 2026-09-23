"""Regression tests: a rescoped story's PR title must be refreshed.

The squash-merge subject on master is the PR title read back at merge time.
A story rescoped after round 1 (brief rewritten) keeps its round-1 title
because ``_open_pr``'s "already exists" recovery branch returned the URL
without refreshing the title. ``_sync_pr_title`` fixes that: it runs
``gh pr edit <branch> --title <title>`` and swallows any failure, because a
failed title refresh must never fail the pipeline tick (the PR already exists
with a usable title).

These tests mock only ``subprocess.run`` in ``pipeline.pr``.
"""

import inspect
import subprocess
from pathlib import Path

import pytest

from pipeline import pr as pr_mod
from pipeline.pr import _open_pr, _pr_title

MODULE_PATH = Path(pr_mod.__file__)


def _sync():
    """The helper under test.

    Resolved lazily so this module still COLLECTS while the implementation is
    missing (a top-level import would abort collection and take unrelated
    tests down with it); the tests below then fail with the AttributeError
    that names the missing function.
    """
    return pr_mod._sync_pr_title
BRANCH = "agent/x-1"
WORKTREE = "wt"
STORY_KEY = "X-1"
# Summary already opens with the key: _pr_title must NOT double it, so the
# expected title distinguishes _pr_title from a blind f-string.
SUMMARY = "X-1: New"
TITLE = "X-1: New"
VIEW_URL = "https://example.test/pull/7"


def _source() -> str:
    return MODULE_PATH.read_text(encoding="utf-8")


def _completed(args, stdout=""):
    return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


class _Gh:
    """Answers the git/gh calls ``_open_pr`` makes and records every call."""

    def __init__(self, *, create_stdout="https://example.test/pull/1\n",
                 create_error=None, edit_error=None):
        self.calls = []
        self.create_stdout = create_stdout
        self.create_error = create_error
        self.edit_error = edit_error

    def __call__(self, args, **kw):
        args = list(args)
        self.calls.append((args, kw))
        if args[:2] == ["git", "rev-parse"]:
            return _completed(args, stdout=BRANCH + "\n")
        if args[:2] == ["git", "push"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "create"]:
            if self.create_error is not None:
                raise subprocess.CalledProcessError(
                    1, args, stderr=self.create_error)
            return _completed(args, stdout=self.create_stdout)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=VIEW_URL + "\n")
        if args[:3] == ["gh", "pr", "edit"]:
            if self.edit_error is not None:
                raise self.edit_error
            return _completed(args)
        raise AssertionError(f"unexpected subprocess.run call: {args}")

    def matching(self, *prefix):
        return [args for args, _kw in self.calls if args[:len(prefix)] == list(prefix)]

    def cwd_of(self, *prefix):
        for args, kw in self.calls:
            if args[:len(prefix)] == list(prefix):
                return kw.get("cwd")
        raise AssertionError(f"no call matching {prefix}")


def _patch(monkeypatch, fake):
    monkeypatch.setattr(pr_mod.subprocess, "run", fake)
    return fake


# ---------------------------------------------------------------------------
# _sync_pr_title
# ---------------------------------------------------------------------------
def test_sync_pr_title_runs_gh_pr_edit_with_cwd(monkeypatch):
    fake = _patch(monkeypatch, _Gh())

    assert _sync()(WORKTREE, BRANCH, TITLE) is None

    edits = fake.matching("gh", "pr", "edit")
    assert edits == [["gh", "pr", "edit", BRANCH, "--title", TITLE]]
    assert fake.cwd_of("gh", "pr", "edit") == WORKTREE


def test_sync_pr_title_passes_empty_title_through(monkeypatch):
    fake = _patch(monkeypatch, _Gh())

    assert _sync()(WORKTREE, BRANCH, "") is None

    assert fake.matching("gh", "pr", "edit") == [
        ["gh", "pr", "edit", BRANCH, "--title", ""]]


def test_sync_pr_title_swallows_called_process_error(monkeypatch):
    err = subprocess.CalledProcessError(1, ["gh", "pr", "edit"], stderr="nope")
    _patch(monkeypatch, _Gh(edit_error=err))

    assert _sync()(WORKTREE, BRANCH, TITLE) is None


def test_sync_pr_title_swallows_oserror(monkeypatch):
    _patch(monkeypatch, _Gh(edit_error=OSError("gh not found")))

    assert _sync()(WORKTREE, BRANCH, TITLE) is None


# ---------------------------------------------------------------------------
# _open_pr call site
# ---------------------------------------------------------------------------
def test_open_pr_already_exists_refreshes_title(monkeypatch):
    fake = _patch(monkeypatch, _Gh(create_error="a pull request already exists"))

    url = _open_pr(WORKTREE, STORY_KEY, {"summary": SUMMARY})

    assert url == VIEW_URL
    assert fake.matching("gh", "pr", "view") == [
        ["gh", "pr", "view", BRANCH, "--json", "url", "-q", ".url"]]
    assert fake.matching("gh", "pr", "edit") == [
        ["gh", "pr", "edit", BRANCH, "--title", _pr_title(STORY_KEY, SUMMARY)]]
    assert fake.cwd_of("gh", "pr", "edit") == WORKTREE


def test_open_pr_already_exists_returns_url_when_title_refresh_fails(monkeypatch):
    err = subprocess.CalledProcessError(1, ["gh", "pr", "edit"], stderr="boom")
    fake = _patch(monkeypatch, _Gh(
        create_error="a pull request already exists", edit_error=err))

    assert _open_pr(WORKTREE, STORY_KEY, {"summary": SUMMARY}) == VIEW_URL
    assert fake.matching("gh", "pr", "edit")


def test_open_pr_create_success_does_not_edit_title(monkeypatch):
    fake = _patch(monkeypatch, _Gh(create_stdout="https://example.test/pull/9\n"))

    url = _open_pr(WORKTREE, STORY_KEY, {"summary": SUMMARY})

    assert url == "https://example.test/pull/9"
    assert fake.matching("gh", "pr", "edit") == []
    assert fake.matching("gh", "pr", "view") == []


def test_open_pr_other_create_failure_propagates_without_edit(monkeypatch):
    fake = _patch(monkeypatch, _Gh(create_error="No commits between master and x"))

    with pytest.raises(subprocess.CalledProcessError):
        _open_pr(WORKTREE, STORY_KEY, {"summary": SUMMARY})

    assert fake.matching("gh", "pr", "edit") == []


# ---------------------------------------------------------------------------
# Structural requirements
# ---------------------------------------------------------------------------
def test_sync_pr_title_is_defined_above_open_pr():
    src = _source()
    assert "def _sync_pr_title(" in src
    assert src.index("def _sync_pr_title(") < src.index("def _open_pr(")


def test_sync_call_sits_between_pr_view_and_return_in_open_pr():
    body = inspect.getsource(_open_pr)
    view = body.index('"gh", "pr", "view"')
    sync = body.index("_sync_pr_title(worktree, branch, title)")
    ret = body.index("return proc.stdout.strip()", sync)
    assert view < sync < ret


def test_module_has_no_logging_import_and_keeps_survivors():
    src = _source()
    assert "import logging" not in src
    for name in ("_pr_title", "_resolve_story_branch", "_merge_pr",
                 "_format_review_comment", "_post_pr_comment"):
        assert f"def {name}(" in src


def test_open_pr_and_merge_pr_docstrings_kept():
    for fn in (_open_pr, pr_mod._merge_pr):
        assert "Tests mock this function" in (fn.__doc__ or "")
