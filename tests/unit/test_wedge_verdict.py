"""Tests for pipeline.wedge.wedge_verdict (written test-first for this story).

wedge_verdict is the PURE wedge detector for dispatched stories. Callers pass:

- pid_alive: three-valued liveness -- True (process alive), False (dead OR
  zombie; pipeline/story_status.py's os.kill(pid, 0) succeeds for defunct
  processes and only a `ps -o stat=` value starting with Z distinguishes
  them, so the caller collapses both into False), None (story has no pid /
  liveness unknown). None and True are never wedge reasons.
- activity_age_seconds: measured seconds since the newest worktree activity
  signal (journal mtime / agent.log mtime), or None when no signal exists.
  None means "cannot prove staleness" and is fail-open (same convention as
  pipeline/usage.py's _usage_state_age_seconds), never a reason on its own.
- stale_seconds: threshold resolved by the caller from pipeline.config
  (WEDGE_STALE_ACTIVITY_SECONDS).

The function must be pure: no clock reads, no environ reads, no filesystem /
subprocess probes anywhere in its call path. These tests enforce that both
statically (import scan of the module source) and behaviorally (clock/env/OS
surfaces are patched to raise before calling the function).

Contract under test:
- returns {"wedged": bool, "reasons": list[str], "measured": {...},
  "thresholds": {"stale_seconds": stale_seconds}};
- pid_alive False -> reason "dead_pid";
- activity_age_seconds > stale_seconds (strictly greater; equal is NOT wedged)
  -> reason "stale_activity"; negative age (future mtime / clock skew) is
  never a reason;
- both reasons can fire at once and "reasons" is sorted (deterministic);
- wedged is True iff reasons is non-empty;
- the measured reading travels with the verdict next to the threshold
  (.claude/rules/testing-config-gates.md gate-validation lesson: a
  mis-thresholded detector must be diagnosable from its own output).
"""

import ast
import builtins
import inspect
import os
import subprocess
import time
from typing import get_type_hints

import pytest
from pipeline.wedge import wedge_verdict

# The only module-level imports pipeline/wedge.py is allowed to make.
_ALLOWED_WEDGE_IMPORTS = frozenset({"__future__", "dataclasses", "typing"})

STALE = 1800


# ---------- purity: static import scan ----------


def _wedge_source() -> str:
    import pipeline.wedge as wedge_module

    return inspect.getsource(wedge_module)


