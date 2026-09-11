"""Tests for ``pipeline.concurrency._released_plan_lock``.

``advance_pipeline`` holds the per-plan flock for its whole tick, including
multi-minute synchronous model calls. ``_released_plan_lock`` is the
primitive that lets a caller drop that lock around such a phase and take it
back afterwards, without changing any caller yet.

Contract pinned by this suite (the implementer must satisfy exactly this):

- ``pipeline.concurrency._released_plan_lock(plan_name)`` is a
  ``@contextmanager``.
- Enter, thread does NOT hold the plan: silent no-op — yields, raises
  nothing, opens no lock file, leaves ``_held_plan_locks()`` unchanged on
  both enter and exit.
- Enter, thread DOES hold the plan: the real flock is released (another
  thread AND another process can acquire it while the body runs) and the
  plan is removed from the held set for the duration of the body.
- Exit: re-acquire, BLOCKING, with a bounded timeout read at call time from
  ``PIPELINE_PLAN_LOCK_REACQUIRE_TIMEOUT_SECONDS`` (default 300; a
  malformed value degrades to the default instead of raising, at import
  time or call time). On timeout a named exception
  ``pipeline.concurrency.PlanLockReacquireTimeout`` is raised whose message
  names the plan — never silently continuing without the lock, never
  failing open.
- A successful re-acquire restores the held-set entry so later nested
  ``_plan_lock`` calls in the same tick still see the plan as held.
- An exception raised by the body still triggers the re-acquire (it must
  live in a ``finally``) and then propagates unchanged.
- Release/re-acquire cycles leak no file descriptor and leave no orphan
  flock behind once the outer ``_plan_lock`` exits.
- ``"_released_plan_lock"`` is a member of the module ``__all__``
  (membership only — ``__all__`` is cumulative; later stories append more
  entries, so its exact contents/length must never be asserted).

Every test points ``pipeline.concurrency.PLAN_DIR`` at a per-test
``tmp_path`` so nothing ever flocks the real ~/.claude/plans directory.
"""

import importlib
import os
import subprocess
import sys
import textwrap
import threading

import pytest

from pipeline import concurrency

REACQUIRE_TIMEOUT_ENV = "PIPELINE_PLAN_LOCK_REACQUIRE_TIMEOUT_SECONDS"


@pytest.fixture(autouse=True)
def _plan_dir_and_held_set_isolation(tmp_path, monkeypatch):
    """Point PLAN_DIR at tmp_path and give every test an empty held set."""
    monkeypatch.setattr(concurrency, "PLAN_DIR", tmp_path)
    concurrency._held_plan_locks().clear()
    yield
    concurrency._held_plan_locks().clear()


def _probe_acquire(plan_name, timeout=5.0):
    """Run ``_plan_lock(plan_name)`` in a fresh thread; return True/False.

    A fresh thread has an empty per-thread held set, so the probe hits the
    real flock — this is what proves the flock itself moved, not just the
    held-set bookkeeping.
    """
    result = {}

    def run():
        try:
            with concurrency._plan_lock(plan_name) as ok:
                result["acquired"] = bool(ok)
        except Exception as exc:  # noqa: BLE001 - recorded, classified below
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f"probe thread for {plan_name!r} did not finish"
    assert "error" not in result, f"probe thread raised: {result['error']!r}"
    return result.get("acquired")


def _count_os_calls(monkeypatch):
    """Instrument os.open/os.close with counters; return the counter dict."""
    real_open, real_close = os.open, os.close
    calls = {"open": 0, "close": 0}

    def counting_open(path, flags, *args, **kwargs):
        calls["open"] += 1
        return real_open(path, flags, *args, **kwargs)

    def counting_close(fd, *args, **kwargs):
        calls["close"] += 1
        return real_close(fd, *args, **kwargs)

    monkeypatch.setattr(os, "open", counting_open)
    monkeypatch.setattr(os, "close", counting_close)
    return calls


