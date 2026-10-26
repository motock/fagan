"""Tests for the stall scan (UC-04): a ready story that sits undispatched.

``pipeline.stall_scan.run_stall_scan`` is DETECTION ONLY, mirroring
``pipeline.wedge_io.run_wedge_scan``: it measures and notifies, never writes a
status, never interrupts, never re-dispatches. Server-owned names resolve
through the live ``pipeline.server`` bindings (the ``_ServerRef`` pattern), so
every test monkeypatches those, and the threshold is read at CALL time.

This file grades only what UC-04 adds: the scan, its cooldown, its skip rules,
the gate reason it reports, and the fail-open tick wiring in
``pipeline.advance._advance_pipeline_locked``.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

import pytest

import pipeline.server as p
from pipeline import advance as advance_mod
from pipeline import stall_scan

PLAN = "sp"
KEY = "s1"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_cooldown():
    """Keep the module-level cooldown table from leaking between tests."""
    stall_scan._STALL_LAST_EMIT.clear()
    yield
    stall_scan._STALL_LAST_EMIT.clear()


def _iso_age(seconds: float) -> str:
    """An ISO-8601 UTC timestamp ``seconds`` in the past."""
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _story(**over):
    story = {"status": "interrupted", "summary": "a story"}
    story.update(over)
    return story


def _manifest_with(*stories):
    return {"stories": {key: story for key, story in stories}}


class _StubStore:
    """Minimal store stand-in exposing ONLY get_manifest_or_none."""

    def __init__(self, manifest=None):
        self.manifest = manifest
        self.calls = []

    def get_manifest_or_none(self, plan_name):
        self.calls.append(plan_name)
        return self.manifest


class _StubBackend:
    """Stand-in for the ``pipeline.server.backend`` module."""

    def __init__(self, status=None, raises=None):
        self.status = status if status is not None else {"ok": True, "reason": ""}
        self.raises = raises
        self.calls = []

    def get_backend(self, role, name=None):
        self.calls.append((role, name))
        if self.raises is not None:
            raise self.raises
        status = self.status

        class _Driver:
            def resource_status(self, model_tag=None):
                return status

        return _Driver()


def _install_notify_spy(monkeypatch):
    emitted = []

    def spy(*args, **kwargs):
        emitted.append((args, kwargs))

    monkeypatch.setattr(p, "_notify_user", spy)
    return emitted


def _setup(monkeypatch, manifest, threshold=1800, enabled=1, backend=None):
    """Common rig: stub store, notify spy, backend, env knobs.

    The threshold and the kill switch are read from the environment at CALL
    time, so stubbing the environment is enough -- no module reload.
    """
    monkeypatch.setenv("PIPELINE_STALL_SECONDS", str(threshold))
    monkeypatch.setenv("PIPELINE_STALL_SCAN_ENABLED", str(enabled))
    monkeypatch.setattr(p, "_store", _StubStore(manifest))
    monkeypatch.setattr(p, "backend", backend if backend is not None else _StubBackend())
    return _install_notify_spy(monkeypatch)


def _message_of(entry):
    args, kwargs = entry
    if len(args) >= 2:
        return args[1]
    return kwargs.get("message")


class _Clock:
    """Fake ``time.monotonic`` source the cooldown tests advance explicitly."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _patch_clock(monkeypatch, clock):
    monkeypatch.setattr(time, "monotonic", clock)


# --------------------------------------------------------------------------- #
# Part 1: the scan
# --------------------------------------------------------------------------- #


def test_interrupted_older_than_threshold_emits_once_with_measured_age(
    plan_dir, monkeypatch
):
    """The message carries the MEASURED age next to the threshold, plus the
    story's status, so a mis-thresholded detector is diagnosable from its own
    output."""
    story = _story(status="interrupted", interrupted_at=_iso_age(3000))
    emitted = _setup(monkeypatch, _manifest_with((KEY, story)), threshold=1800)

    count = stall_scan.run_stall_scan(PLAN)

    assert count == 1
    assert len(emitted) == 1
    message = _message_of(emitted[0])
    assert "interrupted" in message
    assert "3000" in message, f"measured age missing from {message!r}"
    assert "1800" in message, f"threshold missing from {message!r}"
    assert emitted[0][1]["event"] == "story_stalled"


