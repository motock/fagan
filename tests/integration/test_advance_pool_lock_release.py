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
from pathlib import Path

import pytest

# isort: off
# pipeline.server is imported first on purpose: pre-existing import cycle.
import pipeline.server
import pipeline.advance_pool
# isort: on

PLAN_NAME = "lock-release-demo"

#: Takes the plan's real flock, says so, and then overruns the deadline.
LOCKING_CHILD = """
import sys
import time

from pipeline.concurrency import _plan_lock

with _plan_lock(sys.argv[-2]) as acquired:
    print("locked" if acquired else "not-locked", flush=True)
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


def test_timeout_kill_releases_the_plan_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The child inherits these: a scratch plans directory and no .pipeline.env
    # override, so its _plan_lock lands on tmp_path.
    monkeypatch.setenv("PLAN_DIR", str(tmp_path))
    monkeypatch.setenv("PIPELINE_SKIP_ENV_FILE", "1")
    _use(monkeypatch, LOCKING_CHILD)

    start = time.monotonic()
    result = pipeline.advance_pool.advance_plan_subprocess(PLAN_NAME, 5.0)
    elapsed = time.monotonic() - start

    print(f"child result: {result} after {elapsed:.2f}s")
    assert result["timed_out"] is True
    assert elapsed < 15

    lock_path = tmp_path / f"{PLAN_NAME}.lock"
    assert lock_path.exists(), "the child must have created the lock file"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
        print(f"parent re-acquired {lock_path} without blocking")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    # No leftover state: the same plan advances normally right after the kill.
    _use(monkeypatch, FAST_CHILD)
    assert pipeline.advance_pool.advance_plan_subprocess(PLAN_NAME, 30.0) == {"ok": True}
    print("follow-up advance after the kill returned {'ok': True}")
