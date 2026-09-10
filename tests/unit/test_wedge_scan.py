"""Tests for the wedge SCAN (gatherers + detection-only sweep) and its tick wiring.

Second wedge story, building on the pure ``wedge_verdict`` module (graded by
tests/unit/test_wedge_verdict.py). This file covers:

* ``pipeline.wedge_io._pid_is_alive``                - the liveness pattern copied from
  pipeline/story_status.py (os.kill(pid, 0) + ``ps -o stat=`` zombie check) and
  pipeline/concurrency.py's trust-alive-on-PermissionError slot accounting.
* ``pipeline.wedge_io.collect_story_wedge_signals``  - per-story pid/journal/agent.log
  measurements (journal path pattern identical to pipeline/store.py's journal
  helpers: ``PLAN_DIR / f"{plan_name}.{story_key}.journal.json"``). Already
  existed (dashboard-decoration story, 8b11b51c); this story adds the scan.
* ``pipeline.wedge_io.run_wedge_scan``               - the DETECTION-ONLY sweep that
  emits one warning notification per wedge reason, with a per-dedup-key cooldown.
* ``pipeline.advance._advance_pipeline_locked``   - the fail-open tick wiring
  (mirrors the triage-sweep precedent in tests/unit/test_acceptance_triage_sweep_wired.py).

Server-owned names (_store / PLAN_DIR / _notify_user) are resolved through the
``_ServerRef`` pattern, so every test monkeypatches the LIVE ``pipeline.server``
bindings (``monkeypatch.setattr(p, "NAME", ...)``) exactly as
pipeline/concurrency.py's module docstring describes. Thresholds are read from
``pipeline.config`` at CALL time, so tests monkeypatch the config module
attributes directly (a module-level ``from .config import WEDGE_*`` freeze would
fail these tests by design).

This file is intentionally RED until the wedge-scan story lands: the new names
below do not exist yet, so the failures must be AttributeErrors on those names.

Documented assumptions the implementer can rely on:

* ``run_wedge_scan`` returns the number of notifications it EMITTED (the brief
  pins ``return 0`` for the disabled / no-manifest early exits; emit-count is
  the only continuation consistent with those, and the cooldown tests below
  pin a suppressed scan returning 0).
* The cooldown state is the single module-level dict in pipeline.wedge mapping
  dedup_key -> last emit ``time.monotonic()``; tests locate it by type so the
  implementer is free to name it.
* The exact cooldown boundary (elapsed == WEDGE_NOTIFY_COOLDOWN_SECONDS) is
  deliberately NOT graded; tests pin strictly-inside (suppress) and
  strictly-past (re-emit) behaviour only.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import pipeline.server as p
from pipeline import advance as advance_mod
from pipeline import concurrency as pcon
from pipeline import (
    config,
    wedge_io,  # the I/O gatherers + the scan (this story's new names)
)

PLAN = "wp"
KEY = "s1"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_wedge_module_state():
    """Clear the wedge_io module's cooldown state around every test.

    Located by type (dict-valued module globals) so the implementer may name
    the cooldown map anything. Targets wedge_io, not wedge -- the scan and its
    cooldown table live there (wedge.py stays the import-pure verdict module
    per its own committed contract, tests/unit/test_wedge_verdict.py). Dunder
    globals are skipped - ``__builtins__`` is a dict in a module's namespace,
    and clearing it would wipe the interpreter's builtins.
    """
    for key, value in vars(wedge_io).items():
        if not key.startswith("__") and isinstance(value, dict):
            value.clear()
    yield
    for key, value in vars(wedge_io).items():
        if not key.startswith("__") and isinstance(value, dict):
            value.clear()


def _patch_config(monkeypatch, enabled=1, stale=1800, cooldown=600):
    """Stub the three wedge knobs on pipeline.config (read at call time)."""
    monkeypatch.setattr(config, "WEDGE_SCAN_ENABLED", enabled)
    monkeypatch.setattr(config, "WEDGE_STALE_ACTIVITY_SECONDS", stale)
    monkeypatch.setattr(config, "WEDGE_NOTIFY_COOLDOWN_SECONDS", cooldown)


class _StubStore:
    """Minimal store stand-in exposing ONLY get_manifest_or_none.

    If the implementation reaches for ``get_manifest`` (or anything else) it
    gets an AttributeError and the test fails - which grades the documented
    ``get_manifest_or_none`` access path.
    """

    def __init__(self, manifest=None):
        self.manifest = manifest
        self.calls = []

    def get_manifest_or_none(self, plan_name):
        self.calls.append(plan_name)
        return self.manifest


def _install_store(monkeypatch, manifest):
    store = _StubStore(manifest)
    monkeypatch.setattr(p, "_store", store)
    return store


def _install_notify_spy(monkeypatch):
    """Replace pipeline.server._notify_user with a recording spy."""
    emitted = []

    def spy(*args, **kwargs):
        emitted.append((args, kwargs))

    monkeypatch.setattr(p, "_notify_user", spy)
    return emitted


def _story(**over):
    story = {"status": "in_progress", "pid": 12345}
    story.update(over)
    return story


def _age_file(path, age_seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]")
    mtime = time.time() - age_seconds
    os.utime(path, (mtime, mtime))
    return path


def _age_journal(plan_dir, plan, key, age_seconds):
    """Create the journal at the EXACT store.py path pattern with an old mtime."""
    return _age_file(plan_dir / f"{plan}.{key}.journal.json", age_seconds)


def _message_of(entry):
    args, kwargs = entry
    if len(args) >= 2:
        return args[1]
    return kwargs.get("message")


def _plan_of(entry):
    args, kwargs = entry
    return args[0] if args else kwargs.get("plan_name")


def _measured_staleness(message):
    """Pull the 'for <N>s' measured value out of a notification message."""
    match = re.search(r"for\s+(\d+(?:\.\d+)?)s", message)
    assert match, f"no measured staleness embedded in message: {message!r}"
    return float(match.group(1))


class _Clock:
    """Fake time.monotonic source the cooldown tests advance explicitly."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _patch_clock(monkeypatch, clock):
    monkeypatch.setattr(time, "monotonic", clock)
    # Defensive: if the implementation did `from time import monotonic`, the
    # module-global copy must be patched too or the fake clock never lands.
    wedge_monotonic = getattr(wedge_io, "monotonic", None)
    if wedge_monotonic is not None and not isinstance(wedge_monotonic, type(time)):
        monkeypatch.setattr(wedge_io, "monotonic", clock)