def test_younger_than_threshold_does_not_emit(plan_dir, monkeypatch):
    story = _story(status="interrupted", interrupted_at=_iso_age(60))
    emitted = _setup(monkeypatch, _manifest_with((KEY, story)), threshold=1800)

    assert stall_scan.run_stall_scan(PLAN) == 0
    assert emitted == []


def test_repeat_scan_within_cooldown_emits_nothing(plan_dir, monkeypatch):
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    story = _story(status="interrupted", interrupted_at=_iso_age(3000))
    emitted = _setup(monkeypatch, _manifest_with((KEY, story)), threshold=1800)

    assert stall_scan.run_stall_scan(PLAN) == 1
    clock.advance(1799)
    assert stall_scan.run_stall_scan(PLAN) == 0
    assert len(emitted) == 1


def test_story_with_undone_dependency_is_skipped(plan_dir, monkeypatch):
    """A story whose dependency is not done is not dispatch-eligible, so it is
    not stalled -- it is simply waiting."""
    dep = _story(status="in_progress", interrupted_at=_iso_age(5000))
    story = _story(
        status="todo", dependencies=["dep"], interrupted_at=_iso_age(5000)
    )
    emitted = _setup(
        monkeypatch, _manifest_with(("dep", dep), (KEY, story)), threshold=1800
    )

    assert stall_scan.run_stall_scan(PLAN) == 0
    assert emitted == []


def test_story_with_done_dependency_is_eligible(plan_dir, monkeypatch):
    dep = _story(status="done")
    story = _story(
        status="todo", dependencies=["dep"], interrupted_at=_iso_age(5000)
    )
    emitted = _setup(
        monkeypatch, _manifest_with(("dep", dep), (KEY, story)), threshold=1800
    )

    assert stall_scan.run_stall_scan(PLAN) == 1
    assert len(emitted) == 1


def test_story_without_any_timestamp_signal_is_skipped(plan_dir, monkeypatch):
    """No interrupted_at and no readable journal/agent.log -> fail open, skip."""
    story = _story(status="todo")
    emitted = _setup(monkeypatch, _manifest_with((KEY, story)), threshold=1800)

    assert stall_scan.run_stall_scan(PLAN) == 0
    assert emitted == []


def test_fresh_agent_log_outranks_an_old_journal(plan_dir, monkeypatch):
    """The newest of journal / agent.log mtime wins, so a live worktree is not
    reported as stalled."""
    journal = plan_dir / f"{PLAN}.{KEY}.journal.json"
    journal.write_text("[]")
    old = time.time() - 5000
    import os

    os.utime(journal, (old, old))
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text("working\n")
    story = _story(status="interrupted", worktree=str(worktree))
    emitted = _setup(monkeypatch, _manifest_with((KEY, story)), threshold=1800)

    assert stall_scan.run_stall_scan(PLAN) == 0
    assert emitted == []


def test_interrupted_at_takes_precedence_over_an_old_journal(plan_dir, monkeypatch):
    journal = plan_dir / f"{PLAN}.{KEY}.journal.json"
    journal.write_text("[]")
    old = time.time() - 5000
    import os

    os.utime(journal, (old, old))
    story = _story(status="interrupted", interrupted_at=_iso_age(10))
    emitted = _setup(monkeypatch, _manifest_with((KEY, story)), threshold=1800)

    assert stall_scan.run_stall_scan(PLAN) == 0
    assert emitted == []


def test_gate_reason_names_the_usage_gate(plan_dir, monkeypatch):
    """When the claude backend reports a usage pause, the notification says so
    instead of guessing."""
    backend = _StubBackend(status={"ok": False, "reason": "Claude usage gate tripped"})
    story = _story(status="interrupted", interrupted_at=_iso_age(3000))
    emitted = _setup(
        monkeypatch, _manifest_with((KEY, story)), threshold=1800, backend=backend
    )

    assert stall_scan.run_stall_scan(PLAN) == 1
    message = _message_of(emitted[0]).lower()
    assert "usage gate" in message, f"gate reason missing from {message!r}"


