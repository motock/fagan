"""TDD tests: a harness error is reported as one, never as a commit mismatch.

A re-verify job that dies before it computes a sha writes ``state == "error"``
with ``sha == None``.  The merge gate used to compare the sha first, so that
result read as ``merge re-verify graded a different commit`` and the real
error never reached the operator.  The state is now read first: an error (or
an unrecognised state) is a harness error; the sha comparison still guards
``pass`` and ``fail`` and never authorises a merge on its own.

Only the detached-job seam (``collect_merge_reverify``) is faked; the gate
itself runs against a real git worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# isort: off
# pipeline.server is imported first on purpose: pre-existing import cycle
# (tests/unit/test_merge_reverify_gate.py uses the same order).
import pipeline.server  # noqa: F401
from pipeline import advance_merge, merge_reverify
# isort: on

KEY = "P1"
DIFFERENT = "merge re-verify graded a different commit"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_worktree(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "checkout", "-q", "-b", "agent/P1")
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """Run the gate for a story whose job grades the pushed head.

    The job seam returns the canned ``result``.  Returns ``(error, wait, story)``.
    """
    monkeypatch.setattr(merge_reverify, "detached_enabled", lambda: True)
    worktree, head = _make_worktree(tmp_path)

    def _run(result: dict | None) -> tuple[str, bool, dict]:
        monkeypatch.setattr(
            advance_merge, "collect_merge_reverify", lambda job, timeout: result
        )
        story = {
            "summary": "ready pr",
            "status": "pr_open",
            "worktree": str(worktree),
            "merge_reverify": {"pid": 4242, "sha": head, "started_at": "2026-11-22T12:00:00+00:00"},
        }
        error, wait = advance_merge._merge_reverify_gate(
            "go", KEY, story, str(worktree), head, {}
        )
        return error, wait, story

    return _run


# ---------------------------------------------------------------------------
# A harness error names the real error, not a commit mismatch
# ---------------------------------------------------------------------------


def test_error_result_without_a_sha_reports_the_harness_error(gate):
    error, wait, _story = gate(
        {"state": "error", "error": "merge re-verify raised FileNotFoundError", "sha": None}
    )

    assert "harness error" in error
    assert "FileNotFoundError" in error
    assert DIFFERENT not in error
    assert wait is False


def test_error_result_is_never_an_empty_pass(gate):
    error, _wait, _story = gate({"state": "error", "error": "merge re-verify timed out", "sha": None})

    assert error != ""


def test_unrecognised_state_without_a_sha_is_a_harness_error(gate):
    error, _wait, _story = gate({"state": "maybe", "error": "odd", "sha": None})

    assert error == "merge re-verify harness error: odd"


def test_error_result_keeps_the_log_tail_for_the_operator(gate):
    _error, _wait, story = gate(
        {"state": "error", "error": "boom", "sha": None, "log_tail": "trace line"}
    )

    assert story["merge_reverify_log_tail"] == "trace line"


# ---------------------------------------------------------------------------
# pass and fail still have to name the pushed sha
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        {"state": "pass", "error": "", "sha": "9f9f9f9"},
        {"state": "pass", "error": "", "sha": None},
        {"state": "fail", "error": "tests red", "sha": "9f9f9f9"},
    ],
    ids=["pass-other-sha", "pass-no-sha", "fail-other-sha"],
)
def test_pass_or_fail_for_another_commit_is_a_commit_mismatch(gate, result):
    error, wait, _story = gate(result)

    assert error == DIFFERENT
    assert wait is False


def test_pass_naming_the_pushed_sha_passes_the_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(merge_reverify, "detached_enabled", lambda: True)
    worktree, head = _make_worktree(tmp_path)
    monkeypatch.setattr(
        advance_merge,
        "collect_merge_reverify",
        lambda job, timeout: {"state": "pass", "error": "", "sha": head},
    )
    story = {
        "summary": "ready pr",
        "status": "pr_open",
        "worktree": str(worktree),
        "merge_reverify": {"pid": 4242, "sha": head, "started_at": "2026-11-22T12:00:00+00:00"},
    }

    error, wait = advance_merge._merge_reverify_gate(
        "go", KEY, story, str(worktree), head, {}
    )

    assert error == ""
    assert wait is False
