"""SQ-12: reviews run as detached jobs collected on a later advance.

Before this story every ``tests_passed`` story was reviewed inline inside the
tick's review loop: ``review_story`` is a 60-400s model call, so one slow
review held the whole advance (and, with the plan lock, every other actor that
needed the plan). SQ-11 added :mod:`pipeline.review_job` - the child process
that runs one review and writes its result. This suite pins the caller side:
:func:`pipeline.advance_review.review_or_collect` starts the job on the first
tick and collects its result on a later one, so the tick keeps its budget.

The invariants under test:

* A pending job leaves the story ``tests_passed`` and persists nothing - the
  next tick polls again.
* Only a ``done`` result removes the job; its ``status`` (and a
  ``rate_limited`` deferral) lands in the tick summary exactly as the inline
  path reports it, so downstream readers see no shape change.
* An ``error`` result is never a verdict: the field is dropped so the next
  advance starts a fresh job, and the operator is notified once with no path
  or log text in the message.
* A live job whose story an operator moved is cancelled, never collected.
* A finished job's result file wakes its plan immediately
  (``watchers.JOB_RESULT_FIELDS``).
"""

# ruff: noqa: I001  -- pipeline.server must be imported before any other
# pipeline module (pre-existing import cycle; see test_triage_cap_silent_skip).
from __future__ import annotations

import json
import logging

import pytest

# pipeline.server is imported explicitly; import order is not required for
# cold-importability (every pipeline module imports cold, see
# tests/unit/test_triage_cap_silent_skip.py), but the explicit import keeps this
# file's module graph identical to the rest of the unit suite.
import pipeline.server  # noqa: F401  (pre-existing import cycle)
import pipeline.advance as advance_module
import pipeline.config as config_module
from pipeline import advance_review, concurrency, review_job, watchers

_PLAN = "detached-review-plan"
_KEY = "s1"


# ---------------------------------------------------------------------------
# Fixtures / helpers


class _FakeBackend:
    def get_backend(self, *_args, **_kwargs):
        return self

    def resource_status(self, **_kwargs):
        return {"ok": True}


class _FakeAutonomy:
    def __init__(self, value="gated"):
        self._value_ = value

    def _value(self):
        return self._value_


class _FakeStore:
    def __init__(self, path):
        self.path = path

    def manifest_path(self, plan_name):
        return self.path

    def get_manifest(self, plan_name):
        return json.loads(self.path.read_text())


def _story(**extra):
    story = {
        "key": _KEY,
        "title": "story s1",
        "status": "tests_passed",
        "risk": "low",
        "dispatch_attempts": 0,
    }
    story.update(extra)
    return story


