"""The zombie reaper reaps dead-pid stories through the record store.

Story: "Reap dead-pid stories through the record store under each plan's lock".

``_reap_zombie_in_progress_stories`` used to glob ``PLAN_DIR`` directly and
write each manifest with ``_atomic_write_json`` with no plan lock held, so a
concurrent dispatch/status write landing between its read and its write was
silently lost. It now:

* enumerates plans with ``_store.records().list_plans("default")``,
* takes ``_store.transaction(plan_name)`` (non-blocking) around each plan's
  read-modify-write, skipping a busy plan (it is reaped on the next tick),
* reads with ``records().get_manifest`` and writes with
  ``records().put_manifest``, so a manifest is never written back unless at
  least one story was actually reaped.

``os.kill`` is monkeypatched at the process boundary (``concurrency.os.kill``)
so liveness is deterministic without signalling a real process.
"""

from __future__ import annotations

import json
import threading

import pytest

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server
from pipeline import concurrency as pcon
from pipeline import store as pstore


def _file_store() -> pstore.FileStore:
    fn = getattr(pstore, "build_store", None)
    assert fn is not None, "pipeline.store.build_store is not defined"
    return fn({"PIPELINE_STORE": "file"})


@pytest.fixture
def reaper(tmp_path, monkeypatch):
    """A real FileStore rooted at ``tmp_path`` wired into ``pipeline.server``.

    ``concurrency._store`` resolves through the server at call time, and
    ``FileStore`` reads ``pipeline.server.PLAN_DIR`` at call time too, so
    patching the server leaves the files the reaper writes in ``tmp_path``.
    """
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", plans)
    store = _file_store()
    monkeypatch.setattr(pipeline.server, "_store", store)
    monkeypatch.setattr(pcon, "PLAN_DIR", plans)
    return store, plans


def _write(store, plan_name: str, stories: dict) -> None:
    store.records().put_manifest("default", plan_name, {"stories": stories})


def _read(plans, plan_name: str) -> dict:
    return json.loads((plans / f"{plan_name}.manifest.json").read_text())


class _Kill:
    """A fake ``os.kill``: ``ProcessLookupError`` for dead pids, and
    ``PermissionError`` for pids that exist but are not ours to signal."""

    def __init__(self, dead=(), denied=()):
        self.dead = set(dead)
        self.denied = set(denied)
        self.calls: list[int] = []

    def __call__(self, pid, sig):
        self.calls.append(pid)
        if pid in self.denied:
            raise PermissionError(1, "Operation not permitted")
        if pid in self.dead:
            raise ProcessLookupError(3, "No such process")
        # Every other pid is alive: returning normally means "alive".


def test_should_reset_a_dead_pid_story_to_todo_through_the_record_store(
    reaper, monkeypatch
):
    store, plans = reaper
    _write(store, "p1", {
        "s1": {"summary": "dead", "status": "in_progress", "pid": 4242},
        "s2": {"summary": "live", "status": "in_progress", "pid": 5151},
    })
    monkeypatch.setattr(pcon.os, "kill", _Kill(dead=[4242]))

    assert pcon._reap_zombie_in_progress_stories() == 1

    after = _read(plans, "p1")
    assert after["stories"]["s1"] == {"summary": "dead", "status": "todo"}
    assert after["stories"]["s2"] == {
        "summary": "live", "status": "in_progress", "pid": 5151,
    }


def test_should_leave_a_live_pid_story_alone(reaper, monkeypatch):
    store, plans = reaper
    stories = {"s1": {"summary": "live", "status": "in_progress", "pid": 5151}}
    _write(store, "p1", stories)
    before = _read(plans, "p1")
    monkeypatch.setattr(pcon.os, "kill", _Kill())

    assert pcon._reap_zombie_in_progress_stories() == 0
    assert _read(plans, "p1") == before


def test_should_leave_a_permission_denied_pid_alone(reaper, monkeypatch):
    """``PermissionError`` means the process exists but is not ours to signal:
    trust that it is alive. ``PermissionError`` subclasses ``OSError``, so a
    broader ``except`` here would reap a live story."""
    store, plans = reaper
    _write(store, "p1", {
        "s1": {"summary": "someone else's", "status": "in_progress", "pid": 7000},
    })
    before = _read(plans, "p1")
    monkeypatch.setattr(pcon.os, "kill", _Kill(denied=[7000]))

    assert pcon._reap_zombie_in_progress_stories() == 0
    assert _read(plans, "p1") == before


