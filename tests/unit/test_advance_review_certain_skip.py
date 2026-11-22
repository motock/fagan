"""A review that would certainly skip is not spawned as a detached job.

With the detached review on, every advance tick used to spawn
``python -m pipeline.review_job`` for a story whose unchanged-since-review skip
had already been announced for its commit, and that job skipped again. The
caller now checks the same conditions the review would check first. Only when
all of them hold, nothing is spawned and the key is listed under ``skipped``.
Any doubt starts the job as before, so the first skip for a commit still goes
through the job and sends its notice.

Only git and the job spawn are mocked; the store, the story record and the
fingerprint check run for real.
"""

from __future__ import annotations

import json
import subprocess

import pytest

import pipeline.server  # (pre-existing import cycle)
from pipeline import advance_review, concurrency, review_job, review_logic

_PLAN = "certain-skip-plan"
_KEY = "s1"
_SHA = "b" * 40
_OTHER_SHA = "c" * 40


def _job():
    return {
        "pid": 4242,
        "result_path": "/tmp/nonexistent-review-result.json",
        "log_path": "/tmp/nonexistent-review-job.log",
        "started_at": "2026-01-01T00:00:00+00:00",
    }


def _certain_skip_story(worktree, **extra):
    story = {
        "key": _KEY,
        "title": "story s1",
        "status": "tests_passed",
        "risk": "low",
        "dispatch_attempts": 0,
        "last_reviewed_sha": _SHA,
        "skip_notified_sha": _SHA,
        "last_reviewed_logic_fingerprint": review_logic.review_logic_fingerprint(),
        "worktree": str(worktree),
    }
    story.update(extra)
    return story


def _write_manifest(tmp_path, story):
    path = tmp_path / f"{_PLAN}.manifest.json"
    path.write_text(
        json.dumps({"name": _PLAN, "paused": False, "stories": {_KEY: story}})
    )
    return path


def _stored(path):
    return json.loads(path.read_text())["stories"][_KEY]


@pytest.fixture(autouse=True)
def _isolate_plan_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", tmp_path)
    concurrency._held_plan_locks().clear()
    yield
    concurrency._held_plan_locks().clear()