def _write_manifest(tmp_path, stories=None):
    manifest = {
        "name": _PLAN,
        "paused": False,
        "stories": stories if stories is not None else {_KEY: _story()},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _stories(path):
    return json.loads(path.read_text())["stories"]


@pytest.fixture(autouse=True)
def _isolate_plan_lock(tmp_path, monkeypatch):
    """Point the real per-plan flock at ``tmp_path`` with a clean held-set."""
    monkeypatch.setattr(concurrency, "PLAN_DIR", tmp_path)
    concurrency._held_plan_locks().clear()
    yield
    concurrency._held_plan_locks().clear()


def _job(pid=4242, result_path=None, log_path="/tmp/nonexistent-review-job.log"):
    return {
        "pid": pid,
        "result_path": str(result_path or "/tmp/nonexistent-review-result.json"),
        "log_path": log_path,
        "started_at": "2026-01-01T00:00:00+00:00",
    }


def _seed_tick(monkeypatch, path, notifications):
    """Wire every seam the tick body touches, as the sibling advance suites do."""
    monkeypatch.setattr(advance_module, "_store", _FakeStore(path))
    monkeypatch.setattr(advance_module, "backend", _FakeBackend())

    def _role_resource_ok(role, **_kwargs):
        return True, ""

    monkeypatch.setattr(advance_module, "_role_resource_ok", _role_resource_ok)
    monkeypatch.setattr(
        advance_module, "_count_on_device_in_progress_agents", lambda: 0
    )
    monkeypatch.setattr(
        advance_module, "PIPELINE_AUTONOMY", _FakeAutonomy("gated"), raising=True
    )
    monkeypatch.setattr(advance_module, "_adjudicate_merges", lambda *a, **k: None)
    monkeypatch.setattr(
        advance_module,
        "_notify_user",
        lambda _plan, message, **kwargs: notifications.append((message, kwargs)),
    )
    monkeypatch.setattr(advance_module, "interrupt_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        advance_module, "check_story_status", lambda *a, **k: {"ok": True}
    )
    monkeypatch.setattr(advance_module, "dispatch_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        advance_module, "_maybe_record_retro", lambda *a, **k: None, raising=False
    )
    monkeypatch.setattr(
        config_module, "PIPELINE_MAX_DISPATCH_PER_TICK", 1, raising=False
    )
    monkeypatch.setattr(
        advance_module, "PIPELINE_MAX_DISPATCH_PER_TICK", 1, raising=False
    )
    monkeypatch.setattr(config_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    monkeypatch.setattr(advance_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    monkeypatch.setattr(config_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(advance_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)


def _run_tick(plan_name=_PLAN):
    """Drive the real tick body while holding the real plan lock."""
    with concurrency._plan_lock(plan_name) as acquired:
        assert acquired, "test setup: failed to acquire the plan lock"
        return advance_module._advance_pipeline_locked_impl(plan_name)


# ---------------------------------------------------------------------------
# The tick's dispatch to the detached path


class TestTickDispatch:
    def test_inline_path_never_calls_review_or_collect(self, monkeypatch, tmp_path):
        """Under pytest (and with PIPELINE_REVIEW_DETACHED=0) the loop keeps the
        inline ``review_story`` call and never touches the detached helper."""
        path = _write_manifest(tmp_path)
        notifications = []
        _seed_tick(monkeypatch, path, notifications)
        monkeypatch.setattr(review_job, "detached_enabled", lambda: False)
        calls = []
        monkeypatch.setattr(
            advance_module,
            "review_or_collect",
            lambda *a, **k: calls.append(a),
        )
        reviewed = []
        monkeypatch.setattr(
            advance_module,
            "review_story",
            lambda plan, key: reviewed.append(key) or {"ok": True, "status": "pr_open"},
        )

        _run_tick()

        assert calls == []
        assert reviewed == [_KEY]

    def test_detached_first_advance_starts_a_job(self, monkeypatch, tmp_path):
        """A detached tick starts the job, persists the handle, leaves the story
        ``tests_passed`` and does not call ``review_story`` in-process."""
        path = _write_manifest(tmp_path)
        notifications = []
        _seed_tick(monkeypatch, path, notifications)
        monkeypatch.setattr(review_job, "detached_enabled", lambda: True)
        started = []

        def _start(plan_name, story_key, state_root):
            started.append((plan_name, story_key, state_root))
            return _job()

        monkeypatch.setattr(review_job, "start_review_job", _start)
        reviewed = []
        monkeypatch.setattr(
            advance_module,
            "review_story",
            lambda plan, key: reviewed.append(key) or {"ok": True, "status": "pr_open"},
        )

        summary = _run_tick()

        assert started == [(_PLAN, _KEY, tmp_path)]
        assert reviewed == []
        story = _stories(path)[_KEY]
        assert story["status"] == "tests_passed"
        assert story["review_job"]["pid"] == 4242
        assert summary["review_started"] == [_KEY]
        assert summary["advanced"] == []

    def test_tick_cancels_a_live_job_whose_story_an_operator_moved(
        self, monkeypatch, tmp_path
    ):
        """The tick, not just the helper, must cancel: a story an operator moved
        out of ``tests_passed`` while its job ran still has to reach
        ``review_or_collect``, or the orphan child runs on forever."""
        path = _write_manifest(
            tmp_path, {_KEY: _story(status="pr_open", review_job=_job())}
        )
        notifications = []
        _seed_tick(monkeypatch, path, notifications)
        monkeypatch.setattr(review_job, "detached_enabled", lambda: True)
        cancelled = []
        monkeypatch.setattr(
            review_job, "cancel_review_job", lambda job: cancelled.append(job)
        )

        def _never_collect(job, timeout):
            raise AssertionError("a moved story's job must not be collected")

        def _never_start(*args, **kwargs):
            raise AssertionError("a moved story must not start a new job")

        monkeypatch.setattr(review_job, "collect_review_job", _never_collect)
        monkeypatch.setattr(review_job, "start_review_job", _never_start)
        reviewed = []
        monkeypatch.setattr(
            advance_module,
            "review_story",
            lambda plan, key: reviewed.append(key) or {"ok": True, "status": "pr_open"},
        )

        summary = _run_tick()

        assert cancelled == [_job()]
        assert "review_job" not in _stories(path)[_KEY]
        assert reviewed == []
        assert summary["advanced"] == []


# ---------------------------------------------------------------------------
# review_or_collect: start / pending / done / error / operator move


class TestStartJob:
    def test_start_persists_the_handle_and_leaves_the_story_in_place(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(tmp_path)
        story = _stories(path)[_KEY]
        started = []
        monkeypatch.setattr(
            review_job,
            "start_review_job",
            lambda plan, key, root: started.append((plan, key, root)) or _job(),
        )
        summary = {}

        advance_review.review_or_collect(_PLAN, _KEY, story, path, summary)

        assert started == [(_PLAN, _KEY, tmp_path)]
        assert summary["review_started"] == [_KEY]
        assert summary.get("review_pending", []) == []
        stored = _stories(path)[_KEY]
        assert stored["status"] == "tests_passed"
        assert stored["review_job"]["pid"] == 4242

    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            ("/var/lib/pipeline-state", "/var/lib/pipeline-state"),
            ("state", "RELATIVE"),
            (None, "MANIFEST_DIR"),
        ],
    )
    def test_state_root_follows_the_merge_reverify_rule(
        self, monkeypatch, tmp_path, env_value, expected
    ):
        path = _write_manifest(tmp_path)
        story = _stories(path)[_KEY]
        started = []
        monkeypatch.setattr(
            review_job,
            "start_review_job",
            lambda plan, key, root: started.append(root) or _job(),
        )
        if env_value is None:
            monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
        else:
            monkeypatch.setenv("PIPELINE_STATE_DIR", env_value)

        advance_review.review_or_collect(_PLAN, _KEY, story, path, {})

        want = tmp_path / "state" if expected == "RELATIVE" else expected
        if expected == "MANIFEST_DIR":
            want = tmp_path
        elif expected != "RELATIVE":
            from pathlib import Path

            want = Path(expected)
        assert started == [want]

    def test_start_failure_is_logged_and_retried_next_advance(
        self, monkeypatch, tmp_path, caplog
    ):
        path = _write_manifest(tmp_path)
        story = _stories(path)[_KEY]
        before = path.read_text()

        def _boom(plan, key, root):
            raise OSError("/private/secret/path: read-only file system")

        monkeypatch.setattr(review_job, "start_review_job", _boom)
        summary = {}

        with caplog.at_level(logging.WARNING, logger="pipeline.advance_review"):
            advance_review.review_or_collect(_PLAN, _KEY, story, path, summary)

        assert path.read_text() == before
        assert "review_job" not in _stories(path)[_KEY]
        assert summary.get("review_started", []) == []
        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert records, "a start failure must be logged"
        assert records[0].plan == _PLAN
        assert records[0].story == _KEY
        # Secure-by-Design: the exception's text (which carries a path) and the
        # path itself never reach the log line.
        assert "/private/secret/path" not in caplog.text
        assert "OSError" in records[0].getMessage()


class TestCollect:
    def test_pending_job_records_the_key_and_persists_nothing(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(tmp_path, {_KEY: _story(review_job=_job())})
        story = _stories(path)[_KEY]
        before = path.read_text()
        monkeypatch.setattr(review_job, "collect_review_job", lambda job, timeout: None)
        summary = {}

        advance_review.review_or_collect(_PLAN, _KEY, story, path, summary)

        assert summary["review_pending"] == [_KEY]
        assert "review_started" not in summary
        assert path.read_text() == before

    def test_done_result_removes_the_field_and_records_the_status(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(tmp_path, {_KEY: _story(review_job=_job())})
        story = _stories(path)[_KEY]
        # The detached child wrote the manifest itself (its review advanced
        # the story); the collector's copy of ``story`` is therefore stale.
        manifest = json.loads(path.read_text())
        manifest["stories"][_KEY]["status"] = "pr_open"
        path.write_text(json.dumps(manifest))
        monkeypatch.setattr(
            review_job,
            "collect_review_job",
            lambda job, timeout: {"state": "done", "status": "pr_open"},
        )
        notifications = []
        monkeypatch.setattr(
            advance_module,
            "_notify_user",
            lambda _plan, message, **kwargs: notifications.append((message, kwargs)),
        )
        summary = {}

        advance_review.review_or_collect(_PLAN, _KEY, story, path, summary)

        assert summary["advanced"] == [{_KEY: "pr_open"}]
        assert summary.get("review_deferred", []) == []
        stored = _stories(path)[_KEY]
        assert "review_job" not in stored
        # Only the job field is dropped: the child's own status write stands.
        assert stored["status"] == "pr_open"
        assert notifications == []

    def test_done_result_without_a_status_is_recorded_as_skipped(
        self, monkeypatch, tmp_path
    ):
        """A `done` result that ran no review is a skip, never an advance this
        tick did not perform - even when review_story stamped a status on it."""
        path = _write_manifest(tmp_path, {_KEY: _story(review_job=_job())})
        story = _stories(path)[_KEY]
        monkeypatch.setattr(
            review_job,
            "collect_review_job",
            lambda job, timeout: {"state": "done", "skipped": "locked"},
        )
        monkeypatch.setattr(advance_module, "_notify_user", lambda *a, **k: None)
        summary = {}

        advance_review.review_or_collect(_PLAN, _KEY, story, path, summary)

        assert summary["skipped"] == [_KEY]
        assert summary.get("advanced", []) == []
        stored = _stories(path)[_KEY]
        assert "review_job" not in stored
        assert stored["status"] == "tests_passed"

    def test_skipped_done_result_with_a_status_is_still_a_skip(
        self, monkeypatch, tmp_path
    ):
        """review_story's unchanged_since_last_review skip reports the story's
        own ``tests_passed`` status alongside ``skipped``; the inline path
        records that under ``skipped``, so the detached path must too."""
        path = _write_manifest(tmp_path, {_KEY: _story(review_job=_job())})
        story = _stories(path)[_KEY]
        monkeypatch.setattr(
            review_job,
            "collect_review_job",
            lambda job, timeout: {
                "state": "done",
                "status": "tests_passed",
                "skipped": "unchanged_since_last_review",
            },
        )
        monkeypatch.setattr(advance_module, "_notify_user", lambda *a, **k: None)
        summary = {}

        advance_review.review_or_collect(_PLAN, _KEY, story, path, summary)

        assert summary["skipped"] == [_KEY]
        assert summary.get("advanced", []) == []
        assert "review_job" not in _stories(path)[_KEY]

    def test_rate_limited_done_result_is_also_recorded_as_deferred(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(tmp_path, {_KEY: _story(review_job=_job())})
        story = _stories(path)[_KEY]
        monkeypatch.setattr(
            review_job,
            "collect_review_job",
            lambda job, timeout: {
                "state": "done",
                "status": "tests_passed",
                "deferred": "rate_limited",
            },
        )
        monkeypatch.setattr(advance_module, "_notify_user", lambda *a, **k: None)
        summary = {}

        advance_review.review_or_collect(_PLAN, _KEY, story, path, summary)

        assert summary["advanced"] == [{_KEY: "tests_passed"}]
        assert summary["review_deferred"] == [_KEY]

    def test_error_result_notifies_once_and_leaves_the_story_reviewable(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(tmp_path, {_KEY: _story(review_job=_job())})
        story = _stories(path)[_KEY]
        monkeypatch.setattr(
            review_job,
            "collect_review_job",
            lambda job, timeout: {
                "state": "error",
                "error": "review job raised RuntimeError",
                "log_tail": "Traceback ... /home/operator/secret-worktree/x.py",
            },
        )
        notifications = []
        monkeypatch.setattr(
            advance_module,
            "_notify_user",
            lambda _plan, message, **kwargs: notifications.append((message, kwargs)),
        )
        summary = {}

        advance_review.review_or_collect(_PLAN, _KEY, story, path, summary)

        assert len(notifications) == 1
        message, kwargs = notifications[0]
        assert kwargs.get("event") == "review_job_error"
        assert _KEY in message
        assert "/home/operator" not in message
        assert "Traceback" not in message
        assert "RuntimeError" not in message
        stored = _stories(path)[_KEY]
        assert "review_job" not in stored
        assert stored["status"] == "tests_passed"
        assert summary.get("advanced", []) == []

    def test_operator_moved_story_with_a_live_job_is_cancelled(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(
            tmp_path, {_KEY: _story(status="pr_open", review_job=_job())}
        )
        story = _stories(path)[_KEY]
        cancelled = []
        monkeypatch.setattr(
            review_job, "cancel_review_job", lambda job: cancelled.append(job)
        )

        def _never(job, timeout):
            raise AssertionError("a moved story must not be collected")

        monkeypatch.setattr(review_job, "collect_review_job", _never)
        summary = {}

        advance_review.review_or_collect(_PLAN, _KEY, story, path, summary)

        assert cancelled == [_job()]
        assert "review_job" not in _stories(path)[_KEY]
        assert summary == {}


class TestTimeout:
    @pytest.mark.parametrize("raw", ["abc", "0", "-1", "inf", "nan", ""])
    def test_bad_timeout_warns_and_falls_back_to_900(
        self, monkeypatch, tmp_path, caplog, raw
    ):
        path = _write_manifest(tmp_path, {_KEY: _story(review_job=_job())})
        story = _stories(path)[_KEY]
        seen = []
        monkeypatch.setattr(
            review_job,
            "collect_review_job",
            lambda job, timeout: seen.append(timeout) or None,
        )
        monkeypatch.setenv("PIPELINE_REVIEW_JOB_TIMEOUT_SECONDS", raw)

        with caplog.at_level(logging.WARNING, logger="pipeline.advance_review"):
            advance_review.review_or_collect(_PLAN, _KEY, story, path, {})

        assert seen == [900]
        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert records, f"a bad timeout ({raw!r}) must be logged"
        assert records[0].plan == _PLAN
        assert records[0].story == _KEY

    def test_valid_timeout_is_used_without_a_warning(
        self, monkeypatch, tmp_path, caplog
    ):
        path = _write_manifest(tmp_path, {_KEY: _story(review_job=_job())})
        story = _stories(path)[_KEY]
        seen = []
        monkeypatch.setattr(
            review_job,
            "collect_review_job",
            lambda job, timeout: seen.append(timeout) or None,
        )
        monkeypatch.setenv("PIPELINE_REVIEW_JOB_TIMEOUT_SECONDS", "120")

        with caplog.at_level(logging.WARNING, logger="pipeline.advance_review"):
            advance_review.review_or_collect(_PLAN, _KEY, story, path, {})

        assert seen == [120]
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# ---------------------------------------------------------------------------
# Watcher wiring


def test_finished_review_job_wakes_its_plan():
    assert ("review_job", "result_path") in watchers.JOB_RESULT_FIELDS
