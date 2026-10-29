"""SQ-05: the supervised pool of long-lived advance workers.

The pooled reconcile of ``pipeline.advance_pool`` waits for EVERY child before
its pass ends, so the slowest plan in the fleet sets the latency of every
other plan. This pool keeps ``size`` long-lived ``pipeline.advance_worker``
children alive instead: each worker thread takes one plan from the
``AdvanceQueue``, hands it to its own child over an inherited pipe pair, waits
for that one result line, and marks the plan finished so the plan can be
requested again as soon as its own advance ends.

A ``select``-bounded read replaces ``wait(timeout)`` so a hung advance is
terminated at the deadline; process death releases the plan's ``flock``. A
child that dies or recycles is dropped and the next plan respawns it.

Security invariant asserted here: every child spawns with ``cwd`` set to this
control plane's own source root -- never a story worktree, whose unreviewed
``pipeline`` package ``-m`` would import under the scheduler's identity.

These tests point ``_worker_argv`` at a tiny script in ``tmp_path``, so no
real advance and no real manifest is involved.
"""

# ruff: noqa: I001  -- pipeline.server must be imported before any other
# pipeline module (pre-existing import cycle; see test_triage_cap_silent_skip).
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import pipeline.server
import pipeline.actor  # noqa: F401  -- imported for the cycle guard idiom


def _pool_module():
    """Import the module under test lazily (keeps collection cheap)."""
    import pipeline.advance_workers

    return pipeline.advance_workers


_FAKE_WORKER = '''\
"""Tiny stand-in for ``pipeline.advance_worker`` (same wire protocol)."""

import json
import os
import sys
import time

in_fd, out_fd = int(sys.argv[1]), int(sys.argv[2])

with os.fdopen(in_fd, "r", encoding="utf-8") as reader:
    for raw_line in reader:
        plan = json.loads(raw_line)["plan"]
        if plan == "die":
            sys.exit(0)
        if plan == "boom":
            sys.exit(3)
        if plan == "slow":
            time.sleep(5)
        os.write(out_fd, (json.dumps({"ok": True, "plan": plan}) + "\\n").encode())
'''


@pytest.fixture()
def worker_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the pool's argv builder at the fake worker script."""
    script = tmp_path / "fake_advance_worker.py"
    script.write_text(_FAKE_WORKER, encoding="utf-8")
    module = _pool_module()
    monkeypatch.setattr(
        module,
        "_worker_argv",
        lambda in_fd, out_fd: [sys.executable, str(script), str(in_fd), str(out_fd)],
    )
    return script