def test_should_skip_a_plan_whose_lock_is_busy_and_reap_it_next_call(
    reaper, monkeypatch
):
    """A plan whose lock is held elsewhere is skipped, not raced — and reaped
    on the next call once the lock is free.

    The lock is thread-reentrant, so the busy holder must be a *second thread*:
    taking it on the test's own thread would let the reaper acquire it and the
    test would prove nothing.
    """
    store, plans = reaper
    _write(store, "p1", {
        "s1": {"summary": "dead", "status": "in_progress", "pid": 4242},
        "s2": {"summary": "live", "status": "in_progress", "pid": 5151},
    })
    before = _read(plans, "p1")
    monkeypatch.setattr(pcon.os, "kill", _Kill(dead=[4242]))

    acquired_cm = threading.Event()
    release_cm = threading.Event()
    holds: list[bool] = []
    failure: list[BaseException] = []

    def _hold():
        try:
            with store.transaction("p1") as acquired:
                holds.append(acquired)
                acquired_cm.set()
                release_cm.wait(10)
        except BaseException as exc:  # noqa: BLE001 - surfaced on the main thread
            failure.append(exc)
            acquired_cm.set()

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    try:
        assert acquired_cm.wait(10), "the busy-lock holder never started"
        assert not failure, f"busy-lock holder raised: {failure!r}"
        assert holds == [True], "the busy-lock holder failed to take the lock"

        # Call 1: p1 is busy -> skipped, nothing written, count 0.
        assert pcon._reap_zombie_in_progress_stories() == 0
        assert _read(plans, "p1") == before, "a busy plan must not be written"
    finally:
        release_cm.set()
        holder.join(10)
    assert not holder.is_alive(), "the busy-lock holder did not release"

    # Call 2: lock free -> reap s1, leave s2 alone.
    assert pcon._reap_zombie_in_progress_stories() == 1
    after = _read(plans, "p1")
    assert after["stories"]["s1"] == {"summary": "dead", "status": "todo"}
    assert after["stories"]["s2"] == {
        "summary": "live", "status": "in_progress", "pid": 5151,
    }

    # Call 3: nothing matches the predicate any more -> 0, and no rewrite.
    assert pcon._reap_zombie_in_progress_stories() == 0
    assert _read(plans, "p1") == after


def test_should_not_write_a_plan_with_nothing_to_reap(reaper, monkeypatch):
    """Writing back unconditionally would bump mtime (and, under
    ``PIPELINE_STORE=sqlite``, rewrite the row) on every scheduler tick."""
    store, plans = reaper
    _write(store, "p2", {"s2": {"summary": "live", "status": "in_progress", "pid": 5151}})
    path = plans / "p2.manifest.json"
    before_bytes = path.read_bytes()
    before_stat = path.stat()

    writes: list[str] = []
    real_put = store.records().put_manifest

    def _counting_put(namespace, plan_name, manifest):
        writes.append(plan_name)
        return real_put(namespace, plan_name, manifest)

    monkeypatch.setattr(store.records(), "put_manifest", _counting_put)
    monkeypatch.setattr(pcon.os, "kill", _Kill())

    assert pcon._reap_zombie_in_progress_stories() == 0
    assert writes == [], "a plan with nothing to reap must not be written"
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns


def test_should_return_the_number_reaped_across_plans(reaper, monkeypatch):
    store, plans = reaper
    _write(store, "p1", {"a": {"status": "in_progress", "pid": 11}})
    _write(store, "p2", {"b": {"status": "in_progress", "pid": 22}})
    _write(store, "p3", {
        "c": {"status": "in_progress", "pid": 33},
        "d": {"status": "in_progress", "pid": 44},
    })
    monkeypatch.setattr(pcon.os, "kill", _Kill(dead=[11, 33, 44]))

    assert pcon._reap_zombie_in_progress_stories() == 3

    assert _read(plans, "p1")["stories"]["a"] == {"status": "todo"}
    assert _read(plans, "p2")["stories"]["b"] == {
        "status": "in_progress", "pid": 22,
    }
    assert _read(plans, "p3")["stories"]["c"] == {"status": "todo"}
    assert _read(plans, "p3")["stories"]["d"] == {"status": "todo"}

    # Counting only what was actually written: nothing left to reap.
    assert pcon._reap_zombie_in_progress_stories() == 0