# --------------------------------------------------------------------------- #
# Part 1a: module structure - _ServerRef bindings, no config freeze
# --------------------------------------------------------------------------- #


def test_wedge_resolves_server_names_through_concurrency_serverref():
    """_notify_user/_store/PLAN_DIR are _ServerRef bindings imported from .concurrency.

    Targets pipeline.wedge_io, not pipeline.wedge: wedge.py stays the
    import-pure verdict module by committed contract (its own purity test,
    tests/unit/test_wedge_verdict.py, statically forbids os/subprocess/time
    imports there), so the scan and its server-owned name bindings live in
    wedge_io.py alongside the existing I/O gatherers instead.
    """
    assert wedge_io._ServerRef is pcon._ServerRef, (
        "pipeline.wedge_io must import _ServerRef from .concurrency, not redefine it"
    )
    for name in ("_notify_user", "_store", "PLAN_DIR"):
        binding = getattr(wedge_io, name)
        assert isinstance(binding, pcon._ServerRef), name

    source = Path(wedge_io.__file__).read_text()
    assert "class _ServerRef" not in source, "wedge_io.py must not redefine _ServerRef"
    assert "from .concurrency import" in source
    # Thresholds must be resolved at CALL time; a module-level config freeze
    # would make WEDGE_* untestable and ignore env changes.
    assert "from .config import" not in source, (
        "wedge_io.py must not freeze config constants at module load"
    )
    # advance.py must import the scan at module top (no import cycle:
    # wedge_io imports nothing from advance).
    advance_source = Path(advance_mod.__file__).read_text()
    assert "from .wedge_io import run_wedge_scan" in advance_source, (
        "advance.py must import run_wedge_scan from .wedge_io at module top"
    )

# --------------------------------------------------------------------------- #
# Part 1b: _pid_is_alive - the story_status.py / concurrency.py pattern
# --------------------------------------------------------------------------- #


def test_pid_is_alive_true_for_a_live_process():
    assert wedge_io._pid_is_alive(os.getpid()) is True


def test_pid_is_alive_false_for_a_reaped_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()  # reaped -> os.kill raises ProcessLookupError
    assert wedge_io._pid_is_alive(proc.pid) is False


