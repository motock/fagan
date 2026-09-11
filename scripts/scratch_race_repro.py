"""Standalone repro of the flaky timeout-scenario ordering (scratch, not a test).

Mirrors _run_timeout_scenario: owner thread holds the plan lock, starts a
contender thread, then opens the release window. If the contender's
_plan_lock lands before the window opens, it records False and the owner's
assert fires — the same failure the suite shows intermittently.
"""

import threading
import time
import os
import tempfile

from pipeline import concurrency

FAILS = 0
RUNS = 60

for run in range(RUNS):
    tmp = tempfile.mkdtemp()
    concurrency.PLAN_DIR = tmp
    concurrency._held_plan_locks().clear()
    concurrency._held_plan_lock_fds().clear()
    plan = "race"
    contender_acquired = threading.Event()
    outcome = {}
    window_opened = threading.Event()

    def contender():
        with concurrency._plan_lock(plan) as ok:
            outcome["acquired"] = bool(ok)
            contender_acquired.set()

    def owner():
        with concurrency._plan_lock(plan) as ok:
            assert ok is True
            thread = threading.Thread(target=contender, daemon=True)
            thread.start()
            with concurrency._released_plan_lock(plan):
                window_opened.set()
                contender_acquired.wait(10)
            outcome["reacquired"] = True

    worker = threading.Thread(target=owner, daemon=True)
    worker.start()
    worker.join(10)
    if worker.is_alive() or outcome.get("acquired") is not True:
        FAILS += 1
        print(f"run {run}: FAIL acquired={outcome.get('acquired')} alive={worker.is_alive()}")
    # cleanup: drop any lock still held by this thread
    try:
        concurrency._held_plan_locks().discard(plan)
        fd = concurrency._held_plan_lock_fds().pop(plan, None)
        if fd is not None:
            os.close(fd)
    except OSError:
        pass

print(f"{RUNS - FAILS}/{RUNS} runs OK, {FAILS} failures")