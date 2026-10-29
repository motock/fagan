# ruff: noqa: I001  -- pipeline.server must be imported before any other
# pipeline module (pre-existing import cycle; see test_triage_cap_silent_skip).
"""Tests for the coalescing per-plan advance queue (ADR 0011 decision 3).

A state change enqueues "advance plan X"; requests for the same plan coalesce
while one is queued. A request that arrives while the plan is already in
flight marks it dirty instead, so it is re-queued at the tail once the run
finishes. The queue is in-process state with no persistence: the reconcile
sweep refills it after a restart.
"""

from __future__ import annotations

import threading
import time

import pytest

# pipeline.server must be imported before any other pipeline.* module: the
# package has a pre-existing import cycle, so a cold import of a leaf module
# first fails. Same idiom as tests/unit/test_triage_cap_silent_skip.py.
import pipeline.server  # noqa: F401
from pipeline.advance_queue import AdvanceQueue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drain(queue: AdvanceQueue) -> list[str]:
    """Take every queued plan, stopping when the queue reports empty."""
    taken: list[str] = []
    while True:
        plan = queue.take(0.05)
        if plan is None:
            return taken
        taken.append(plan)


# ---------------------------------------------------------------------------
# Ordering and coalescing
# ---------------------------------------------------------------------------


def test_fifo_order_across_three_plans():
    queue = AdvanceQueue()

    assert queue.request("alpha") is True
    assert queue.request("beta") is True
    assert queue.request("gamma") is True

    assert queue.take(1.0) == "alpha"
    assert queue.take(1.0) == "beta"
    assert queue.take(1.0) == "gamma"
    assert queue.take(0.05) is None


def test_second_request_for_queued_plan_coalesces():
    queue = AdvanceQueue()

    assert queue.request("alpha") is True
    assert queue.request("alpha") is False
    assert queue.request("alpha") is False

    assert queue.take(1.0) == "alpha"
    assert queue.take(0.05) is None


def test_request_while_in_flight_requeues_behind_intervening_plan():
    queue = AdvanceQueue()

    assert queue.request("apply") is True
    assert queue.take(1.0) == "apply"

    # A different plan is requested while "apply" is in flight.
    assert queue.request("bootstrap") is True
    # Repeated requests for the in-flight plan coalesce into one dirty mark.
    assert queue.request("apply") is False
    assert queue.request("apply") is False

    queue.finish("apply")

    # The dirty plan goes to the TAIL, behind the plan already waiting.
    assert queue.take(1.0) == "bootstrap"
    assert queue.take(1.0) == "apply"
    assert queue.take(0.05) is None


def test_finish_without_dirty_mark_does_not_requeue_and_sets_last_advanced():
    queue = AdvanceQueue()

    assert queue.request("alpha") is True
    assert queue.take(1.0) == "alpha"
    assert queue.request("beta") is True

    queue.finish("alpha")

    advanced = queue.last_advanced("alpha")
    assert isinstance(advanced, float)
    assert queue.take(1.0) == "beta"
    assert queue.take(0.05) is None


# ---------------------------------------------------------------------------
# Blocking behaviour
# ---------------------------------------------------------------------------


def test_take_times_out_on_empty_queue():
    queue = AdvanceQueue()

    started = time.monotonic()
    assert queue.take(0.05) is None
    elapsed = time.monotonic() - started

    assert elapsed < 1.0


def test_blocked_take_returns_plan_after_request():
    queue = AdvanceQueue()
    worker_started = threading.Event()
    result: list[str | None] = []

    def worker() -> None:
        worker_started.set()
        result.append(queue.take(None))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert worker_started.wait(timeout=2), "worker thread never started"
    # Give the worker a moment to actually enter the wait.
    time.sleep(0.05)

    assert queue.request("alpha") is True

    thread.join(timeout=2)
    assert not thread.is_alive(), "take(None) did not wake on request"
    assert result == ["alpha"]


def test_close_releases_blocked_take_and_rejects_requests():
    queue = AdvanceQueue()
    worker_started = threading.Event()
    result: list[str | None] = []

    def worker() -> None:
        worker_started.set()
        result.append(queue.take(None))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert worker_started.wait(timeout=2), "worker thread never started"
    time.sleep(0.05)

    queue.close()

    thread.join(timeout=2)
    assert not thread.is_alive(), "close() did not wake the blocked take"
    assert result == [None]

    assert queue.request("alpha") is False
    assert queue.take(0.05) is None


# ---------------------------------------------------------------------------
# Boundary validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_name", ["", "../x"])
def test_invalid_plan_name_raises_value_error(bad_name):
    queue = AdvanceQueue()

    with pytest.raises(ValueError):
        queue.request(bad_name)

    # An empty string is a substring of every message, so only the
    # non-empty case can assert on the message text.
    if bad_name:
        with pytest.raises(ValueError) as excinfo:
            queue.request(bad_name)
        assert bad_name not in str(excinfo.value)


def test_non_str_plan_name_raises_type_error_without_echoing_value():
    queue = AdvanceQueue()

    with pytest.raises(TypeError) as excinfo:
        queue.request(123)

    assert "123" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_requests_leave_each_plan_queued_at_most_once():
    queue = AdvanceQueue()
    plans = ["p0", "p1", "p2", "p3", "p4"]
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait(timeout=5)
        for _ in range(200):
            for plan in plans:
                queue.request(plan)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "requester thread did not finish"

    taken = _drain(queue)
    assert sorted(taken) == plans