def _run_timeout_scenario(monkeypatch, tmp_path, plan, env_value):
    """Shared body for the two re-acquire-timeout tests.

    A worker thread plays the tick: it holds the plan lock, opens a
    ``_released_plan_lock`` window, lets a contender thread take the flock
    inside the window, then exits the window. Everything runs in a daemon
    thread joined under a deadline so a broken implementation FAILS the
    test instead of hanging the suite.
    """
    if env_value is None:
        monkeypatch.delenv(REACQUIRE_TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(REACQUIRE_TIMEOUT_ENV, env_value)
    # Reload so an implementation that resolves the timeout at import time
    # also honours the env value; a call-time implementation reads it
    # directly. Reload resets module globals, so re-patch PLAN_DIR.
    importlib.reload(concurrency)
    monkeypatch.setattr(concurrency, "PLAN_DIR", tmp_path)

    contender_acquired = threading.Event()
    release_contender = threading.Event()
    done = threading.Event()
    outcome = {}

    def contender():
        with concurrency._plan_lock(plan) as ok:
            outcome["contender_acquired"] = bool(ok)
            contender_acquired.set()
            release_contender.wait(10)

    def owner():
        try:
            with concurrency._plan_lock(plan) as ok:
                assert ok is True, "tick owner must acquire the plan lock"
                thread = threading.Thread(target=contender, daemon=True)
                thread.start()
                with concurrency._released_plan_lock(plan):
                    assert contender_acquired.wait(10), (
                        "contender must acquire inside the release window"
                    )
                    assert outcome.get("contender_acquired") is True
                # Exiting the window: blocking re-acquire while the
                # contender still holds the flock.
                outcome["reacquired_without_raise"] = True
                outcome["held_after_reacquire"] = plan in concurrency._held_plan_locks()
        except Exception as exc:  # noqa: BLE001 - classified by the main thread
            outcome["exc"] = exc
            outcome["held_after_exc"] = plan in concurrency._held_plan_locks()
        finally:
            outcome["done"] = True
            release_contender.set()
            done.set()

    worker = threading.Thread(target=owner, daemon=True)
    worker.start()
    return contender_acquired, release_contender, done, outcome, worker


def test_released_lock_lets_second_thread_acquire_while_body_runs():
    """Case 1: inside _plan_lock, the released window really frees the flock."""
    plan = "relock-basic"
    contender_acquired = threading.Event()
    outcome = {}
    body_entered = False

    def contender():
        with concurrency._plan_lock(plan) as ok:
            outcome["acquired"] = bool(ok)
            contender_acquired.set()

    with concurrency._plan_lock(plan) as ok:
        assert ok is True, "outer _plan_lock must acquire for this test to mean anything"
        with concurrency._released_plan_lock(plan):
            body_entered = True
            # the held-set entry is gone for the duration of the body
            assert plan not in concurrency._held_plan_locks()
            thread = threading.Thread(target=contender, daemon=True)
            thread.start()
            assert contender_acquired.wait(5), (
                "second thread could not acquire during the released window: "
                "the flock was not really released"
            )
            assert outcome["acquired"] is True
            thread.join(5)
            assert not thread.is_alive()
        # exiting the window re-acquires
        assert plan in concurrency._held_plan_locks()
    assert body_entered is True
    assert plan not in concurrency._held_plan_locks()


def test_after_exit_outer_thread_holds_lock_again():
    """Case 2: after exit the flock and the held-set entry are both back."""
    plan = "relock-reacquired"
    with concurrency._plan_lock(plan) as ok:
        assert ok is True
        with concurrency._released_plan_lock(plan):
            assert plan not in concurrency._held_plan_locks()
        assert plan in concurrency._held_plan_locks(), (
            "exiting _released_plan_lock must restore the held-set entry"
        )
        # reentrance still works in the same tick without re-flocking
        with concurrency._plan_lock(plan) as nested:
            assert nested is True, (
                "nested _plan_lock after re-acquire must see the plan as held"
            )
        # and the flock is really held again: a second thread cannot acquire
        assert _probe_acquire(plan) is False, (
            "second thread acquired although the outer tick holds the lock again"
        )
    assert plan not in concurrency._held_plan_locks()


def test_noop_when_thread_does_not_hold_the_lock(monkeypatch):
    """Case 3: releasing a lock this thread does not hold is a silent no-op."""
    plan = "relock-noop"
    before = set(concurrency._held_plan_locks())
    calls = _count_os_calls(monkeypatch)

    body_ran = False
    with concurrency._released_plan_lock(plan):
        body_ran = True
        assert set(concurrency._held_plan_locks()) == before
    assert body_ran is True, "_released_plan_lock must yield so the body runs"
    assert set(concurrency._held_plan_locks()) == before, "held set changed"
    assert calls == {"open": 0, "close": 0}, (
        f"the no-op path must do nothing at all (no fd, no flock), saw {calls}"
    )


def test_body_exception_propagates_and_lock_is_reacquired():
    """Case 4: a body exception propagates AND the lock is re-acquired."""
    plan = "relock-exc"
    with concurrency._plan_lock(plan) as ok:
        assert ok is True
        with pytest.raises(ValueError, match="boom"), concurrency._released_plan_lock(plan):
            raise ValueError("boom")
        # the re-acquire lives in a finally: it must have run before the
        # exception finished propagating out of the released block
        assert plan in concurrency._held_plan_locks(), (
            "exception path must still re-acquire the lock"
        )
        assert _probe_acquire(plan) is False, (
            "re-acquired lock after an exception is not the real flock"
        )
    assert plan not in concurrency._held_plan_locks()


def test_reacquire_timeout_raises_named_exception(monkeypatch, tmp_path):
    """Case 5: a contended re-acquire times out and raises the named error."""
    plan = "relock-timeout"
    contender_acquired, release_contender, done, outcome, worker = _run_timeout_scenario(
        monkeypatch, tmp_path, plan, "0.2"
    )
    del contender_acquired, release_contender
    worker.join(15)
    assert outcome.get("done") is True and done.is_set(), (
        "re-acquire did not settle within 15s - the env timeout was not honoured"
    )
    assert "reacquired_without_raise" not in outcome, (
        "re-acquire succeeded while another thread held the flock: "
        "_released_plan_lock must raise on timeout, never fail open"
    )
    exc = outcome.get("exc")
    assert isinstance(exc, concurrency.PlanLockReacquireTimeout), (
        f"expected PlanLockReacquireTimeout, got {exc!r}"
    )
    assert plan in str(exc), f"exception message must name the plan, got: {exc}"


def test_malformed_timeout_env_degrades_to_default(monkeypatch, tmp_path):
    """Case 6: a malformed env value degrades to the 300s default, no raise."""
    plan = "relock-malformed"
    monkeypatch.setenv(REACQUIRE_TIMEOUT_ENV, "abc")
    # must not raise at import time either, whichever point the module reads
    importlib.reload(concurrency)
    monkeypatch.setattr(concurrency, "PLAN_DIR", tmp_path)

    # call time, no-op path: must not raise
    with concurrency._released_plan_lock("relock-malformed-noop"):
        pass

    contender_acquired, release_contender, done, outcome, worker = _run_timeout_scenario(
        monkeypatch, tmp_path, plan, "abc"
    )
    assert contender_acquired.wait(10), "contender must acquire inside the window"
    # a malformed value degrades to the long default: 0.3s later the worker
    # must still be blocked in the re-acquire - not raised, not failed open.
    assert not done.wait(0.3), (
        "re-acquire settled within 0.3s despite a malformed timeout: it must "
        "degrade to the long default, not fail fast and not fail open"
    )
    release_contender.set()
    worker.join(10)
    assert not worker.is_alive(), "worker still blocked after the contender released"
    assert outcome.get("done") is True
    assert outcome.get("exc") is None, (
        f"malformed env value must not raise, got: {outcome.get('exc')!r}"
    )
    assert outcome.get("held_after_reacquire") is True
    assert outcome.get("contender_acquired") is True
    # leave the module pristine for the rest of the suite
    monkeypatch.delenv(REACQUIRE_TIMEOUT_ENV, raising=False)
    importlib.reload(concurrency)


def test_default_timeout_is_long_when_env_unset(monkeypatch, tmp_path):
    """With the env unset the re-acquire uses the long 300s default.

    The literal 300 cannot be asserted in reasonable test time; what is
    mechanically checkable is that an unset env means the re-acquire stays
    BLOCKING well past any short window - it neither fails open nor
    times out quickly.
    """
    plan = "relock-default"
    contender_acquired, release_contender, done, outcome, worker = _run_timeout_scenario(
        monkeypatch, tmp_path, plan, None
    )
    assert contender_acquired.wait(10), "contender must acquire inside the window"
    assert not done.wait(0.3), (
        "re-acquire settled within 0.3s with the env unset: the default must "
        "be a long blocking timeout, not a fast fail"
    )
    release_contender.set()
    worker.join(10)
    assert not worker.is_alive(), "worker still blocked after the contender released"
    assert outcome.get("done") is True
    assert outcome.get("exc") is None, f"unexpected error: {outcome.get('exc')!r}"
    assert outcome.get("held_after_reacquire") is True
    assert outcome.get("contender_acquired") is True
    monkeypatch.delenv(REACQUIRE_TIMEOUT_ENV, raising=False)
    importlib.reload(concurrency)


def test_nested_released_lock_inner_is_noop():
    """Case 7: an inner _released_plan_lock for the same plan is a no-op."""
    plan = "relock-nested"
    with concurrency._plan_lock(plan) as ok:
        assert ok is True
        with concurrency._released_plan_lock(plan):
            assert plan not in concurrency._held_plan_locks()
            with concurrency._released_plan_lock(plan):
                # the outer already released: the inner must not re-acquire
                assert plan not in concurrency._held_plan_locks()
            assert plan not in concurrency._held_plan_locks(), (
                "inner block exit re-acquired although the outer window is "
                "still open - the held set is corrupted"
            )
        assert plan in concurrency._held_plan_locks()
    assert plan not in concurrency._held_plan_locks()


def test_released_plan_lock_is_exported_in_all():
    """Case 8: __all__ membership only - the list is cumulative, never exact."""
    assert "_released_plan_lock" in concurrency.__all__


def test_no_orphan_flock_after_outer_plan_lock_exits():
    """The flock must not leak when the outer tick ends after a cycle."""
    plan = "relock-noleak"
    with concurrency._plan_lock(plan) as ok:
        assert ok is True
        with concurrency._released_plan_lock(plan):
            pass
    # the whole tick is over: even if the re-acquire moved the lock onto a
    # different fd, the outer exit must have released the real flock
    assert _probe_acquire(plan) is True, (
        "flock leaked after the outer _plan_lock exited following a "
        "release/re-acquire cycle"
    )


def test_no_fd_leak_across_release_cycles(monkeypatch):
    """No descriptor may leak per release/re-acquire cycle."""
    plan = "relock-fd"
    calls = _count_os_calls(monkeypatch)
    with concurrency._plan_lock(plan) as ok:
        assert ok is True
        for _ in range(5):
            with concurrency._released_plan_lock(plan):
                pass
    assert calls["open"] >= 1, (
        "instrumentation observed no os.open calls - if _plan_lock no longer "
        "opens the lock file via os.open, update this test"
    )
    assert calls["open"] == calls["close"], (
        f"fd leak across release/re-acquire cycles: {calls}"
    )


def test_released_lock_is_visible_to_another_process(tmp_path):
    """Another PROCESS must be able to take the flock inside the window."""
    plan = "relock-proc"
    lock_path = tmp_path / f"{plan}.lock"
    probe = textwrap.dedent(
        """
        import fcntl, os, sys
        fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(3)
        os.close(fd)
        sys.exit(0)
        """
    )

    def probe_lock():
        proc = subprocess.run(
            [sys.executable, "-c", probe, str(lock_path)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        return proc.returncode

    with concurrency._plan_lock(plan) as ok:
        assert ok is True
        assert probe_lock() == 3, "outer _plan_lock must hold the flock for real"
        with concurrency._released_plan_lock(plan):
            assert probe_lock() == 0, (
                "another PROCESS must be able to acquire while the window is open"
            )
        assert probe_lock() == 3, "after exit the flock must be held again"
