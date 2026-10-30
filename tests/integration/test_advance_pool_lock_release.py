"""Real-environment check: killing an advance child releases that plan's flock.

The unit tests substitute a ``python -c`` script, so they prove the runner's
control flow but not the property that makes a child process worth having.
This test proves it end-to-end against the real locking code: the child takes
the real per-plan ``flock`` through ``pipeline.concurrency._plan_lock`` and then
sleeps past the deadline. When the runner terminates the child, the kernel
releases the lock -- the fd is not inheritable, so nothing survives to hold it
-- and the parent can take it immediately with ``LOCK_EX | LOCK_NB``.

That is the whole point of TP-01: an over-deadline advance in the daemon today
is an abandoned thread that keeps holding the lock; a killed child cannot.

The child's stdout is discarded by design (the runner sends it to DEVNULL) and
stderr stays inherited, so the assertion is on the parent's observations: the
call reports ``timed_out`` (so the child really started and slept -- a child
that died at import would report ``exited with code 1``), the lock is free, and
a following run against the same plan name behaves normally.
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

# isort: off
# pipeline.server is imported first on purpose: pre-existing import cycle.
import pipeline.server
import pipeline.advance_pool
# isort: on

PLAN_NAME = "lock-release-demo"

#: How long the child gets to reach the plan lock. That budget has to cover
#: interpreter startup plus the ``pipeline.repo_config -> pipeline.context_sources``
#: import chain, measured at 5.13s / 7.79s / 7.57s on an idle box while
#: ``addopts = "-n auto"`` keeps every other worker busy; the old hardcoded 5.0s
#: SIGKILLed the child before ``_plan_lock`` ever called ``os.open(..., O_CREAT)``,
#: so the lock file was missing for a reason unrelated to lock release.
#: 30.0s is ~4x the worst observation.
CHILD_STARTUP_BUDGET_S = 30.0

#: How long the lock may still read as held after the child is killed. This is
#: a *separate, sequentially-scoped* budget: SIGKILL is asynchronous, so the
#: child's fd is not necessarily released until it has been reaped, and a
#: single shared deadline would let a slow startup consume the whole release
#: window and re-create this flake in a new place.
LOCK_RELEASE_BUDGET_S = 10.0

#: Slack on top of ``CHILD_STARTUP_BUDGET_S`` for the helper's own teardown:
#: ``advance_plan_subprocess`` sends SIGTERM, waits ``_KILL_GRACE_SECONDS``
#: (10s), then SIGKILLs. Only an outer sanity ceiling on the call itself - not
#: the deadline the release assertion races.
_CHILD_TEARDOWN_GRACE_S = 15.0

#: Granularity of both waits above.
_POLL_INTERVAL_S = 0.05

#: Sentinelled path the child writes once the plan lock is actually held.
_SENTINEL_ENV = "PLAN_LOCK_SENTINEL"

#: Takes the plan's real flock, says so, and then overruns the deadline.
LOCKING_CHILD = """
import os
import sys
import time
from pathlib import Path

from pipeline.concurrency import _plan_lock

with _plan_lock(sys.argv[-2]) as acquired:
    print("locked" if acquired else "not-locked", flush=True)
    sentinel = os.environ.get("PLAN_LOCK_SENTINEL")
    if acquired and sentinel:
        # Written here, inside the ``with`` body, because by now _plan_lock has
        # returned from os.open(..., O_CREAT) and taken the flock -- so the file
        # means "the lock is held" and nothing weaker. write_text closes the fd:
        # this process is killed later, and a buffered, unclosed write would
        # leave an empty file behind.
        Path(sentinel).write_text("1", encoding="utf-8")
    time.sleep(60)
"""

#: Writes a result immediately, for the follow-up call on the same plan name.
FAST_CHILD = """
import json
import sys

with open(sys.argv[-1], "w", encoding="utf-8") as fh:
    json.dump({"ok": True}, fh)
"""


def _use(monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Run *script* instead of the child module, keeping the argv shape."""
    monkeypatch.setattr(
        pipeline.advance_pool,
        "_child_argv",
        lambda plan_name, result_path: [sys.executable, "-c", script, plan_name, result_path],
    )


