"""LOCKSTARVE-B4: the tick's review loop releases the plan lock around
``review_story``.

Background: the dispatch loop's lock-release fix (LOCKSTARVE-B3) established
the pattern this story applies to the ``if review_ok:`` block instead - the
loop that calls ``review_story(plan_name, key)`` for every ``tests_passed``
story. ``review_story`` is a synchronous, model-call-heavy operation, so
holding the per-plan flock for its whole duration starves any other actor
(another tick, another MCP server process, an operator's ``approve_merge``)
that needs the same lock.

No dispatch lease is used here: ``review_story`` (pipeline/review_orchestrator.py)
re-reads the manifest itself and is a documented no-op skip for any story not
in ``tests_passed`` - so a second tick entering it concurrently cannot
double-review. But the per-iteration STATUS CHECK in the loop must still come
from a fresh on-disk read, not a snapshot taken before the loop - otherwise a
later iteration decides "still tests_passed" from stale data even though an
earlier iteration's own released-lock window let another actor already
review and advance that very story, and calls review_story on it anyway
(spurious "review skipped" notification + a misleading summary["advanced"]
entry). This is the check-side analogue of the write-side stale-reference
hazard LOCKSTARVE-B3's round-2 review caught on the dispatch path.

This suite runs the REAL tick body (``_advance_pipeline_locked_impl``) under
a REAL ``pipeline.concurrency._plan_lock``, exactly as
``PipelineService.advance_pipeline`` does via ``_store.transaction`` in
production - only the model-call-heavy collaborators (review_story itself,
the resource backend, autonomy) are stubbed.
"""

import json
import threading
import time

import pytest

import pipeline.advance as advance_module
import pipeline.config as config_module
from pipeline import concurrency

_PLAN = "review-lock-release-plan"


# ---------------------------------------------------------------------------
# Fixtures / helpers


@pytest.fixture(autouse=True)
def _isolate_plan_lock(tmp_path, monkeypatch):
    """Mirrors tests/unit/test_advance_dispatch_releases_plan_lock.py: point
    the real flock at tmp_path and give every test a clean held-set."""
    monkeypatch.setattr(concurrency, "PLAN_DIR", tmp_path)
    concurrency._held_plan_locks().clear()
    yield
    concurrency._held_plan_locks().clear()


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


