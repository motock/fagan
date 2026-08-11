"""Regression tests for the per-plan lock guard around advance_pipeline.

`PipelineService.advance_pipeline` (pipeline/server.py) is supposed to wrap
the real tick body ``_advance_pipeline_locked`` in ``with _plan_lock(...)`` so
that two concurrent ticks for the same plan serialize: the first acquires the
flock and runs the tick; the second finds the flock held and returns
``{"ok": True, "skipped": "locked", ...}`` without running any tick work.

A migration introduced an indentation regression: the
``return _advance_pipeline_locked(plan_name)`` line was dedented to the same
level as the ``with`` statement, so it runs *after* the ``with`` block exits —
i.e. the flock is released *before* the tick body runs. The lock is acquired
only to be immediately released, so it protects nothing: two concurrent ticks
both run ``_advance_pipeline_locked`` at the same time and the
``skipped: "locked"`` path is never hit.

These tests reproduce that race. They are fully standalone (own fixtures and
helpers, no cross-file imports of test code) and are written to FAIL against
the buggy code and PASS once the ``return`` is indented back inside the
``with`` block.
"""

import json
import threading
import time

import pytest

import pipeline.server as p

from pipeline import concurrency as pcon
from pipeline import persistence as ppers


# ---------------------------------------------------------------------------
# Fixtures (mirror test_review_story_lock_guard.py / test_pipeline_mcp_server.py
# so this file is fully standalone).
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


# ---------------------------------------------------------------------------
# (1) Two concurrent advance_pipeline ticks for the same plan must serialize:
#     exactly one runs the tick body, the other observes skipped:"locked".
# ---------------------------------------------------------------------------

def test_advance_pipeline_serializes_concurrent_ticks(plan_dir, monkeypatch):
    """The core lock-contract regression.

    We replace the real ``_advance_pipeline_locked`` with a fake that (a) is
    deliberately slow and (b) records how many tick bodies are running
    *simultaneously*. We then fire two threads calling
    ``PipelineService.advance_pipeline`` for the same plan at the same time.

    Contract (fixed code):
      - Exactly one thread runs the fake tick body (max concurrency == 1).
      - The other thread returns ``{"ok": True, "skipped": "locked", ...}``
        and never enters the tick body.

    Buggy code (current): the ``return _advance_pipeline_locked(...)`` is
    outside the ``with _plan_lock`` block, so the flock is released before
    the slow tick body runs. Both threads acquire-and-release the flock
    instantly, then both run the tick body concurrently: max concurrency
    reaches 2 and neither thread returns ``skipped: "locked"``.
    """
    # A manifest must exist so the real _advance_pipeline_locked would not
    # short-circuit on "No manifest"; the fake ignores it but we keep the
    # setup realistic.
    (plan_dir / "advplan.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {}})
    )

    max_concurrency = {"current": 0, "peak": 0}
    lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    def fake_tick(plan_name):
        with lock:
            max_concurrency["current"] += 1
            max_concurrency["peak"] = max(max_concurrency["peak"], max_concurrency["current"])
        started.set()
        # Hold the "tick" open until the test releases it, simulating 200ms+
        # of real tick work during which a concurrent caller must be blocked.
        release.wait(timeout=5.0)
        with lock:
            max_concurrency["current"] -= 1
        return {"ok": True, "ran": True, "plan": plan_name}

    monkeypatch.setattr(p, "_advance_pipeline_locked", fake_tick)

    results = {}
    errors = []

    def call_advance():
        try:
            results[threading.get_ident()] = p._service.advance_pipeline("advplan")
        except Exception as exc:  # pragma: no cover - surface unexpected errors
            errors.append(exc)

    t1 = threading.Thread(target=call_advance)
    t2 = threading.Thread(target=call_advance)
    t1.start()
    t2.start()

    # Give both threads a chance to enter. With the buggy code both will be
    # inside the tick body simultaneously; with the fixed code only one will
    # be inside and the other will have already returned skipped:"locked".
    assert started.wait(timeout=5.0), "tick body never started"
    # Let the second thread race in (buggy code) or hit the lock (fixed code).
    time.sleep(0.2)

    # Release the held tick so the test can finish.
    release.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert errors == [], f"threads raised unexpectedly: {errors!r}"

    outcomes = list(results.values())
    assert len(outcomes) == 2, f"expected two results, got {outcomes!r}"

    skipped = [r for r in outcomes if r.get("skipped") == "locked"]
    ran = [r for r in outcomes if r.get("ran") is True]

    # The fixed contract: exactly one tick ran, exactly one skipped.
    assert len(ran) == 1, (
        f"expected exactly one tick body to run, got {len(ran)}; "
        f"outcomes={outcomes!r}"
    )
    assert len(skipped) == 1, (
        f"expected exactly one skipped:'locked' result, got {len(skipped)}; "
        f"outcomes={outcomes!r}"
    )
    assert skipped[0]["ok"] is True
    assert "another advance_pipeline tick" in skipped[0].get("reason", ""), (
        f"unexpected skip reason: {skipped[0]!r}"
    )

    # The peak simultaneous tick-body count must be 1 (serialized), never 2.
    assert max_concurrency["peak"] == 1, (
        f"two tick bodies ran concurrently (peak={max_concurrency['peak']}); "
        "the _plan_lock is not protecting advance_pipeline's tick body"
    )


# ---------------------------------------------------------------------------
# (2) advance_pipeline holds the lock for the WHOLE tick, not just an instant.
#     A second caller arriving while the first tick is mid-flight must skip.
# ---------------------------------------------------------------------------

def test_advance_pipeline_second_caller_skips_while_first_runs(plan_dir, monkeypatch):
    """A more direct proxy for the race: thread 1 enters the (slow) tick body
    and holds it open; thread 2, started afterwards, must observe the lock as
    held and return skipped:"locked" immediately — it must NOT run the tick
    body.

    Buggy code: thread 1 releases the flock before entering the slow body, so
    thread 2 acquires the flock, releases it, and ALSO runs the tick body.
    """
    (plan_dir / "advplan2.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {}})
    )

    entered = threading.Event()
    release = threading.Event()
    tick_calls = {"count": 0}
    lock = threading.Lock()

    def fake_tick(plan_name):
        with lock:
            tick_calls["count"] += 1
        entered.set()
        release.wait(timeout=5.0)
        return {"ok": True, "ran": True, "plan": plan_name}

    monkeypatch.setattr(p, "_advance_pipeline_locked", fake_tick)

    results = {}

    def call_advance(key):
        results[key] = p._service.advance_pipeline("advplan2")

    t1 = threading.Thread(target=call_advance, args=("first",))
    t2 = threading.Thread(target=call_advance, args=("second",))
    t1.start()
    assert entered.wait(timeout=5.0), "first tick body never started"
    # First thread is now inside the (blocked) tick body. Start the second.
    t2.start()
    # Give the second thread time to either skip (fixed) or run (buggy).
    time.sleep(0.2)

    # Now release the first tick and join both.
    release.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert "first" in results and "second" in results, results
    assert results["first"].get("ran") is True, results
    # The second caller must have skipped, not run.
    assert results["second"].get("skipped") == "locked", (
        f"second concurrent caller should have skipped with 'locked', "
        f"got {results['second']!r}"
    )
    assert tick_calls["count"] == 1, (
        f"tick body should have run exactly once, ran {tick_calls['count']} "
        f"times — the lock is not held for the duration of the tick"
    )