@pytest.fixture()
def spawned(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[subprocess.Popen]]:
    """Record every ``Popen`` the pool starts, without disturbing the spawn."""
    recorded: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def _record(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        recorded.append(proc)
        return proc

    monkeypatch.setattr(_pool_module().subprocess, "Popen", _record)
    yield recorded


def _queue():
    from pipeline.advance_queue import AdvanceQueue

    return AdvanceQueue()


def _wait_for(predicate, timeout_s: float = 10.0) -> bool:
    """Return whether *predicate* became true within *timeout_s*."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _plan_timeout(value: float = 10.0):
    return lambda: value


def test_three_plans_are_each_advanced_once(worker_script: Path) -> None:
    module = _pool_module()
    queue = _queue()
    finished: list[str] = []
    real_finish = queue.finish

    def _finish(plan_name: str) -> None:
        finished.append(plan_name)
        real_finish(plan_name)

    queue.finish = _finish  # type: ignore[method-assign]
    for plan in ("alpha", "beta", "gamma"):
        assert queue.request(plan)

    pool = module.AdvanceWorkerPool(size=1, queue=queue, plan_timeout_s=_plan_timeout())
    pool.start()
    try:
        assert _wait_for(lambda: len(finished) == 3)
    finally:
        pool.stop(timeout_s=5.0)

    assert sorted(finished) == ["alpha", "beta", "gamma"]
    for plan in ("alpha", "beta", "gamma"):
        assert queue.last_advanced(plan) is not None
    assert pool.stats()["advances"] == 3


def test_slow_plan_does_not_delay_later_fast_plans(worker_script: Path) -> None:
    module = _pool_module()
    queue = _queue()
    finished: list[str] = []
    real_finish = queue.finish

    def _finish(plan_name: str) -> None:
        finished.append(plan_name)
        real_finish(plan_name)

    queue.finish = _finish  # type: ignore[method-assign]
    for plan in ("slow", "fast1", "fast2"):
        assert queue.request(plan)

    pool = module.AdvanceWorkerPool(size=2, queue=queue, plan_timeout_s=_plan_timeout(120.0))
    pool.start()
    try:
        assert _wait_for(lambda: len(finished) == 3, timeout_s=60.0)
    finally:
        pool.stop(timeout_s=5.0)

    assert finished[-1] == "slow"
    assert set(finished[:2]) == {"fast1", "fast2"}


def test_timeout_kills_child_counts_and_next_plan_respawns(
    worker_script: Path, spawned: list[subprocess.Popen], caplog: pytest.LogCaptureFixture
) -> None:
    module = _pool_module()
    queue = _queue()
    queue.request("slow")
    pool = module.AdvanceWorkerPool(size=1, queue=queue, plan_timeout_s=_plan_timeout(0.5))
    pool.start()
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        try:
            assert _wait_for(lambda: pool.stats()["timeouts"] == 1)
            first = list(spawned)
            assert first and first[0].poll() is not None
            queue.request("fast1")
            assert _wait_for(lambda: pool.stats()["advances"] == 1)
        finally:
            pool.stop(timeout_s=5.0)

    assert any(rec.message == "advance worker timed out" for rec in caplog.records)
    assert len(spawned) == 2


def test_dying_child_counts_a_respawn_and_next_plan_runs(
    worker_script: Path, caplog: pytest.LogCaptureFixture
) -> None:
    module = _pool_module()
    queue = _queue()
    queue.request("die")
    pool = module.AdvanceWorkerPool(size=1, queue=queue, plan_timeout_s=_plan_timeout())
    with caplog.at_level(logging.INFO, logger=module.__name__):
        pool.start()
        try:
            assert _wait_for(lambda: pool.stats()["respawns"] == 1)
            queue.request("fast1")
            assert _wait_for(lambda: pool.stats()["advances"] == 1)
        finally:
            pool.stop(timeout_s=5.0)

    assert pool.stats()["respawns"] == 1
    assert queue.last_advanced("fast1") is not None
    recycled = [
        rec for rec in caplog.records if rec.message == "advance worker recycled"
    ]
    assert recycled and recycled[0].levelno == logging.INFO


def test_nonzero_exit_child_is_reaped_and_warned(
    worker_script: Path, caplog: pytest.LogCaptureFixture
) -> None:
    module = _pool_module()
    queue = _queue()
    queue.request("boom")
    pool = module.AdvanceWorkerPool(size=1, queue=queue, plan_timeout_s=_plan_timeout())
    pool.start()
    with caplog.at_level(logging.INFO, logger=module.__name__):
        try:
            assert _wait_for(lambda: pool.stats()["respawns"] == 1)
            queue.request("fast1")
            assert _wait_for(lambda: pool.stats()["advances"] == 1)
        finally:
            pool.stop(timeout_s=5.0)

    crashed = [
        rec
        for rec in caplog.records
        if rec.message == "advance worker exited unexpectedly"
    ]
    assert crashed and crashed[0].levelno == logging.WARNING


def test_stop_returns_within_timeout_and_leaves_no_child_alive(
    worker_script: Path, spawned: list[subprocess.Popen]
) -> None:
    module = _pool_module()
    queue = _queue()
    queue.request("fast1")
    pool = module.AdvanceWorkerPool(size=1, queue=queue, plan_timeout_s=_plan_timeout())
    pool.start()
    assert _wait_for(lambda: pool.stats()["advances"] == 1)
    assert spawned

    started = time.monotonic()
    pool.stop(timeout_s=5.0)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert all(proc.poll() is not None for proc in spawned)
    assert not any(thread.is_alive() for thread in threading.enumerate()
                   if thread.name.startswith("advance-worker-"))


def test_children_spawn_with_the_source_root_as_cwd(
    worker_script: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _pool_module()
    seen: list[dict] = []
    real_popen = subprocess.Popen

    def _record(*args, **kwargs):
        seen.append(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, "Popen", _record)
    queue = _queue()
    queue.request("fast1")
    pool = module.AdvanceWorkerPool(size=1, queue=queue, plan_timeout_s=_plan_timeout())
    pool.start()
    try:
        assert _wait_for(lambda: pool.stats()["advances"] == 1)
    finally:
        pool.stop(timeout_s=5.0)

    import pipeline

    expected = str(Path(pipeline.__file__).resolve().parent.parent)
    assert seen
    assert seen[0]["cwd"] == expected
    assert seen[0]["stdin"] is subprocess.DEVNULL
    assert seen[0]["stdout"] is subprocess.DEVNULL
    assert "start_new_session" not in seen[0]


def test_invalid_plan_name_is_rejected_without_advancing(worker_script: Path) -> None:
    module = _pool_module()

    class _StubQueue:
        """Yields a traversal-shaped name that the real queue would reject."""

        def __init__(self) -> None:
            self._plans = ["ok", "../evil"]
            self.finished: list[str] = []
            self._lock = threading.Lock()

        def take(self, timeout_s: float | None) -> str | None:
            with self._lock:
                return self._plans.pop(0) if self._plans else None

        def finish(self, plan_name: str) -> None:
            with self._lock:
                self.finished.append(plan_name)

        def close(self) -> None:
            pass

    queue = _StubQueue()
    pool = module.AdvanceWorkerPool(size=1, queue=queue, plan_timeout_s=_plan_timeout())
    pool.start()
    try:
        assert _wait_for(lambda: sorted(queue.finished) == ["../evil", "ok"])
    finally:
        pool.stop(timeout_s=5.0)

    assert pool.stats()["advances"] == 1


def test_stop_terminates_a_stuck_child_and_leaves_none_alive(
    worker_script: Path, spawned: list[subprocess.Popen]
) -> None:
    module = _pool_module()
    queue = _queue()
    queue.request("slow")
    # A deadline far longer than the stop budget: only stop() can end this.
    pool = module.AdvanceWorkerPool(size=1, queue=queue, plan_timeout_s=_plan_timeout(300.0))
    pool.start()
    assert _wait_for(lambda: len(spawned) == 1)

    started = time.monotonic()
    pool.stop(timeout_s=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 10.0
    assert spawned[0].poll() is not None


def test_size_below_one_is_rejected() -> None:
    module = _pool_module()
    with pytest.raises(ValueError):
        module.AdvanceWorkerPool(size=0, queue=_queue(), plan_timeout_s=_plan_timeout())


def test_stats_are_zero_before_any_advance(worker_script: Path) -> None:
    module = _pool_module()
    pool = module.AdvanceWorkerPool(size=2, queue=_queue(), plan_timeout_s=_plan_timeout())
    stats = pool.stats()
    assert stats["advances"] == 0
    assert stats["timeouts"] == 0
    assert stats["respawns"] == 0


def test_stop_without_start_is_a_noop() -> None:
    module = _pool_module()
    pool = module.AdvanceWorkerPool(size=2, queue=_queue(), plan_timeout_s=_plan_timeout())
    pool.stop(timeout_s=0.5)
    assert pool.stats()["advances"] == 0