def test_gate_reason_names_the_usage_cooldown(plan_dir, monkeypatch):
    backend = _StubBackend(
        status={"ok": False, "reason": "Claude usage cooldown active"}
    )
    story = _story(status="interrupted", interrupted_at=_iso_age(3000))
    emitted = _setup(
        monkeypatch, _manifest_with((KEY, story)), threshold=1800, backend=backend
    )

    assert stall_scan.run_stall_scan(PLAN) == 1
    assert "usage cooldown" in _message_of(emitted[0]).lower()


def test_gate_reason_names_memory_pressure(plan_dir, monkeypatch):
    backend = _StubBackend(
        status={"ok": False, "reason": "insufficient free memory for model"}
    )
    story = _story(status="interrupted", interrupted_at=_iso_age(3000))
    emitted = _setup(
        monkeypatch, _manifest_with((KEY, story)), threshold=1800, backend=backend
    )

    assert stall_scan.run_stall_scan(PLAN) == 1
    assert "memory" in _message_of(emitted[0]).lower()


def test_raising_backend_lookup_still_emits_with_reason_unknown(plan_dir, monkeypatch):
    backend = _StubBackend(raises=RuntimeError("backend registry exploded"))
    story = _story(status="interrupted", interrupted_at=_iso_age(3000))
    emitted = _setup(
        monkeypatch, _manifest_with((KEY, story)), threshold=1800, backend=backend
    )

    assert stall_scan.run_stall_scan(PLAN) == 1
    assert "unknown" in _message_of(emitted[0]).lower()


def test_scan_never_raises_on_a_broken_manifest(plan_dir, monkeypatch):
    """A manifest whose stories are not dicts must not take the tick down."""
    monkeypatch.setenv("PIPELINE_STALL_SECONDS", "1800")
    monkeypatch.setenv("PIPELINE_STALL_SCAN_ENABLED", "1")
    monkeypatch.setattr(p, "_store", _StubStore({"stories": {KEY: "not-a-dict"}}))
    _install_notify_spy(monkeypatch)

    assert stall_scan.run_stall_scan(PLAN) == 0


def test_disabled_scan_emits_nothing(plan_dir, monkeypatch):
    story = _story(status="interrupted", interrupted_at=_iso_age(3000))
    emitted = _setup(
        monkeypatch, _manifest_with((KEY, story)), threshold=1800, enabled=0
    )

    assert stall_scan.run_stall_scan(PLAN) == 0
    assert emitted == []


# --------------------------------------------------------------------------- #
# Part 2: tick wiring in pipeline/advance.py
# --------------------------------------------------------------------------- #


def _patch_tick(monkeypatch, result=None):
    calls = []
    monkeypatch.setattr(
        p,
        "_advance_pipeline_locked_impl",
        lambda plan_name: calls.append(plan_name) or (result if result else {"ok": True}),
    )
    monkeypatch.setattr(p, "run_triage_sweep", lambda plan_name: None)
    monkeypatch.setattr(advance_mod, "run_wedge_scan", lambda plan_name: 0)
    return calls


def test_wiring_stall_scan_runs_on_the_tick_path(plan_dir, monkeypatch):
    seen = []
    _patch_tick(monkeypatch)
    monkeypatch.setattr(advance_mod, "run_stall_scan", lambda plan_name: seen.append(plan_name))

    advance_mod._advance_pipeline_locked(PLAN)

    assert seen == [PLAN], "run_stall_scan must be called by the tick wrapper"


def test_wiring_stall_scan_raising_is_fail_open(plan_dir, monkeypatch, caplog):
    def boom(plan_name):
        raise RuntimeError("stall scan exploded")

    _patch_tick(monkeypatch, result={"ok": True, "stories": 3})
    monkeypatch.setattr(advance_mod, "run_stall_scan", boom)

    with caplog.at_level(logging.WARNING):
        result = advance_mod._advance_pipeline_locked(PLAN)

    assert result == {"ok": True, "stories": 3}
    assert any("stall" in record.message.lower() for record in caplog.records), (
        "the fail-open path must log a WARNING naming the stall scan"
    )