@pytest.fixture
def worktree(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    return wt


@pytest.fixture
def git(monkeypatch):
    """Stand in for ``git rev-parse HEAD``; the test sets ``head`` or ``raises``."""
    calls = []
    state = {"head": _SHA, "raises": None}

    def _run(args, **kwargs):
        calls.append((list(args), kwargs))
        if state["raises"] is not None:
            raise state["raises"]
        return subprocess.CompletedProcess(
            args, 0, stdout=state["head"] + "\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _run)
    state["calls"] = calls
    return state


@pytest.fixture
def spawned(monkeypatch):
    started = []

    def _start(plan_name, story_key, state_root):
        started.append((plan_name, story_key))
        return _job()

    monkeypatch.setattr(review_job, "start_review_job", _start)
    return started


def _tick(path):
    story = json.loads(path.read_text())["stories"][_KEY]
    summary = {}
    advance_review.review_or_collect(_PLAN, _KEY, story, summary)
    return summary


# ---------------------------------------------------------------------------
# The certain skip


def test_certain_skip_starts_no_job_and_lists_the_story_as_skipped(
    tmp_path, worktree, git, spawned
):
    path = _write_manifest(tmp_path, _certain_skip_story(worktree))

    summary = _tick(path)

    assert spawned == []
    assert _KEY in summary["skipped"]
    assert _KEY not in summary.get("review_started", [])
    assert "review_job" not in _stored(path)


def test_two_consecutive_certain_skip_ticks_start_nothing_and_write_nothing(
    tmp_path, worktree, git, spawned
):
    path = _write_manifest(tmp_path, _certain_skip_story(worktree))
    before = path.read_bytes()

    first = _tick(path)
    second = _tick(path)

    assert spawned == []
    assert _KEY in first["skipped"]
    assert _KEY in second["skipped"]
    assert path.read_bytes() == before


def test_story_holding_a_job_handle_is_collected_as_today(
    tmp_path, worktree, git, spawned, monkeypatch
):
    job = _job()
    path = _write_manifest(tmp_path, _certain_skip_story(worktree, review_job=job))
    collected = []
    monkeypatch.setattr(
        review_job,
        "collect_review_job",
        lambda handle, timeout: collected.append(handle) or None,
    )

    summary = _tick(path)

    assert collected == [job]
    assert summary["review_pending"] == [_KEY]
    assert git["calls"] == []
    assert spawned == []


# ---------------------------------------------------------------------------
# Any doubt starts the job as before


def test_missing_skip_notified_sha_starts_a_job(tmp_path, worktree, git, spawned):
    story = _certain_skip_story(worktree)
    del story["skip_notified_sha"]
    path = _write_manifest(tmp_path, story)

    summary = _tick(path)

    assert spawned == [(_PLAN, _KEY)]
    assert summary["review_started"] == [_KEY]
    assert "skipped" not in summary


def test_skip_notified_for_another_sha_starts_a_job(tmp_path, worktree, git, spawned):
    path = _write_manifest(
        tmp_path, _certain_skip_story(worktree, skip_notified_sha=_OTHER_SHA)
    )

    summary = _tick(path)

    assert spawned == [(_PLAN, _KEY)]
    assert summary["review_started"] == [_KEY]


def test_head_moved_since_last_review_starts_a_job(tmp_path, worktree, git, spawned):
    git["head"] = _OTHER_SHA
    path = _write_manifest(tmp_path, _certain_skip_story(worktree))

    summary = _tick(path)

    assert spawned == [(_PLAN, _KEY)]
    assert summary["review_started"] == [_KEY]


def test_reviewing_logic_changed_starts_a_job(tmp_path, worktree, git, spawned):
    path = _write_manifest(
        tmp_path,
        _certain_skip_story(worktree, last_reviewed_logic_fingerprint="stale"),
    )

    summary = _tick(path)

    assert spawned == [(_PLAN, _KEY)]
    assert summary["review_started"] == [_KEY]


def test_no_last_reviewed_sha_starts_a_job_without_invoking_git(
    tmp_path, worktree, git, spawned
):
    story = _certain_skip_story(worktree)
    del story["last_reviewed_sha"]
    path = _write_manifest(tmp_path, story)

    summary = _tick(path)

    assert spawned == [(_PLAN, _KEY)]
    assert summary["review_started"] == [_KEY]
    assert git["calls"] == []


def test_missing_worktree_starts_a_job(tmp_path, git, spawned):
    path = _write_manifest(tmp_path, _certain_skip_story(tmp_path / "gone"))

    summary = _tick(path)

    assert spawned == [(_PLAN, _KEY)]
    assert summary["review_started"] == [_KEY]
    assert git["calls"] == []


def test_worktree_that_is_a_file_starts_a_job(tmp_path, git, spawned):
    not_a_dir = tmp_path / "wt-file"
    not_a_dir.write_text("x")
    path = _write_manifest(tmp_path, _certain_skip_story(not_a_dir))

    summary = _tick(path)

    assert spawned == [(_PLAN, _KEY)]
    assert summary["review_started"] == [_KEY]
    assert git["calls"] == []


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(128, ["git"]),
        subprocess.TimeoutExpired(["git"], 5),
        OSError("git is not installed"),
    ],
    ids=["called-process-error", "timeout", "os-error"],
)
def test_git_failure_starts_a_job_and_nothing_propagates(
    tmp_path, worktree, git, spawned, error
):
    git["raises"] = error
    path = _write_manifest(tmp_path, _certain_skip_story(worktree))

    summary = _tick(path)

    assert spawned == [(_PLAN, _KEY)]
    assert summary["review_started"] == [_KEY]
    assert "skipped" not in summary


def test_git_is_run_with_an_argument_list_in_the_worktree_with_a_timeout(
    tmp_path, worktree, git, spawned
):
    path = _write_manifest(tmp_path, _certain_skip_story(worktree))

    _tick(path)

    args, kwargs = git["calls"][0]
    assert args == ["git", "rev-parse", "HEAD"]
    assert kwargs.get("cwd") == str(worktree)
    assert kwargs.get("timeout")
    assert "shell" not in kwargs or kwargs["shell"] is False