def test_pid_is_alive_false_for_a_zombie():
    """Unreaped exited child: ps stat starts with Z -> False (story_status.py)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.time() + 5
        stat = ""
        while time.time() < deadline:
            out = subprocess.run(
                ["ps", "-p", str(proc.pid), "-o", "stat="],
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if out.startswith("Z"):
                stat = out
                break
            time.sleep(0.02)
        assert stat.startswith("Z"), f"child never showed as zombie (stat={stat!r})"
        assert wedge_io._pid_is_alive(proc.pid) is False
    finally:
        proc.wait()


def test_pid_is_alive_trusts_alive_on_permission_error(monkeypatch):
    """PermissionError -> True (trust-alive, per concurrency.py slot accounting)."""

    def _deny(pid, sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", _deny)
    assert wedge_io._pid_is_alive(12345) is True


def test_pid_is_alive_false_on_process_lookup_error(monkeypatch):
    def _gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", _gone)
    assert wedge_io._pid_is_alive(12345) is False


# --------------------------------------------------------------------------- #
# Part 1c: collect_story_wedge_signals
# --------------------------------------------------------------------------- #


def test_collect_signals_returns_the_two_documented_keys(plan_dir):
    """Master's collect_story_wedge_signals (dashboard-decoration story,
    8b11b51c, pipeline/wedge_io.py) returns pid_alive + activity_age_seconds
    + agent_done -- no "sources" key. (Originally asserted a third "sources"
    key, from this branch's own now-superseded inline duplicate of the
    gatherer; that duplicate is dropped in favor of the shared wedge_io.py
    implementation dashboard.py already depends on, so the test pins that
    implementation's return shape. The agent_done key is this story's
    deliberate, requested contract change: the collector now reports whether
    the worktree carries a .agent_done / .agent_done.consumed completion
    marker so the verdict can spare finished agents the dead_pid reason.)"""
    sig = wedge_io.collect_story_wedge_signals(PLAN, None, {"status": "in_progress"})
    assert set(sig) == {"pid_alive", "activity_age_seconds", "agent_done"}


def test_collect_signals_agent_done_true_when_marker_file_present(plan_dir):
    """An unconsumed .agent_done marker in the worktree -> agent_done True
    (pipeline/watchers.py's scan_done_markers renames it to .agent_done.consumed
    only after processing, so its presence still proves a legitimate finish)."""
    worktree = plan_dir / "wt_done"
    worktree.mkdir()
    (worktree / ".agent_done").write_text("{}")
    sig = wedge_io.collect_story_wedge_signals(
        PLAN, None, {"pid": 1, "worktree": str(worktree)}
    )
    assert sig["agent_done"] is True


def test_collect_signals_agent_done_true_when_consumed_marker_present(plan_dir):
    """An already-consumed .agent_done.consumed marker (processed on a prior
    tick, still sitting in the worktree) -> agent_done True as well."""
    worktree = plan_dir / "wt_done2"
    worktree.mkdir()
    (worktree / ".agent_done.consumed").write_text("{}")
    sig = wedge_io.collect_story_wedge_signals(
        PLAN, None, {"pid": 1, "worktree": str(worktree)}
    )
    assert sig["agent_done"] is True


def test_collect_signals_agent_done_false_when_no_marker(plan_dir):
    """A worktree with no completion marker -> agent_done False (the pid may
    genuinely have died mid-work; the verdict must stay free to wedge it)."""
    worktree = plan_dir / "wt_nodone"
    worktree.mkdir()
    sig = wedge_io.collect_story_wedge_signals(
        PLAN, None, {"pid": 1, "worktree": str(worktree)}
    )
    assert sig["agent_done"] is False


def test_scan_dead_pid_suppressed_when_agent_done_marker_present(plan_dir, monkeypatch):
    """End-to-end wiring (run_wedge_scan -> wedge_verdict): a dead pid whose
    worktree carries a .agent_done marker is NOT notified as wedged. Grades
    the scan's agent_done=signals.get("agent_done", False) pass-through: with
    the marker present the dead_pid reason must never fire, so the scan
    emits nothing (activity is fresh -- no journal, no agent.log -- so
    stale_activity cannot fire either)."""
    worktree = plan_dir / "wt_scan_done"
    worktree.mkdir()
    (worktree / ".agent_done").write_text("{}")
    story = _story(pid=12345, worktree=str(worktree))
    _, emitted = _wedge_setup(monkeypatch, plan_dir, story)
    monkeypatch.setattr(wedge_io, "_pid_is_alive", lambda pid: False)

    assert wedge_io.run_wedge_scan(PLAN) == 0
    assert emitted == []


def test_collect_signals_pid_alive_none_without_int_pid(plan_dir, monkeypatch):
    """No pid (or a non-int pid) -> pid_alive None, liveness never consulted."""
    calls = []
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: calls.append(pid) or True)

    no_pid = wedge_io.collect_story_wedge_signals(PLAN, None, {"status": "in_progress"})
    assert no_pid["pid_alive"] is None

    string_pid = wedge_io.collect_story_wedge_signals(
        PLAN, None, {"status": "in_progress", "pid": "12345"}
    )
    assert string_pid["pid_alive"] is None

    assert calls == [], "non-int / missing pid must not reach the liveness check"


def test_collect_signals_pid_alive_delegates_to_pid_is_alive(plan_dir, monkeypatch):
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: pid == 7)
    assert wedge_io.collect_story_wedge_signals(PLAN, None, {"pid": 7})["pid_alive"] is True
    assert wedge_io.collect_story_wedge_signals(PLAN, None, {"pid": 8})["pid_alive"] is False


def test_collect_signals_age_uses_newest_of_journal_and_agent_log(plan_dir):
    _age_journal(plan_dir, PLAN, KEY, 3000)
    worktree = plan_dir / "wt"
    worktree.mkdir()
    _age_file(worktree / "agent.log", 100)

    sig = wedge_io.collect_story_wedge_signals(PLAN, None, {"pid": 1, "worktree": str(worktree)})

    age = sig["activity_age_seconds"]
    assert age is not None
    assert 95 <= age <= 105, f"max (newest) mtime must win, got {age}"


def test_collect_signals_journal_only_age(plan_dir):
    _age_journal(plan_dir, PLAN, KEY, 3000)
    sig = wedge_io.collect_story_wedge_signals(PLAN, None, {"pid": 1})
    assert 2990 <= sig["activity_age_seconds"] <= 3010


def test_collect_signals_agent_log_only_age(plan_dir):
    worktree = plan_dir / "wt2"
    _age_file(worktree / "agent.log", 2500)
    sig = wedge_io.collect_story_wedge_signals(
        PLAN, None, {"pid": 1, "worktree": str(worktree)}
    )
    assert 2490 <= sig["activity_age_seconds"] <= 2510


def test_collect_signals_no_signals_returns_none_age(plan_dir):
    """Neither file exists -> None (fail open), and no raise."""
    sig = wedge_io.collect_story_wedge_signals(
        PLAN, None, {"worktree": str(plan_dir / "nope")}
    )
    assert sig["activity_age_seconds"] is None
    assert sig["pid_alive"] is None


def test_collect_signals_journal_path_pattern_is_exact(plan_dir):
    """Only PLAN_DIR / f"{plan}.{key}.journal.json" feeds the measurement."""
    _age_file(plan_dir / f"{KEY}.journal.json", 9000)
    _age_file(plan_dir / f"{PLAN}.{KEY}.journal.json.bak", 9000)
    sig = wedge_io.collect_story_wedge_signals(PLAN, None, {"pid": 1})
    assert sig["activity_age_seconds"] is None, "decoy paths must be ignored"

    _age_journal(plan_dir, PLAN, KEY, 3000)
    sig = wedge_io.collect_story_wedge_signals(PLAN, None, {"pid": 1})
    assert 2990 <= sig["activity_age_seconds"] <= 3010


def test_collect_signals_ignores_non_string_worktree(plan_dir):
    """story['worktree'] must be a str to count; a Path object is ignored."""
    _age_journal(plan_dir, PLAN, "s2", 3000)
    worktree = plan_dir / "wt3"
    _age_file(worktree / "agent.log", 10)  # fresh - would win if consulted

    sig = wedge_io.collect_story_wedge_signals(
        PLAN, None, {"pid": 1, "worktree": worktree}
    )

    assert 2990 <= sig["activity_age_seconds"] <= 3010


def test_collect_signals_broken_symlink_fail_open(plan_dir):
    """A stat that raises OSError is treated as missing; the other signal still counts."""
    _age_journal(plan_dir, PLAN, "s3", 3000)
    worktree = plan_dir / "wt4"
    worktree.mkdir()
    (worktree / "agent.log").symlink_to(plan_dir / "does-not-exist")

    sig = wedge_io.collect_story_wedge_signals(
        PLAN, None, {"pid": 1, "worktree": str(worktree)}
    )
    assert 2990 <= sig["activity_age_seconds"] <= 3010


def test_collect_signals_all_stats_failing_fail_open(plan_dir):
    broken = plan_dir / f"{PLAN}.s4.journal.json"
    broken.symlink_to(plan_dir / "missing-target")
    sig = wedge_io.collect_story_wedge_signals(PLAN, None, {"pid": 1})
    assert sig["activity_age_seconds"] is None


def test_collect_signals_future_mtime_negative_age_not_clamped(plan_dir):
    """Negative age (clock skew / future mtime) is reported as-is, never normalized."""
    journal = plan_dir / f"{PLAN}.s5.journal.json"
    _age_file(journal, -3600)  # mtime in the future
    sig = wedge_io.collect_story_wedge_signals(PLAN, None, {"pid": 1})
    assert sig["activity_age_seconds"] is not None
    assert sig["activity_age_seconds"] < 0


# --- run_wedge_scan tests (appended below) ---


# --------------------------------------------------------------------------- #
# Part 1d: run_wedge_scan - detection-only sweep
# --------------------------------------------------------------------------- #


def _manifest_with(story):
    return {"epics": {}, "stories": {KEY: story}}


def _wedge_setup(monkeypatch, plan_dir, story, stale=1800, cooldown=600, enabled=1):
    """Common rig: tmp PLAN_DIR, stub store, notify spy, config knobs."""
    _patch_config(monkeypatch, enabled=enabled, stale=stale, cooldown=cooldown)
    store = _install_store(monkeypatch, _manifest_with(story))
    emitted = _install_notify_spy(monkeypatch)
    return store, emitted


def test_scan_emits_one_notification_per_reason(plan_dir, monkeypatch):
    """Dead pid + stale activity -> exactly two notifications, one per reason."""
    _age_journal(plan_dir, PLAN, KEY, 3000)
    story = _story(pid=12345, worktree=str(plan_dir / "wt"))
    _, emitted = _wedge_setup(monkeypatch, plan_dir, story)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)

    count = wedge_io.run_wedge_scan(PLAN)

    assert count == 2
    assert len(emitted) == 2
    reasons = {e[1]["dedup_key"].rsplit(":", 1)[-1] for e in emitted}
    assert reasons == {"dead_pid", "stale_activity"}


def test_scan_notification_contract(plan_dir, monkeypatch):
    """Every emit carries plan_name, message, story_key, severity, event, dedup_key."""
    _age_journal(plan_dir, PLAN, KEY, 3000)
    story = _story(pid=12345)
    _, emitted = _wedge_setup(monkeypatch, plan_dir, story)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)

    wedge_io.run_wedge_scan(PLAN)

    assert len(emitted) == 1
    (_, kwargs) = emitted[0]
    assert _plan_of(emitted[0]) == PLAN
    assert _message_of(emitted[0])
    assert kwargs["story_key"] == KEY
    assert kwargs["severity"] == "warning"
    assert kwargs["event"] == "wedge"
    assert kwargs["dedup_key"] == f"wedge:{PLAN}:{KEY}:stale_activity"


def test_scan_dead_pid_message_and_dedup_key(plan_dir, monkeypatch):
    story = _story(pid=12345)
    _, emitted = _wedge_setup(monkeypatch, plan_dir, story)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)

    wedge_io.run_wedge_scan(PLAN)

    (_, kwargs) = emitted[0]
    assert kwargs["dedup_key"] == f"wedge:{PLAN}:{KEY}:dead_pid"
    message = _message_of(emitted[0])
    assert "dead_pid" in message
    assert "12345" in message, "the dead pid must appear in the message"


def test_scan_stale_message_embeds_measured_value_and_threshold(plan_dir, monkeypatch):
    _age_journal(plan_dir, PLAN, KEY, 2731)
    story = _story(pid=12345)
    _, emitted = _wedge_setup(monkeypatch, plan_dir, story, stale=1800)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: True)

    wedge_io.run_wedge_scan(PLAN)

    message = _message_of(emitted[0])
    assert "stale_activity" in message
    assert "1800" in message, f"threshold must appear in message: {message!r}"
    measured = _measured_staleness(message)
    assert 2720 <= measured <= 2742, f"measured staleness wrong: {measured}"


def test_scan_return_value_is_the_emit_count(plan_dir, monkeypatch):
    """No-worktree story: dead_pid collapses into stale_activity, so exactly
    one notification is actually emitted this pass -- matching this test's
    own name and test_scan_notification_contract's identical setup, which
    pins the same single emit. (Originally asserted `== 2`, pinning the
    pre-fix behavior where the collapse's clock-tie comparison failed to
    suppress the duplicate and both reasons emitted -- see pipeline/wedge_io.py's
    run_wedge_scan docstring and the duplicate-emit bugfix.)"""
    _age_journal(plan_dir, PLAN, KEY, 3000)
    story = _story(pid=12345)
    _wedge_setup(monkeypatch, plan_dir, story)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)

    assert wedge_io.run_wedge_scan(PLAN) == 1


def test_scan_healthy_story_emits_nothing(plan_dir, monkeypatch):
    """Live pid + fresh activity -> no reasons, no notifications, return 0."""
    _age_journal(plan_dir, PLAN, KEY, 10)
    story = _story(pid=os.getpid(), worktree=str(plan_dir / "wt"))
    _, emitted = _wedge_setup(monkeypatch, plan_dir, story)

    assert wedge_io.run_wedge_scan(PLAN) == 0
    assert emitted == []


def test_scan_disabled_returns_zero_without_touching_store(plan_dir, monkeypatch):
    """WEDGE_SCAN_ENABLED falsy -> immediate 0; the manifest is never read."""
    store, emitted = _wedge_setup(
        monkeypatch, plan_dir, _story(pid=12345), enabled=0
    )

    assert wedge_io.run_wedge_scan(PLAN) == 0
    assert emitted == []
    assert store.calls == [], "disabled scan must exit before reading the manifest"


def test_scan_enabled_zero_is_falsy_but_one_is_truthy(plan_dir, monkeypatch):
    """Boundary: enabled=0 suppresses; enabled=1 with a wedged story emits."""
    story = _story(pid=12345)
    _wedge_setup(monkeypatch, plan_dir, story, enabled=0)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)
    assert wedge_io.run_wedge_scan(PLAN) == 0

    _patch_config(monkeypatch, enabled=1)
    assert wedge_io.run_wedge_scan(PLAN) == 1


def test_scan_no_manifest_returns_zero(plan_dir, monkeypatch):
    store, emitted = _wedge_setup(monkeypatch, plan_dir, _story())
    store.manifest = None

    assert wedge_io.run_wedge_scan(PLAN) == 0
    assert emitted == []


def test_scan_empty_manifest_returns_zero(plan_dir, monkeypatch):
    store, emitted = _wedge_setup(monkeypatch, plan_dir, _story())
    store.manifest = {"epics": {}, "stories": {}}

    assert wedge_io.run_wedge_scan(PLAN) == 0
    assert emitted == []


def test_scan_skips_non_in_progress_stories(plan_dir, monkeypatch):
    """Only status == 'in_progress' stories are scanned."""
    story = _story(pid=12345, status="done")
    _, emitted = _wedge_setup(monkeypatch, plan_dir, story)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)

    assert wedge_io.run_wedge_scan(PLAN) == 0
    assert emitted == []


def test_scan_story_missing_status_key_is_skipped(plan_dir, monkeypatch):
    """Malformed story (no status field at all) -> skipped, no raise."""
    story = {"pid": 12345}
    _, emitted = _wedge_setup(monkeypatch, plan_dir, story)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)

    assert wedge_io.run_wedge_scan(PLAN) == 0
    assert emitted == []


def test_scan_story_without_pid_is_not_wedged_by_liveness(plan_dir, monkeypatch):
    """No pid -> pid_alive None -> never a dead_pid reason; no raise.

    Journal age fixed from 99999s to 10s (well under the 1800s default
    stale threshold): this test isolates the dead_pid/no-pid behavior its
    own docstring names, and a 99999s-old journal independently triggers
    the UNRELATED stale_activity reason regardless of pid, which the
    original setup didn't account for -- the docstring's "0 emits" claim
    only holds once activity itself is fresh.
    """
    _age_journal(plan_dir, PLAN, KEY, 10)
    story = _story(pid=None)
    del story["pid"]
    _, emitted = _wedge_setup(monkeypatch, plan_dir, story)

    assert wedge_io.run_wedge_scan(PLAN) == 0
    assert emitted == []


def test_scan_missing_worktree_and_journal_no_emit_no_raise(plan_dir, monkeypatch):
    """No activity signals at all -> fail open -> no stale_activity reason."""
    story = _story(pid=12345, worktree=str(plan_dir / "gone"))
    _, emitted = _wedge_setup(monkeypatch, plan_dir, story)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: True)

    assert wedge_io.run_wedge_scan(PLAN) == 0
    assert emitted == []


def test_scan_threshold_boundary_equal_age_is_not_wedged(plan_dir, monkeypatch):
    """age just under the threshold is NOT wedged (strictly-greater contract).

    Aged 5s under the threshold rather than exactly at it: the measured age
    grows by the few ms between os.utime and the scan's stat, so an
    exactly-at-threshold fixture would flake above the line.
    """
    _age_journal(plan_dir, PLAN, KEY, 1795)
    story = _story(pid=12345)
    _wedge_setup(monkeypatch, plan_dir, story, stale=1800)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: True)

    assert wedge_io.run_wedge_scan(PLAN) == 0


def test_scan_multiple_stories_each_emit(plan_dir, monkeypatch):
    """Two wedged stories -> two notifications with distinct dedup keys."""
    _age_journal(plan_dir, PLAN, "a", 3000)
    _age_journal(plan_dir, PLAN, "b", 4000)
    manifest = {
        "epics": {},
        "stories": {
            "a": _story(pid=1),
            "b": _story(pid=2),
        },
    }
    _patch_config(monkeypatch)
    _install_store(monkeypatch, manifest)
    emitted = _install_notify_spy(monkeypatch)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)

    assert wedge_io.run_wedge_scan(PLAN) == 2
    keys = {e[1]["dedup_key"] for e in emitted}
    # Both stories are worktree-less with both reasons wedged, so each one's
    # dead_pid collapses into its own stale_activity alert (the survivor is
    # stale_activity, not dead_pid -- see test_scan_notification_contract's
    # identical single-story case, and the collapse rule in
    # pipeline/wedge_io.py's run_wedge_scan). Originally asserted dead_pid as
    # the survivor, pinning the pre-fix behavior where the collapse's
    # clock-tie comparison never suppressed anything and both reasons raced
    # to emit for each story.
    assert keys == {f"wedge:{PLAN}:a:stale_activity", f"wedge:{PLAN}:b:stale_activity"}


def test_scan_uses_get_manifest_or_none_on_the_server_store(plan_dir, monkeypatch):
    """The manifest comes from _store.get_manifest_or_none (ServerRef target)."""
    story = _story(pid=12345)
    store, _ = _wedge_setup(monkeypatch, plan_dir, story)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)

    wedge_io.run_wedge_scan(PLAN)

    assert store.calls == [PLAN]


def test_scan_reads_config_at_call_time(plan_dir, monkeypatch):
    """Thresholds come from pipeline.config attributes resolved during the call."""
    _age_journal(plan_dir, PLAN, KEY, 500)
    story = _story(pid=12345)
    _wedge_setup(monkeypatch, plan_dir, story, stale=1800)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: True)

    assert wedge_io.run_wedge_scan(PLAN) == 0  # 500s < 1800s: fresh

    _patch_config(monkeypatch, stale=100)
    assert wedge_io.run_wedge_scan(PLAN) == 1  # same call, new threshold applies


def test_scan_does_not_write_manifest_or_journal(plan_dir, monkeypatch):
    """DETECTION ONLY: a scan that flags a story leaves every file byte-identical."""
    journal = _age_journal(plan_dir, PLAN, KEY, 3000)
    manifest_path = plan_dir / f"{PLAN}.manifest.json"
    manifest_path.write_text('{"epics": {}, "stories": {}}')
    before_manifest = manifest_path.read_bytes()
    before_journal = journal.read_bytes()
    before_journal_stat = journal.stat()
    story = _story(pid=12345)
    _wedge_setup(monkeypatch, plan_dir, story)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)

    wedge_io.run_wedge_scan(PLAN)

    assert manifest_path.read_bytes() == before_manifest
    assert journal.read_bytes() == before_journal
    assert journal.stat().st_mtime_ns == before_journal_stat.st_mtime_ns


def test_scan_detection_only_no_reap_interrupt_or_status_write(plan_dir, monkeypatch):
    """The scan must not reap, interrupt, terminate, re-dispatch or set status."""
    story = _story(pid=12345)
    _wedge_setup(monkeypatch, plan_dir, story)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)

    wedge_io.run_wedge_scan(PLAN)

    assert story["status"] == "in_progress", "status must be untouched"
    assert "interrupted_at" not in story
    assert "reaped_at" not in story

    source = Path(wedge_io.__file__).read_text()
    assert "DETECTION ONLY" in source or "DETECTION-ONLY" in source, (
        "run_wedge_scan must carry the explicit detection-only comment"
    )
    # _pid_is_alive legitimately uses os.kill(pid, 0) (the story_status.py
    # liveness probe), so os.kill itself is NOT banned - only the recovery
    # actions the scan must never take.
    for banned in (
        "os.killpg(",
        ".terminate(",
        ".interrupt(",
        "_reap_zombie",
        "interrupt_story",
        "dispatch_story",
        "SIGKILL",
        "SIGTERM",
        "SIGSTOP",
    ):
        assert banned not in source, f"detection-only scan must not {banned!r}"


# --------------------------------------------------------------------------- #
# Part 1e: cooldown (anti cry-wolf)
# --------------------------------------------------------------------------- #


def test_cooldown_suppresses_second_emit_in_the_same_window(plan_dir, monkeypatch):
    story = _story(pid=12345)
    _, emitted = _wedge_setup(monkeypatch, plan_dir, story, cooldown=600)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)
    clock = _Clock()
    _patch_clock(monkeypatch, clock)

    assert wedge_io.run_wedge_scan(PLAN) == 1
    assert wedge_io.run_wedge_scan(PLAN) == 0  # suppressed
    assert len(emitted) == 1


def test_cooldown_reemits_after_the_window_passes(plan_dir, monkeypatch):
    story = _story(pid=12345)
    _, emitted = _wedge_setup(monkeypatch, plan_dir, story, cooldown=600)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)
    clock = _Clock()
    _patch_clock(monkeypatch, clock)

    assert wedge_io.run_wedge_scan(PLAN) == 1
    clock.advance(601)
    assert wedge_io.run_wedge_scan(PLAN) == 1
    assert len(emitted) == 2


def test_cooldown_keys_are_independent_per_reason(plan_dir, monkeypatch):
    """dead_pid and stale_activity collapse separately: one emit each per window.

    Justification for the pass-2 assertion below (originally `== 0`, i.e.
    "both reasons now cooling down"): this story has no worktree, so pass 1
    collapses dead_pid into the stale_activity alert -- exactly one
    notification goes out (stale_activity's), and per run_wedge_scan's own
    contract the collapsed dead_pid reason is deliberately NOT written into
    the cooldown table (only a successful, actually-sent notify is). The
    original `== 0` assumed dead_pid's cooldown got recorded anyway, which
    would make it silent forever under a frozen clock with no way to ever
    fire dead_pid on its own -- contradicted by both the module's docstring
    ("a later scan... re-derives the dead pid") and by
    test_scan_notification_contract, which pins pass 1 to exactly one emit
    for this identical no-worktree setup. With dead_pid never entered into
    the cooldown table, pass 2 correctly re-derives it as due (1, not 0):
    stale_activity is now the one cooling down (recorded in pass 1), so
    dead_pid is no longer collapsed and fires on its own. Total emits across
    both passes is still 2 -- one per reason, one per window, as the
    docstring says -- just split across passes instead of both landing in
    pass 1.
    """
    _age_journal(plan_dir, PLAN, KEY, 3000)
    story = _story(pid=12345)
    _, emitted = _wedge_setup(monkeypatch, plan_dir, story, cooldown=600)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)
    clock = _Clock()
    _patch_clock(monkeypatch, clock)

    # Pass 1: run_wedge_scan's return value is notifications actually
    # EMITTED (per this file's own "Documented assumptions" header) --
    # dead_pid collapses into the stale_activity alert this pass, so only
    # stale_activity emits.
    assert wedge_io.run_wedge_scan(PLAN) == 1
    # Pass 2: stale_activity is now cooling down (recorded in pass 1) so it
    # isn't due; dead_pid was never recorded (a collapsed reason is
    # deliberately not entered into the cooldown table), so it re-derives
    # and emits on its own.
    assert wedge_io.run_wedge_scan(PLAN) == 1
    assert len(emitted) == 2
    reasons = {e[1]["dedup_key"].rsplit(":", 1)[-1] for e in emitted}
    assert reasons == {"dead_pid", "stale_activity"}


def test_cooldown_keys_are_independent_per_story(plan_dir, monkeypatch):
    """Each story's dead_pid/stale_activity collapse and re-derive on their
    own schedule, independent of the other story.

    Justification for the pass-2 assertion (originally `== 0`, "both stories
    fully cooling down"): both stories are worktree-less with both reasons
    wedged, so pass 1 collapses each story's dead_pid into its own
    stale_activity alert (2 emits total, one per story -- see
    test_scan_multiple_stories_each_emit). A collapsed reason is
    deliberately not entered into the cooldown table (see run_wedge_scan's
    docstring), so on pass 2 each story's dead_pid re-derives and fires on
    its own -- 2 more emits, not 0. Total across both passes is 4.
    """
    _age_journal(plan_dir, PLAN, "a", 3000)
    _age_journal(plan_dir, PLAN, "b", 3000)
    manifest = {"epics": {}, "stories": {"a": _story(pid=1), "b": _story(pid=2)}}
    _patch_config(monkeypatch, cooldown=600)
    _install_store(monkeypatch, manifest)
    emitted = _install_notify_spy(monkeypatch)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)
    clock = _Clock()
    _patch_clock(monkeypatch, clock)

    assert wedge_io.run_wedge_scan(PLAN) == 2
    assert wedge_io.run_wedge_scan(PLAN) == 2
    assert len(emitted) == 4


def test_cooldown_table_is_capped(plan_dir, monkeypatch):
    """The cooldown dict must not grow unbounded: >1000 entries get pruned."""
    story = _story(pid=12345)
    _wedge_setup(monkeypatch, plan_dir, story, cooldown=600)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)
    clock = _Clock()
    _patch_clock(monkeypatch, clock)

    # Stuff EVERY dict-valued module global with stale entries: whichever one
    # is the cooldown table, it must be pruned once it exceeds 1000 keys.
    # Dunders skipped: __builtins__ is a dict in a module's own namespace and
    # is not the cooldown table (same filter as _reset_wedge_module_state).
    tables = [
        v for k, v in vars(wedge_io).items()
        if not k.startswith("__") and isinstance(v, dict)
    ]
    assert tables, "wedge_io module must keep its cooldown state in a module dict"
    for table in tables:
        table.update({f"wedge:oldplan:s{i}:dead_pid": 1.0 for i in range(1500)})

    wedge_io.run_wedge_scan(PLAN)

    for table in tables:
        assert len(table) <= 1001, "cooldown table must be pruned once it exceeds 1000"


def test_cooldown_uses_monotonic_not_wall_clock(plan_dir, monkeypatch):
    """Emit times are recorded from time.monotonic (immune to wall-clock jumps)."""
    story = _story(pid=12345)
    _wedge_setup(monkeypatch, plan_dir, story, cooldown=600)
    monkeypatch.setattr(wedge_io, "_pid_is_alive",lambda pid: False)
    clock = _Clock()
    _patch_clock(monkeypatch, clock)

    wedge_io.run_wedge_scan(PLAN)

    tables = [
        v for k, v in vars(wedge_io).items()
        if not k.startswith("__") and isinstance(v, dict)
    ]
    recorded = [t for t in tables if t]
    assert recorded, "an emit must record its time in the cooldown table"
    for table in recorded:
        for value in table.values():
            assert value == clock.now, (
                "cooldown timestamps must come from time.monotonic()"
            )


# --------------------------------------------------------------------------- #
# Part 2: tick wiring in pipeline/advance.py
# --------------------------------------------------------------------------- #


def _patch_tick(monkeypatch, result=None):
    """Stub the tick body via the _impl_ref target (pipeline.server).

    Also stubs the triage sweep so the real sweep never runs against the tmp
    manifest (the wiring tests grade the WEDGE insertion, not triage).
    """
    calls = []
    monkeypatch.setattr(
        p,
        "_advance_pipeline_locked_impl",
        lambda plan_name: calls.append(plan_name) or (result if result else {"ok": True}),
    )
    monkeypatch.setattr(p, "run_triage_sweep", lambda plan_name: None)
    return calls


def _patch_scan(monkeypatch, fn):
    """Patch run_wedge_scan on BOTH resolution paths the implementation may use.

    advance.py imports it at module top (``from .wedge import run_wedge_scan``),
    so the bare name inside _advance_pipeline_locked resolves from
    pipeline.advance's globals; patching there always lands. A defensive second
    patch on pipeline.server covers a ServerRef-style resolution too.
    """
    monkeypatch.setattr(advance_mod, "run_wedge_scan", fn)
    monkeypatch.setattr(p, "run_wedge_scan", fn, raising=False)


def test_wiring_scan_runs_on_the_tick_path(plan_dir, monkeypatch):
    seen = []
    _patch_tick(monkeypatch)
    _patch_scan(monkeypatch, lambda plan_name: seen.append(plan_name))

    advance_mod._advance_pipeline_locked(PLAN)

    assert seen == [PLAN], "run_wedge_scan must be called by the tick wrapper"


def test_wiring_scan_runs_before_the_tick_body(plan_dir, monkeypatch):
    order = []

    def fake_tick(plan_name):
        order.append("tick")
        return {"ok": True}

    _patch_tick(monkeypatch)
    monkeypatch.setattr(p, "_advance_pipeline_locked_impl", fake_tick)
    _patch_scan(monkeypatch, lambda plan_name: order.append("scan"))

    advance_mod._advance_pipeline_locked(PLAN)

    assert order == ["scan", "tick"]


def test_wiring_scan_raising_is_fail_open_and_tick_result_survives(plan_dir, monkeypatch, caplog):
    def boom(plan_name):
        seen.append(plan_name)
        raise RuntimeError("wedge scan exploded")

    seen = []
    tick_calls = _patch_tick(monkeypatch, result={"ok": True, "stories": 3})
    _patch_scan(monkeypatch, boom)

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        result = advance_mod._advance_pipeline_locked(PLAN)

    assert seen == [PLAN], "the scan must run on the tick path (wiring graded here)"
    assert tick_calls == [PLAN]
    assert result == {"ok": True, "stories": 3}, "tick result must come back unchanged"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a raising scan must log a warning, not propagate"
    assert all(r.name == "pipeline" for r in warnings)
    assert any("wedge" in r.getMessage().lower() for r in warnings)


def test_wiring_triage_sweep_still_runs_alongside_the_scan(plan_dir, monkeypatch):
    order = []
    _patch_tick(monkeypatch)
    _patch_scan(monkeypatch, lambda plan_name: order.append("scan"))
    monkeypatch.setattr(p, "run_triage_sweep", lambda plan_name: order.append("triage"))

    advance_mod._advance_pipeline_locked(PLAN)

    assert sorted(order) == ["scan", "triage"]


def test_wiring_wrapper_source_contains_the_fail_open_block():
    """The wrapper carries an identical fail-open block for the wedge scan."""
    source = inspect.getsource(advance_mod._advance_pipeline_locked)
    assert "run_wedge_scan(plan_name)" in source
    assert "except Exception:" in source
    assert "logging.getLogger" in source
    assert "wedge scan raised" in source
    # The insertion must sit in the WRAPPER, not inside the impl.
    impl_source = inspect.getsource(advance_mod._advance_pipeline_locked_impl)
    assert "run_wedge_scan" not in impl_source