def _wait_for(predicate: Callable[[], bool], budget_s: float) -> bool:
    """Poll *predicate* until it is true or *budget_s* elapses, then report it."""
    deadline = time.monotonic() + budget_s
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL_S)


def _lock_is_free(lock_path: Path) -> bool:
    """Return whether the plan's flock can be taken right now.

    Release is detected the way it always was - by *re-acquiring* the lock,
    not by the file disappearing - only retried until ``LOCK_RELEASE_BUDGET_S``
    expires, because SIGKILL is asynchronous and the child's fd is not
    necessarily released the instant ``kill()`` returns.
    """
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return True


def test_timeout_kill_releases_the_plan_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The child inherits these: a scratch plans directory and no .pipeline.env
    # override, so its _plan_lock lands on tmp_path.
    monkeypatch.setenv("PLAN_DIR", str(tmp_path))
    monkeypatch.setenv("PIPELINE_SKIP_ENV_FILE", "1")
    _use(monkeypatch, LOCKING_CHILD)

    # The child signals "the flock is mine" by writing this file. It lives under
    # tmp_path -- never a fixed /tmp path -- and is removed and re-checked before
    # every spawn, so a run can only ever observe its OWN child's write.
    sentinel = tmp_path / "plan_lock_reached"
    sentinel.unlink(missing_ok=True)
    assert not sentinel.exists(), "the sentinel must not pre-exist the child"
    monkeypatch.setenv(_SENTINEL_ENV, str(sentinel))

    lock_path = tmp_path / f"{PLAN_NAME}.lock"

    try:
        start = time.monotonic()
        result = pipeline.advance_pool.advance_plan_subprocess(
            PLAN_NAME, CHILD_STARTUP_BUDGET_S
        )
        elapsed = time.monotonic() - start

        # ``advance_plan_subprocess`` waits for its child on every path and
        # reaps it (SIGTERM, then SIGKILL) before returning, so by here nothing
        # of ours is still running and the two budgets below are sequential.
        print(f"child result: {result} after {elapsed:.2f}s")
        assert result["timed_out"] is True
        # Outer sanity ceiling on the call as a whole: the startup budget plus
        # the slack this helper needs to tear its own child down (SIGTERM, wait
        # up to ``_KILL_GRACE_SECONDS`` = 10s, then SIGKILL). The release budget
        # is NOT part of this: it only starts once the call has returned.
        assert elapsed < CHILD_STARTUP_BUDGET_S + _CHILD_TEARDOWN_GRACE_S

        # Nothing below means anything unless the child got far enough to take
        # the lock at all: a child that died at import would otherwise
        # "release" a lock it never held and pass for the wrong reason.
        reached = _wait_for(sentinel.exists, CHILD_STARTUP_BUDGET_S)
        assert reached, (
            f"child never reached the plan lock within {CHILD_STARTUP_BUDGET_S}s "
            "-- startup budget exhausted"
        )

        assert lock_path.exists(), "the child must have created the lock file"
        # A fresh deadline starting now, never shared with the startup budget:
        # the kill is asynchronous, so the fd may not be released for a moment.
        released = _wait_for(lambda: _lock_is_free(lock_path), LOCK_RELEASE_BUDGET_S)
        assert released, (
            f"the plan lock was still held {LOCK_RELEASE_BUDGET_S}s after the "
            "child was killed"
        )

        # No leftover state: the same plan advances normally right after the kill.
        _use(monkeypatch, FAST_CHILD)
        assert pipeline.advance_pool.advance_plan_subprocess(PLAN_NAME, 30.0) == {"ok": True}
    finally:
        # Even when an assertion above fails, do not walk away with this plan's
        # lock still held: the next test in this file uses the same plan name
        # and would block behind it, turning one red into many.
        _wait_for(lambda: _lock_is_free(lock_path), LOCK_RELEASE_BUDGET_S)

    print("follow-up advance after the kill returned {'ok': True}")
