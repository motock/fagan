"""LOCKSTARVE-B3: the tick's dispatch loop releases the plan lock around
``dispatch_story``, guarded by the LOCKSTARVE-B2 dispatch lease.

Background: ``advance_pipeline`` holds the per-plan flock for the whole
tick (``pipeline/concurrency.py::_plan_lock``), including the synchronous
``dispatch_story`` call, which starves any other actor (another tick,
another MCP server process, an operator's ``approve_merge``) that needs the
same lock for the whole duration of a dispatch. This story wires
``pipeline.concurrency._released_plan_lock`` (already implemented, B1) and
``pipeline.dispatch_lease.claim_dispatch_lease`` (already implemented, B2)
into ``pipeline/advance.py``'s ``for key in ready:`` dispatch loop:

1. Claim the dispatch lease under the lock the tick already holds.
2. Persist the claim to the manifest BEFORE releasing the lock.
3. Release the plan lock only around the ``dispatch_story`` call.
4. Re-read the manifest fresh from disk after the lock is re-acquired -
   any in-memory reference captured before the release is stale and must
   never be written back.
5. Clear the lease fields on the freshly re-read story, whether dispatch
   succeeded, returned ``ok: False``, or raised.

This suite runs the REAL tick body (``_advance_pipeline_locked_impl``)
under a REAL ``pipeline.concurrency._plan_lock``, exactly as
``PipelineService.advance_pipeline`` does via ``_store.transaction`` in
production - only the model-call-heavy collaborators (dispatch_story
itself, review_story, the resource backend, autonomy) are stubbed.
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import pipeline.advance as advance_module
import pipeline.config as config_module
from pipeline import concurrency, dispatch_lease

_PLAN = "lock-release-plan"


# ---------------------------------------------------------------------------
# Fixtures / helpers


@pytest.fixture(autouse=True)
def _isolate_plan_lock(tmp_path, monkeypatch):
    """Point the real flock at tmp_path and give every test a clean held-set.

    Mirrors tests/unit/test_released_plan_lock.py: PLAN_DIR is patched
    directly on the concurrency module (bypassing the _ServerRef
    indirection), so ``_plan_lock``/``_released_plan_lock`` never touch the
    real ~/.claude/plans directory.
    """
    monkeypatch.setattr(concurrency, "PLAN_DIR", tmp_path)
    concurrency._held_plan_locks().clear()
    yield
    concurrency._held_plan_locks().clear()


class _FakeBackend:
    """Per-story resource gate always reports healthy."""

    def get_backend(self, *_args, **_kwargs):
        return self

    def resource_status(self, **_kwargs):
        return {"ok": True}


class _FakeAutonomy:
    """Stands in for the PIPELINE_AUTONOMY _ServerRef proxy."""

    def __init__(self, value="gated"):
        self._value_ = value

    def _value(self):
        return self._value_


class _FakeStore:
    """Backs the tick's ``_store`` seam with a real JSON file on disk."""

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