def test_wedge_module_imports_nothing_beyond_typing_and_dataclasses():
    """No os/subprocess/time/datetime (or anything else) in wedge.py's imports.

    ast.walk covers imports nested inside functions too, so a lazy
    `import time` inside wedge_verdict's body is caught as well. Docstring
    mentions of those module names are irrelevant: only Import nodes count.
    """
    imported = set()
    for node in ast.walk(ast.parse(_wedge_source())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = imported - _ALLOWED_WEDGE_IMPORTS
    assert not forbidden, (
        "pipeline/wedge.py must stay pure (typing/dataclasses only); "
        f"forbidden imports: {sorted(forbidden)}"
    )


def test_wedge_module_does_not_bind_os_subprocess_time_or_datetime():
    import pipeline.wedge as wedge_module

    bound = set(vars(wedge_module))
    for forbidden in ("os", "subprocess", "time", "datetime"):
        assert forbidden not in bound, (
            f"pipeline/wedge.py binds {forbidden!r} at module level; the "
            "verdict function must receive measurements, not take them"
        )


# ---------- purity: behavioral (clock/env/OS patched to raise) ----------


def _raise_purity(*_args, **_kwargs):
    raise AssertionError(
        "wedge_verdict touched a clock/environment/OS surface; it must be pure"
    )


def test_wedge_verdict_does_not_touch_clock_env_or_os(monkeypatch):
    for name in (
        "time",
        "monotonic",
        "perf_counter",
        "time_ns",
        "monotonic_ns",
        "perf_counter_ns",
    ):
        monkeypatch.setattr(time, name, _raise_purity, raising=False)
    # datetime.datetime is an extension type (cannot setattr); its import is
    # banned by the AST scan above instead.
    monkeypatch.setattr(os.environ, "get", _raise_purity, raising=False)
    monkeypatch.setattr(os, "getenv", _raise_purity, raising=False)
    monkeypatch.setattr(os, "kill", _raise_purity, raising=False)
    monkeypatch.setattr(os, "stat", _raise_purity, raising=False)
    monkeypatch.setattr(os.path, "getmtime", _raise_purity, raising=False)
    monkeypatch.setattr(subprocess, "run", _raise_purity, raising=False)
    monkeypatch.setattr(subprocess, "Popen", _raise_purity, raising=False)
    monkeypatch.setattr(subprocess, "check_output", _raise_purity, raising=False)
    monkeypatch.setattr(builtins, "open", _raise_purity, raising=False)

    dead = wedge_verdict(False, 2731.2, 1800)
    assert dead["wedged"] is True
    assert dead["reasons"] == ["dead_pid", "stale_activity"]

    healthy = wedge_verdict(True, None, 1800)
    assert healthy["wedged"] is False
    assert healthy["reasons"] == []


# ---------- signature ----------


def test_wedge_verdict_signature_and_annotations():
    signature = inspect.signature(wedge_verdict)
    assert list(signature.parameters) == [
        "pid_alive",
        "activity_age_seconds",
        "stale_seconds",
    ]
    hints = get_type_hints(wedge_verdict)
    assert hints.get("pid_alive") == bool | None
    assert hints.get("activity_age_seconds") == float | None
    assert hints.get("stale_seconds") is int


# ---------- result shape ----------


def test_result_shape_keys_and_types():
    healthy = wedge_verdict(True, 12.0, 1800)
    assert set(healthy) == {"wedged", "reasons", "measured", "thresholds"}
    assert isinstance(healthy["wedged"], bool)
    assert isinstance(healthy["reasons"], list)
    assert isinstance(healthy["measured"], dict)
    assert isinstance(healthy["thresholds"], dict)
    assert healthy["thresholds"] == {"stale_seconds": 1800}

    wedged = wedge_verdict(False, 2731.2, 1800)
    assert set(wedged) == {"wedged", "reasons", "measured", "thresholds"}
    assert wedged["thresholds"] == {"stale_seconds": 1800}


def test_measured_reading_travels_with_verdict():
    """The measured reading sits next to the threshold in the output."""
    verdict = wedge_verdict(False, 2731.2, 1800)
    assert verdict["measured"]["activity_age_seconds"] == 2731.2
    assert verdict["measured"]["pid_alive"] is False
    assert verdict["thresholds"] == {"stale_seconds": 1800}

    # Even a "no signal" None measurement travels with the verdict.
    no_signal = wedge_verdict(True, None, 1800)
    assert no_signal["measured"]["activity_age_seconds"] is None
    assert no_signal["measured"]["pid_alive"] is True


# ---------- individual reasons ----------


def test_dead_pid_alone_wedges():
    verdict = wedge_verdict(False, None, 1800)
    assert verdict["wedged"] is True
    assert verdict["reasons"] == ["dead_pid"]
    assert verdict["measured"]["pid_alive"] is False
    assert verdict["measured"]["activity_age_seconds"] is None
    assert verdict["thresholds"] == {"stale_seconds": 1800}


def test_stale_activity_alone_wedges():
    verdict = wedge_verdict(True, 2731.2, 1800)
    assert verdict["wedged"] is True
    assert verdict["reasons"] == ["stale_activity"]
    assert verdict["measured"]["activity_age_seconds"] == 2731.2
    assert verdict["measured"]["pid_alive"] is True
    assert verdict["thresholds"] == {"stale_seconds": 1800}


def test_both_reasons_fire_sorted():
    verdict = wedge_verdict(False, 2731.2, 1800)
    assert verdict["wedged"] is True
    assert verdict["reasons"] == ["dead_pid", "stale_activity"]
    assert verdict["reasons"] == sorted(verdict["reasons"])


def test_output_deterministic_across_calls():
    first = wedge_verdict(False, 2731.2, 1800)
    second = wedge_verdict(False, 2731.2, 1800)
    assert first == second
    assert first["reasons"] == sorted(first["reasons"])


# ---------- boundaries ----------


def test_age_exactly_at_threshold_is_not_wedged():
    verdict = wedge_verdict(True, 1800.0, 1800)
    assert verdict["wedged"] is False
    assert verdict["reasons"] == []


def test_age_one_microsecond_over_threshold_is_wedged():
    verdict = wedge_verdict(True, 1800.0 + 1e-6, 1800)
    assert verdict["wedged"] is True
    assert verdict["reasons"] == ["stale_activity"]
    assert verdict["measured"]["activity_age_seconds"] == 1800.0 + 1e-6


def test_age_just_under_threshold_is_not_wedged():
    verdict = wedge_verdict(True, 1800.0 - 1e-6, 1800)
    assert verdict["wedged"] is False
    assert verdict["reasons"] == []


def test_zero_age_is_not_wedged():
    verdict = wedge_verdict(True, 0.0, 1800)
    assert verdict["wedged"] is False
    assert verdict["reasons"] == []


def test_negative_age_is_not_wedged():
    verdict = wedge_verdict(True, -5.0, 1800)
    assert verdict["wedged"] is False
    assert verdict["reasons"] == []
    assert verdict["measured"]["activity_age_seconds"] == -5.0


def test_negative_age_does_not_add_stale_reason_to_dead_pid():
    verdict = wedge_verdict(False, -5.0, 1800)
    assert verdict["reasons"] == ["dead_pid"]


def test_age_none_is_not_wedged_even_with_live_pid():
    verdict = wedge_verdict(True, None, 1800)
    assert verdict["wedged"] is False
    assert verdict["reasons"] == []
    assert verdict["measured"]["activity_age_seconds"] is None


def test_pid_alive_none_never_produces_dead_pid():
    unknown_no_signal = wedge_verdict(None, None, 1800)
    assert unknown_no_signal["wedged"] is False
    assert unknown_no_signal["reasons"] == []

    unknown_stale = wedge_verdict(None, 2731.2, 1800)
    assert unknown_stale["wedged"] is True
    assert unknown_stale["reasons"] == ["stale_activity"]
    assert "dead_pid" not in unknown_stale["reasons"]


def test_healthy_story_is_not_wedged():
    verdict = wedge_verdict(True, 12.0, 1800)
    assert verdict["wedged"] is False
    assert verdict["reasons"] == []
    assert verdict["measured"] == {"activity_age_seconds": 12.0, "pid_alive": True}
    assert verdict["thresholds"] == {"stale_seconds": 1800}


# ---------- threshold comes from the argument, not a config default ----------


def test_threshold_comes_from_argument_not_config_default():
    over = wedge_verdict(True, 61.0, 60)
    assert over["wedged"] is True
    assert over["reasons"] == ["stale_activity"]
    assert over["thresholds"] == {"stale_seconds": 60}
    assert over["measured"]["activity_age_seconds"] == 61.0

    under = wedge_verdict(True, 59.0, 60)
    assert under["wedged"] is False
    assert under["reasons"] == []

    exact = wedge_verdict(True, 60.0, 60)
    assert exact["wedged"] is False
    assert exact["reasons"] == []


def test_dead_pid_wedges_regardless_of_threshold():
    assert wedge_verdict(False, None, 60)["reasons"] == ["dead_pid"]
    assert wedge_verdict(False, None, 999999)["reasons"] == ["dead_pid"]


# ---------- wedged iff reasons non-empty ----------


@pytest.mark.parametrize(
    ("pid_alive", "activity_age_seconds", "stale_seconds"),
    [
        (True, 12.0, 1800),
        (True, None, 1800),
        (True, 1800.0, 1800),
        (True, 1800.000001, 1800),
        (True, -5.0, 1800),
        (True, 0.0, 1800),
        (False, None, 1800),
        (False, -5.0, 1800),
        (False, 1800.0, 1800),
        (None, None, 1800),
        (None, 2731.2, 1800),
        (False, 2731.2, 1800),
        (True, 61.0, 60),
    ],
)
def test_wedged_iff_reasons_nonempty(pid_alive, activity_age_seconds, stale_seconds):
    verdict = wedge_verdict(pid_alive, activity_age_seconds, stale_seconds)
    assert isinstance(verdict["wedged"], bool)
    assert verdict["wedged"] is bool(verdict["reasons"])
    assert set(verdict["reasons"]) <= {"dead_pid", "stale_activity"}