def _write_manifest(tmp_path, stories):
    manifest = {"name": _PLAN, "paused": False, "stories": stories}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _seed_common(monkeypatch, path, review_ok=True, review_reason=""):
    """Wire every seam the tick body touches, matching the conventions in
    tests/unit/test_advance_dispatch_releases_plan_lock.py. Returns the list
    that captures every _notify_user call as (message, kwargs)."""
    monkeypatch.setattr(advance_module, "_store", _FakeStore(path))
    monkeypatch.setattr(advance_module, "backend", _FakeBackend())

    def _role_resource_ok(role, **_kwargs):
        if role == "dispatch":
            return True, ""
        return review_ok, review_reason

    monkeypatch.setattr(advance_module, "_role_resource_ok", _role_resource_ok)
    monkeypatch.setattr(
        advance_module, "_count_on_device_in_progress_agents", lambda: 0
    )
    monkeypatch.setattr(
        advance_module, "PIPELINE_AUTONOMY", _FakeAutonomy("gated"), raising=True
    )
    monkeypatch.setattr(advance_module, "_adjudicate_merges", lambda *a, **k: None)
    notifications = []

    def _notify_user(_plan_name, message, **kwargs):
        notifications.append((message, kwargs))

    monkeypatch.setattr(advance_module, "_notify_user", _notify_user)
    monkeypatch.setattr(advance_module, "interrupt_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module, "check_story_status", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module, "dispatch_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(config_module, "PIPELINE_MAX_DISPATCH_PER_TICK", 1, raising=False)
    monkeypatch.setattr(advance_module, "PIPELINE_MAX_DISPATCH_PER_TICK", 1, raising=False)
    monkeypatch.setattr(config_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    monkeypatch.setattr(advance_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    monkeypatch.setattr(config_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(advance_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)
    return notifications


def _story(status="tests_passed", **extra):
    story = {
        "key": "s1",
        "title": "story s1",
        "status": status,
        "risk": "low",
        "dispatch_attempts": 0,
    }
    story.update(extra)
    return story


def _run_tick_holding_lock(plan_name=_PLAN):
    """Simulate the production entry point: hold the real plan lock for the
    whole tick, exactly as PipelineService.advance_pipeline does via
    _store.transaction(plan_name)."""
    with concurrency._plan_lock(plan_name) as acquired:
        assert acquired, "test setup: failed to acquire the plan lock"
        return advance_module._advance_pipeline_locked_impl(plan_name)


def _read_stories(path):
    return json.loads(path.read_text())["stories"]


def _probe_acquire_and_mutate(plan_name, manifest_path, mutate_fn, timeout=3.0):
    """From a fresh thread (representing a second process), spin trying to
    acquire the REAL plan lock; on success, mutate the manifest before
    releasing. Returns True iff the mutation was written."""
    result = {"mutated": False}

    def run():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with concurrency._plan_lock(plan_name) as ok:
                if ok:
                    m = json.loads(manifest_path.read_text())
                    mutate_fn(m)
                    advance_module._atomic_write_json(manifest_path, m)
                    result["mutated"] = True
                    return
            time.sleep(0.02)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout + 1.0)
    return result["mutated"]


# ---------------------------------------------------------------------------
# Fakes for review_story


class _ProbingReview:
    """Probes, from a second thread, whether the plan lock is acquirable
    WHILE review_story runs."""

    def __init__(self, plan_name):
        self.plan_name = plan_name
        self.calls = []
        self.acquired = {}

    def __call__(self, plan_name, key):
        self.calls.append(key)
        result = {}

        def probe():
            with concurrency._plan_lock(plan_name) as ok:
                result["acquired"] = bool(ok)

        t = threading.Thread(target=probe, daemon=True)
        t.start()
        t.join(5.0)
        self.acquired[key] = result.get("acquired", False)
        return {"ok": True, "status": "pr_open"}


class _ReviewToPrOpen:
    """Mimics review_story's real effect of advancing the story's status."""

    def __init__(self, manifest_path):
        self.manifest_path = manifest_path
        self.calls = []

    def __call__(self, plan_name, key):
        self.calls.append(key)
        m = json.loads(self.manifest_path.read_text())
        m["stories"][key]["status"] = "pr_open"
        advance_module._atomic_write_json(self.manifest_path, m)
        return {"ok": True, "status": "pr_open"}


class _ConcurrentMutationReview:
    """While "reviewing" a story, a second thread genuinely acquires the
    plan lock and mutates a DIFFERENT story on disk."""

    def __init__(self, plan_name, manifest_path):
        self.plan_name = plan_name
        self.manifest_path = manifest_path
        self.mutated = None

    def __call__(self, plan_name, key):
        def _mutate(m):
            m["stories"]["s2"]["marker"] = "written-during-release-window"

        self.mutated = _probe_acquire_and_mutate(plan_name, self.manifest_path, _mutate)
        m = json.loads(self.manifest_path.read_text())
        m["stories"][key]["status"] = "pr_open"
        advance_module._atomic_write_json(self.manifest_path, m)
        return {"ok": True, "status": "pr_open"}


class _StaleStatusReview:
    """Faithfully mirrors review_story's real not_reviewable_state guard
    (pipeline/review_orchestrator.py::review_story), so this fake exercises
    the same "review skipped" notification path the real function would.

    While "reviewing" s1, writes s2's status to pr_open on disk directly -
    simulating a second process reviewing and advancing s2 during s1's
    released-lock window.
    """

    def __init__(self, manifest_path):
        self.manifest_path = manifest_path
        self.calls = []

    def __call__(self, plan_name, key):
        self.calls.append(key)
        m = json.loads(self.manifest_path.read_text())
        story = m["stories"][key]
        if story["status"] != "tests_passed":
            advance_module._notify_user(
                plan_name,
                f"{key} review skipped: status {story['status']!r} - only "
                f"stories with status 'tests_passed' are reviewable.",
            )
            return {
                "ok": True,
                "status": story["status"],
                "skipped": "not_reviewable_state",
            }
        if key == "s1":
            m["stories"]["s2"]["status"] = "pr_open"
        story["status"] = "pr_open"
        advance_module._atomic_write_json(self.manifest_path, m)
        return {"ok": True, "status": "pr_open"}


# ---------------------------------------------------------------------------
# Positive tests


class TestLockReleasedAroundReview:
    def test_plan_lock_is_not_held_during_review_story(self, monkeypatch, tmp_path):
        path = _write_manifest(tmp_path, {"s1": _story()})
        _seed_common(monkeypatch, path)
        fake = _ProbingReview(_PLAN)
        monkeypatch.setattr(advance_module, "review_story", fake)

        _run_tick_holding_lock()

        assert fake.calls == ["s1"]
        assert fake.acquired["s1"] is True

    def test_tests_passed_story_reaches_pr_open_through_the_tick(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(tmp_path, {"s1": _story()})
        _seed_common(monkeypatch, path)
        fake = _ReviewToPrOpen(path)
        monkeypatch.setattr(advance_module, "review_story", fake)

        result = _run_tick_holding_lock()

        assert _read_stories(path)["s1"]["status"] == "pr_open"
        assert result["advanced"] == [{"s1": "pr_open"}]

    def test_two_tests_passed_stories_each_reviewed_with_lock_released(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(
            tmp_path,
            {"s1": _story(key="s1"), "s2": _story(key="s2")},
        )
        _seed_common(monkeypatch, path)
        fake = _ProbingReview(_PLAN)
        monkeypatch.setattr(advance_module, "review_story", fake)

        _run_tick_holding_lock()

        assert fake.calls == ["s1", "s2"]
        assert fake.acquired["s1"] is True
        assert fake.acquired["s2"] is True


# ---------------------------------------------------------------------------
# Negative / boundary tests


class TestReviewLockReleaseGuards:
    def test_review_story_raising_still_reacquires_lock_and_tick_finishes(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(tmp_path, {"s1": _story()})
        _seed_common(monkeypatch, path)

        def _raising(plan_name, key):
            raise RuntimeError("reviewer crashed")

        monkeypatch.setattr(advance_module, "review_story", _raising)

        with pytest.raises(RuntimeError, match="reviewer crashed"):
            _run_tick_holding_lock()

        assert concurrency._held_plan_locks() == set()
        with concurrency._plan_lock(_PLAN) as acquired:
            assert acquired is True

    def test_concurrent_write_during_release_window_survives_the_tick(
        self, monkeypatch, tmp_path
    ):
        """Regression guard for the stale-reference hazard: while the lock is
        released around review_story, a SECOND THREAD genuinely acquires the
        real plan lock (only possible if the tick actually released it) and
        mutates a DIFFERENT story on disk. That mutation must survive the
        tick."""
        path = _write_manifest(
            tmp_path,
            {"s1": _story(), "s2": _story(key="s2", status="pr_open")},
        )
        _seed_common(monkeypatch, path)
        fake = _ConcurrentMutationReview(_PLAN, path)
        monkeypatch.setattr(advance_module, "review_story", fake)

        _run_tick_holding_lock()

        assert fake.mutated is True, (
            "test setup: the concurrent writer thread never acquired the "
            "plan lock - the lock was not released during review_story"
        )
        stories = _read_stories(path)
        assert stories["s2"].get("marker") == "written-during-release-window"
        assert stories["s1"]["status"] == "pr_open"

    def test_review_ok_false_defers_without_calling_review_story(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(tmp_path, {"s1": _story()})
        notifications = _seed_common(
            monkeypatch, path, review_ok=False, review_reason="claude usage exhausted"
        )

        def _must_not_be_called(plan_name, key):
            raise AssertionError(
                "review_story must not be called while review_ok is False"
            )

        monkeypatch.setattr(advance_module, "review_story", _must_not_be_called)

        result = _run_tick_holding_lock()

        assert result["review_paused"] is True
        assert any("Review backend gated" in msg for msg, _ in notifications)
        assert _read_stories(path)["s1"]["status"] == "tests_passed"

    def test_zero_tests_passed_stories_review_story_never_called_lock_never_released(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(tmp_path, {"s1": _story(status="pr_open")})
        _seed_common(monkeypatch, path)

        def _must_not_be_called(plan_name, key):
            raise AssertionError("review_story must not be called")

        monkeypatch.setattr(advance_module, "review_story", _must_not_be_called)
        release_calls = []
        _orig_released_plan_lock = advance_module._released_plan_lock

        def _counting_released_plan_lock(plan_name):
            release_calls.append(plan_name)
            return _orig_released_plan_lock(plan_name)

        monkeypatch.setattr(
            advance_module, "_released_plan_lock", _counting_released_plan_lock
        )

        _run_tick_holding_lock()

        assert release_calls == []
        assert concurrency._held_plan_locks() == set()


    def test_review_story_no_such_story_return_shape_does_not_keyerror(
        self, monkeypatch, tmp_path
    ):
        """review_story's real "no such story" return
        (pipeline/review_orchestrator.py:266) is {"ok": False, "error": ...}
        with no "status" key - reachable if a concurrent re-ingest drops the
        story between this loop's fresh read and review_story's own internal
        re-read, both of which happen inside the released-lock window. The
        loop must skip rather than KeyError on rv["status"] and abort the
        whole tick."""
        path = _write_manifest(tmp_path, {"s1": _story()})
        _seed_common(monkeypatch, path)

        def _no_such_story(plan_name, key):
            return {"ok": False, "error": f"No such story {key}"}

        monkeypatch.setattr(advance_module, "review_story", _no_such_story)

        result = _run_tick_holding_lock()

        assert result["advanced"] == []
        assert concurrency._held_plan_locks() == set()


class TestStaleStatusGuardOnReReview:
    def test_stale_status_snapshot_does_not_cause_a_double_review(
        self, monkeypatch, tmp_path
    ):
        """While s1 is under review (lock released), a second process
        reviews and advances s2 to pr_open. The loop must re-check s2's
        status from a fresh read before calling review_story on it - a
        version that snapshots status once before the loop would still see
        s2 as "tests_passed" and call review_story on it a second time,
        producing a spurious "review skipped" notification and a misleading
        summary["advanced"] entry."""
        path = _write_manifest(
            tmp_path,
            {"s1": _story(key="s1"), "s2": _story(key="s2")},
        )
        notifications = _seed_common(monkeypatch, path)
        fake = _StaleStatusReview(path)
        monkeypatch.setattr(advance_module, "review_story", fake)

        result = _run_tick_holding_lock()

        assert fake.calls == ["s1"], (
            "s2 must never be passed to review_story this tick - its status "
            "was already advanced to pr_open during s1's released-lock window"
        )
        assert not any("review skipped" in msg for msg, _ in notifications)
        assert result["advanced"] == [{"s1": "pr_open"}]