def _seed_common(monkeypatch, path):
    """Wire every seam the tick body touches, wide open, matching the
    conventions in tests/unit/test_advance_dispatch_per_tick_cap.py."""
    monkeypatch.setattr(advance_module, "_store", _FakeStore(path))
    monkeypatch.setattr(advance_module, "backend", _FakeBackend())
    monkeypatch.setattr(
        advance_module, "_role_resource_ok", lambda *a, **k: (True, "")
    )
    monkeypatch.setattr(
        advance_module, "_count_on_device_in_progress_agents", lambda: 0
    )
    monkeypatch.setattr(
        advance_module, "PIPELINE_AUTONOMY", _FakeAutonomy("gated"), raising=True
    )
    monkeypatch.setattr(advance_module, "_adjudicate_merges", lambda *a, **k: None)
    monkeypatch.setattr(advance_module, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(advance_module, "interrupt_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module, "check_story_status", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module, "review_story", lambda *a, **k: {"ok": True})
    # Cap/device slot math must never be the binding constraint here.
    monkeypatch.setattr(config_module, "PIPELINE_MAX_DISPATCH_PER_TICK", 1, raising=False)
    monkeypatch.setattr(advance_module, "PIPELINE_MAX_DISPATCH_PER_TICK", 1, raising=False)
    monkeypatch.setattr(config_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    monkeypatch.setattr(advance_module, "MAX_CONCURRENT_AGENTS", 8, raising=False)
    monkeypatch.setattr(config_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(advance_module, "DISPATCH_MAX_ATTEMPTS", 3, raising=False)
    for name in (
        "SESSION_PAUSE_THRESHOLD",
        "SESSION_RESUME_THRESHOLD",
        "WEEK_PAUSE_THRESHOLD",
        "WEEK_RESUME_THRESHOLD",
    ):
        monkeypatch.setattr(config_module, name, 101, raising=False)
        if hasattr(advance_module, name):
            monkeypatch.setattr(advance_module, name, 101, raising=False)


def _story(status="todo", **extra):
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
    _store.transaction(plan_name), then run the tick body directly (skipping
    the advisory triage/wedge sweeps, irrelevant here)."""
    with concurrency._plan_lock(plan_name) as acquired:
        assert acquired, "test setup: failed to acquire the plan lock"
        return advance_module._advance_pipeline_locked_impl(plan_name)


def _read_stories(path):
    return json.loads(path.read_text())["stories"]


def _future_iso(seconds=900):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _past_iso(seconds=10):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# ---------------------------------------------------------------------------
# The fake dispatch_story used by the positive-path tests: probes, from a
# second thread, whether the plan lock is acquirable WHILE it runs.


class _ProbingDispatch:
    def __init__(self, plan_name, manifest_path):
        self.plan_name = plan_name
        self.manifest_path = manifest_path
        self.acquired_during_dispatch = None
        self.lease_seen_live = None
        self.calls = []

    def __call__(self, plan_name, key):
        self.calls.append((plan_name, key))
        result = {}

        def probe():
            with concurrency._plan_lock(plan_name) as ok:
                result["acquired"] = bool(ok)

        t = threading.Thread(target=probe, daemon=True)
        t.start()
        t.join(5.0)
        self.acquired_during_dispatch = result.get("acquired", False)

        m = json.loads(self.manifest_path.read_text())
        self.lease_seen_live = dispatch_lease.lease_is_live(m["stories"][key])

        m["stories"][key]["status"] = "in_progress"
        advance_module._atomic_write_json(self.manifest_path, m)
        return {"ok": True}


def _probe_acquire_and_mutate(plan_name, manifest_path, mutate_fn, timeout=3.0):
    """From a fresh thread (representing a second process), spin trying to
    acquire the REAL plan lock; on success, mutate the manifest before
    releasing. Returns True iff the mutation was written.

    A genuinely concurrent writer needs the actual lock, not a direct write
    bypassing it — this is what ties the mutation to the lock truly being
    released, rather than merely to the fake dispatch_story running.
    """
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


class _LockStealingDispatch:
    """Fake dispatch_story that, while the plan lock is released around it,
    has a SECOND THREAD genuinely steal the real flock and hold it past the
    reacquire timeout - simulating another tick/process winning the plan
    lock during the release window. Used to force
    ``_released_plan_lock``'s exit to raise ``PlanLockReacquireTimeout``."""

    def __init__(self, plan_name):
        self.plan_name = plan_name
        self._stole = threading.Event()
        self._release = threading.Event()
        self._thread = None

    def _hold_lock(self):
        with concurrency._plan_lock(self.plan_name) as acquired:
            if acquired:
                self._stole.set()
            self._release.wait(5.0)

    def __call__(self, plan_name, key):
        self._thread = threading.Thread(target=self._hold_lock, daemon=True)
        self._thread.start()
        got = self._stole.wait(3.0)
        assert got, "test setup: stealer thread never acquired the plan lock"
        return {"ok": True}

    def release(self):
        self._release.set()
        if self._thread is not None:
            self._thread.join(5.0)


class _ConcurrentMutationDispatch:
    """Fake dispatch_story for the stale-reference regression guard: while
    this call is "in flight", a second thread tries to genuinely acquire
    the plan lock and mutate a DIFFERENT story on disk."""

    def __init__(self, plan_name, manifest_path):
        self.plan_name = plan_name
        self.manifest_path = manifest_path
        self.mutated = None

    def __call__(self, plan_name, key):
        def _mutate(m):
            m["stories"]["s2"]["marker"] = "written-during-release-window"

        self.mutated = _probe_acquire_and_mutate(plan_name, self.manifest_path, _mutate)

        m = json.loads(self.manifest_path.read_text())
        m["stories"][key]["status"] = "in_progress"
        advance_module._atomic_write_json(self.manifest_path, m)
        return {"ok": True}


# ---------------------------------------------------------------------------
# Positive tests


class TestLockReleasedAroundDispatch:
    def test_plan_lock_is_not_held_during_dispatch_story(self, monkeypatch, tmp_path):
        path = _write_manifest(tmp_path, {"s1": _story()})
        _seed_common(monkeypatch, path)
        fake = _ProbingDispatch(_PLAN, path)
        monkeypatch.setattr(advance_module, "dispatch_story", fake)

        _run_tick_holding_lock()

        assert fake.calls == [(_PLAN, "s1")]
        assert fake.acquired_during_dispatch is True

    def test_lock_released_normally_after_tick_and_story_in_progress(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(tmp_path, {"s1": _story()})
        _seed_common(monkeypatch, path)
        fake = _ProbingDispatch(_PLAN, path)
        monkeypatch.setattr(advance_module, "dispatch_story", fake)

        _run_tick_holding_lock()

        assert concurrency._held_plan_locks() == set()
        with concurrency._plan_lock(_PLAN) as acquired:
            assert acquired is True
        assert _read_stories(path)["s1"]["status"] == "in_progress"

    def test_lease_claimed_before_dispatch_and_cleared_after(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(tmp_path, {"s1": _story()})
        _seed_common(monkeypatch, path)
        fake = _ProbingDispatch(_PLAN, path)
        monkeypatch.setattr(advance_module, "dispatch_story", fake)

        _run_tick_holding_lock()

        assert fake.lease_seen_live is True
        final = _read_stories(path)["s1"]
        assert "dispatch_lease_expires_at" not in final
        assert "dispatch_lease_owner_pid" not in final


# ---------------------------------------------------------------------------
# Negative / boundary tests


class TestDispatchLeaseGuards:
    def test_live_lease_from_another_owner_blocks_dispatch_this_tick(
        self, monkeypatch, tmp_path
    ):
        story = _story(
            dispatch_lease_expires_at=_future_iso(),
            dispatch_lease_owner_pid=999999,
        )
        path = _write_manifest(tmp_path, {"s1": story})
        _seed_common(monkeypatch, path)

        def _must_not_be_called(plan_name, key):
            raise AssertionError("dispatch_story must not be called while a live lease is held")

        monkeypatch.setattr(advance_module, "dispatch_story", _must_not_be_called)

        result = _run_tick_holding_lock()

        assert result["ok"] is True
        final = _read_stories(path)["s1"]
        assert final["status"] == "todo"
        assert final["dispatch_attempts"] == 0
        assert final["dispatch_lease_owner_pid"] == 999999

    def test_expired_lease_does_not_block_dispatch(self, monkeypatch, tmp_path):
        story = _story(
            dispatch_lease_expires_at=_past_iso(),
            dispatch_lease_owner_pid=999999,
        )
        path = _write_manifest(tmp_path, {"s1": story})
        _seed_common(monkeypatch, path)
        fake = _ProbingDispatch(_PLAN, path)
        monkeypatch.setattr(advance_module, "dispatch_story", fake)

        _run_tick_holding_lock()

        assert fake.calls == [(_PLAN, "s1")]
        final = _read_stories(path)["s1"]
        assert final["status"] == "in_progress"
        assert "dispatch_lease_expires_at" not in final

    def test_dispatch_story_raising_still_clears_lease_and_counts_attempt(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(tmp_path, {"s1": _story()})
        _seed_common(monkeypatch, path)

        def _raising(plan_name, key):
            raise RuntimeError("git fetch failed")

        monkeypatch.setattr(advance_module, "dispatch_story", _raising)

        _run_tick_holding_lock()

        final = _read_stories(path)["s1"]
        assert final["status"] == "todo"  # DISPATCH_MAX_ATTEMPTS pinned to 3
        assert final["dispatch_attempts"] == 1
        assert "dispatch_lease_expires_at" not in final
        assert "dispatch_lease_owner_pid" not in final

    def test_concurrent_write_during_release_window_survives_the_tick(
        self, monkeypatch, tmp_path
    ):
        """Regression guard for the stale-reference hazard: while the lock is
        released around dispatch_story, a SECOND THREAD genuinely acquires
        the real plan lock (only possible if the tick actually released it)
        and mutates a DIFFERENT story on disk. That mutation must survive
        the tick's own post-dispatch writes - the lease-clear write must
        re-read fresh, never write back the stale top-of-tick manifest."""
        path = _write_manifest(
            tmp_path,
            {"s1": _story(), "s2": _story(status="interrupted", key="s2")},
        )
        _seed_common(monkeypatch, path)
        fake = _ConcurrentMutationDispatch(_PLAN, path)
        monkeypatch.setattr(advance_module, "dispatch_story", fake)

        _run_tick_holding_lock()

        assert fake.mutated is True, (
            "test setup: the concurrent writer thread never acquired the "
            "plan lock - the lock was not released during dispatch_story"
        )
        stories = _read_stories(path)
        assert stories["s2"].get("marker") == "written-during-release-window"
        assert stories["s1"]["status"] == "in_progress"

    def test_ok_false_result_clears_lease_and_preserves_attempt_counting(
        self, monkeypatch, tmp_path
    ):
        path = _write_manifest(tmp_path, {"s1": _story()})
        _seed_common(monkeypatch, path)

        def _ok_false(plan_name, key):
            return {"ok": False, "error": "git setup failed"}

        monkeypatch.setattr(advance_module, "dispatch_story", _ok_false)

        _run_tick_holding_lock()

        final = _read_stories(path)["s1"]
        assert final["status"] == "todo"
        assert final["dispatch_attempts"] == 1
        assert "dispatch_lease_expires_at" not in final
        assert "dispatch_lease_owner_pid" not in final


# ---------------------------------------------------------------------------
# Regression guard: a failed re-acquire must abort the tick, not be treated
# as an ordinary dispatch failure.


class TestPlanLockReacquireTimeoutAbortsTheTick:
    def test_reacquire_timeout_propagates_and_is_not_counted_as_dispatch_attempt(
        self, monkeypatch, tmp_path
    ):
        """If another thread/process wins the real flock during the release
        window and still holds it once dispatch_story returns,
        ``_released_plan_lock``'s exit raises ``PlanLockReacquireTimeout``.
        That must propagate out of the tick uncaught by the broad
        ``except Exception`` dispatch-failure handler below it - a caught
        timeout would keep mutating the manifest with no flock held (the
        exact hazard the lease's check-then-set safety depends on the lock
        for), and would misattribute a possibly-already-launched dispatch as
        a counted failure."""
        path = _write_manifest(tmp_path, {"s1": _story()})
        _seed_common(monkeypatch, path)
        # Bound the blocking re-acquire tightly so the test doesn't wait 300s.
        monkeypatch.setattr(concurrency, "_plan_reacquire_timeout", lambda: 0.2)
        fake = _LockStealingDispatch(_PLAN)
        monkeypatch.setattr(advance_module, "dispatch_story", fake)

        try:
            with pytest.raises(concurrency.PlanLockReacquireTimeout):
                _run_tick_holding_lock()
        finally:
            fake.release()

        final = _read_stories(path)["s1"]
        # The broad except Exception handler (which would bump
        # dispatch_attempts, clear the lease, and possibly flip status to
        # "failed") must never have run.
        assert final["status"] == "todo"
        assert final["dispatch_attempts"] == 0
        # The lease claimed under step 2 (persisted before the lock was
        # released) is still on disk - proof the abort happened before
        # reaching the generic failure handler's lease-clearing code, not
        # after it silently succeeded.
        assert final.get("dispatch_lease_owner_pid") is not None
