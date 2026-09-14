"""Per-story tests for the scheduler-side role-call timeout clamp.

2026-09-14 incident: ``OllamaDriver.complete`` already bounds a SINGLE model
call with a wall-clock budget computed once before the first attempt
(app/backend_ollama.py) from ``resolve_role_call_timeout()``
(app/inference_providers.py, env ``PIPELINE_ROLE_CALL_TIMEOUT_SECONDS``,
hardcoded default 600s). But one ``advance_all_plans`` tick STACKS several
in-process model calls (review-loop turns, security review, overlord
adjudications). During a provider outage every stacked call burns its full
600s before failing: two stacked calls (1200s) already exceed the 900s
reconcile join deadline (``_DEFAULT_RECONCILE_JOIN_TIMEOUT_S``,
pipeline/scheduler_daemon.py), and the review loop can stack three. That is
the exact shape that triggers the watchdog abandonment which leaks the plan
lock.

Contract pinned here:

- ``app.inference_providers.resolve_scheduler_role_call_timeout()`` reads the
  NEW env var ``PIPELINE_SCHEDULER_ROLE_CALL_TIMEOUT_SECONDS`` with the same
  parsing discipline as ``resolve_role_call_timeout()``: unset / blank /
  unparseable / non-positive / non-finite all degrade to the hardcoded 180.0
  default, and the resolver never returns None, zero, or inf.
- ``resolve_role_call_timeout()`` and its env var are untouched (different
  env var name; the two resolvers must not read each other's variable).
- ``pipeline.scheduler_daemon._apply_scheduler_role_call_clamp()`` returns
  ``min(resolve_role_call_timeout(), resolve_scheduler_role_call_timeout())``
  and lowers ``os.environ[PIPELINE_ROLE_CALL_TIMEOUT_SECONDS]`` only when the
  clamp is strictly smaller than what the process env currently resolves to;
  an operator value that is already smaller-or-equal is left byte-for-byte
  untouched.
- ``run_daemon()`` applies the clamp BEFORE the lazy ``from .server import
  advance_all_plans`` import, so no model call in the scheduler process can
  happen before the per-tick stacking is bounded. Merely importing the module
  must NOT mutate the environment.
- The clamp carries the deliberate-mutation comment with the sizing
  arithmetic (180s clamp x 3-4 stacked calls ~ 540-720s, inside the 900s join
  deadline, so the watchdog is a backstop again rather than the primary
  bound).
- No new error handling at call sites: app/backend_ollama.py is untouched and
  the reconcile/scan join defaults stay 900s.

Every env value is stubbed with monkeypatch; no live value is asserted.
"""
from __future__ import annotations

import inspect
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import ClassVar

import pytest

from app import inference_providers
from pipeline import scheduler_daemon as mod
from pipeline import server as server_mod

ROLE_TIMEOUT_ENV = "PIPELINE_ROLE_CALL_TIMEOUT_SECONDS"
SCHEDULER_TIMEOUT_ENV = "PIPELINE_SCHEDULER_ROLE_CALL_TIMEOUT_SECONDS"
SCHEDULER_CLAMP_DEFAULT_S = 180.0
ROLE_TIMEOUT_DEFAULT_S = 600.0
REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_scheduler() -> float:
    """Call the new resolver (AttributeError until it exists - the RED state)."""
    return inference_providers.resolve_scheduler_role_call_timeout()


def _apply_clamp() -> float:
    """Call the new applier (AttributeError until it exists - the RED state)."""
    return mod._apply_scheduler_role_call_clamp()


def _stub_env(monkeypatch, *, role=None, scheduler=None) -> None:
    """Stub both timeout env vars; ``None`` means 'unset'."""
    for name, value in ((ROLE_TIMEOUT_ENV, role), (SCHEDULER_TIMEOUT_ENV, scheduler)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


# ---------------------------------------------------------------------------
# resolve_scheduler_role_call_timeout() - parsing matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "\t", "abc", "0", "-5", "0.0", "-0.5", "inf", "-inf",
     "nan", "1e999", "None", "180s", "1,5"],
)
def test_scheduler_resolver_degrades_to_180_default(monkeypatch, raw):
    _stub_env(monkeypatch, scheduler=raw)
    value = _resolve_scheduler()
    assert value == SCHEDULER_CLAMP_DEFAULT_S
    assert isinstance(value, float)
    assert value is not None and value > 0
    assert value != float("inf")


@pytest.mark.parametrize(
    "raw,expected",
    [("90.5", 90.5), ("0.2", 0.2), ("180", 180.0), ("1e3", 1000.0), (" 90.5 ", 90.5)],
)
def test_scheduler_resolver_honors_valid_override(monkeypatch, raw, expected):
    _stub_env(monkeypatch, scheduler=raw)
    assert _resolve_scheduler() == expected


def test_scheduler_resolver_reads_its_own_env_var_not_the_role_one(monkeypatch):
    # Role var set, scheduler var unset -> scheduler resolver still defaults.
    _stub_env(monkeypatch, role="90.5", scheduler=None)
    assert _resolve_scheduler() == SCHEDULER_CLAMP_DEFAULT_S


def test_scheduler_resolver_is_read_per_call_not_cached(monkeypatch):
    _stub_env(monkeypatch, scheduler="90.5")
    assert _resolve_scheduler() == 90.5
    monkeypatch.setenv(SCHEDULER_TIMEOUT_ENV, "42")
    assert _resolve_scheduler() == 42.0


def test_scheduler_resolver_is_module_level_callable():
    assert callable(inference_providers.resolve_scheduler_role_call_timeout)
    assert inspect.signature(
        inference_providers.resolve_scheduler_role_call_timeout
    ).parameters == {}


# ---------------------------------------------------------------------------
# resolve_role_call_timeout() - must stay untouched (different env var)
# ---------------------------------------------------------------------------


def test_role_resolver_env_var_name_unchanged():
    assert inference_providers.ROLE_CALL_TIMEOUT_ENV == ROLE_TIMEOUT_ENV


@pytest.mark.parametrize("raw", [None, "", "abc", "0", "-5", "inf", "nan"])
def test_role_resolver_still_defaults_to_600(monkeypatch, raw):
    _stub_env(monkeypatch, role=raw)
    assert inference_providers.resolve_role_call_timeout() == ROLE_TIMEOUT_DEFAULT_S


def test_role_resolver_still_honors_valid_override(monkeypatch):
    _stub_env(monkeypatch, role="90.5")
    assert inference_providers.resolve_role_call_timeout() == 90.5


def test_role_resolver_ignores_the_new_scheduler_env_var(monkeypatch):
    _stub_env(monkeypatch, role=None, scheduler="90.5")
    assert inference_providers.resolve_role_call_timeout() == ROLE_TIMEOUT_DEFAULT_S


# ---------------------------------------------------------------------------
# _apply_scheduler_role_call_clamp() - applier matrix
# ---------------------------------------------------------------------------


def test_clamp_operator_env_unset_lowers_to_180(monkeypatch):
    _stub_env(monkeypatch, role=None, scheduler=None)
    effective = _apply_clamp()
    assert effective == SCHEDULER_CLAMP_DEFAULT_S
    assert isinstance(effective, float)
    assert float(os.environ[ROLE_TIMEOUT_ENV]) == SCHEDULER_CLAMP_DEFAULT_S
    assert inference_providers.resolve_role_call_timeout() == SCHEDULER_CLAMP_DEFAULT_S


def test_clamp_operator_600_explicit_is_lowered_to_180(monkeypatch):
    _stub_env(monkeypatch, role="600", scheduler=None)
    assert _apply_clamp() == SCHEDULER_CLAMP_DEFAULT_S
    assert float(os.environ[ROLE_TIMEOUT_ENV]) == SCHEDULER_CLAMP_DEFAULT_S


def test_clamp_operator_90_is_left_untouched(monkeypatch):
    _stub_env(monkeypatch, role="90", scheduler=None)
    assert _apply_clamp() == 90.0
    # Byte-for-byte untouched: the operator's own string survives.
    assert os.environ[ROLE_TIMEOUT_ENV] == "90"
    assert inference_providers.resolve_role_call_timeout() == 90.0


@pytest.mark.parametrize("raw,expected", [("180", 180.0), ("179.9", 179.9), ("0.5", 0.5)])
def test_clamp_smaller_or_equal_operator_value_is_untouched(monkeypatch, raw, expected):
    _stub_env(monkeypatch, role=raw, scheduler=None)
    assert _apply_clamp() == expected
    assert os.environ[ROLE_TIMEOUT_ENV] == raw


def test_clamp_is_idempotent_on_reapply(monkeypatch):
    _stub_env(monkeypatch, role=None, scheduler=None)
    first = _apply_clamp()
    after_first = os.environ[ROLE_TIMEOUT_ENV]
    second = _apply_clamp()
    assert first == second == SCHEDULER_CLAMP_DEFAULT_S
    assert os.environ[ROLE_TIMEOUT_ENV] == after_first


def test_clamp_honors_smaller_scheduler_override(monkeypatch):
    _stub_env(monkeypatch, role=None, scheduler="90.5")
    assert _apply_clamp() == 90.5
    assert float(os.environ[ROLE_TIMEOUT_ENV]) == 90.5
    # The scheduler var itself is never rewritten.
    assert os.environ[SCHEDULER_TIMEOUT_ENV] == "90.5"


def test_clamp_uses_min_when_operator_value_is_already_smaller(monkeypatch):
    _stub_env(monkeypatch, role="120", scheduler="300")
    assert _apply_clamp() == 120.0
    assert os.environ[ROLE_TIMEOUT_ENV] == "120"


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "inf", "nan", ""])
def test_clamp_treats_malformed_operator_value_as_the_600_default(monkeypatch, raw):
    _stub_env(monkeypatch, role=raw, scheduler=None)
    assert _apply_clamp() == SCHEDULER_CLAMP_DEFAULT_S
    assert float(os.environ[ROLE_TIMEOUT_ENV]) == SCHEDULER_CLAMP_DEFAULT_S


@pytest.mark.parametrize(
    "role,scheduler",
    [(None, None), ("600", None), ("90", None), (None, "90.5"), ("120", "300"),
     ("abc", None), ("0", "0"), ("inf", "nan"), ("1e3", "0.2")],
)
def test_clamp_returns_the_effective_value_and_never_raises(monkeypatch, role, scheduler):
    _stub_env(monkeypatch, role=role, scheduler=scheduler)
    effective = _apply_clamp()
    assert isinstance(effective, float)
    assert effective > 0 and effective != float("inf")
    # The returned value IS the effective value the process now resolves to.
    assert effective == float(os.environ[ROLE_TIMEOUT_ENV])
    assert effective == inference_providers.resolve_role_call_timeout()


def test_clamp_is_module_level_callable_with_no_required_args():
    assert callable(mod._apply_scheduler_role_call_clamp)
    sig = inspect.signature(mod._apply_scheduler_role_call_clamp)
    required = [
        p for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]
    assert required == []


# ---------------------------------------------------------------------------
# run_daemon() wiring: clamp BEFORE the lazy advance_all_plans import
# ---------------------------------------------------------------------------


class _FakeDaemon:
    instances: ClassVar[list] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeDaemon.instances.append(self)

    def run_forever(self, stop_event) -> None:
        self.stop_event = stop_event


def _wire_run_daemon(monkeypatch, tmp_path) -> None:
    _FakeDaemon.instances = []
    monkeypatch.setattr(mod, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(mod, "SchedulerDaemon", _FakeDaemon)
    monkeypatch.setattr(mod, "build_bus", lambda: object())
    monkeypatch.setattr(mod, "scan_all_plans", lambda bus: None)
    monkeypatch.delenv("PIPELINE_SCHEDULER_INTERVAL_S", raising=False)
    monkeypatch.delenv("PIPELINE_SCHEDULER_HEALTH_PATH", raising=False)


def test_run_daemon_clamps_before_lazy_advance_all_plans_import(monkeypatch, tmp_path):
    _wire_run_daemon(monkeypatch, tmp_path)
    _stub_env(monkeypatch, role=None, scheduler=None)

    real_clamp = mod._apply_scheduler_role_call_clamp
    env_when_clamped: list = []

    def spy_clamp() -> float:
        env_when_clamped.append(os.environ.get(ROLE_TIMEOUT_ENV))
        return real_clamp()

    monkeypatch.setattr(mod, "_apply_scheduler_role_call_clamp", spy_clamp)

    # A stand-in for pipeline.server whose attribute access records the env at
    # the exact moment run_daemon() performs its lazy import.
    fake_server = types.ModuleType("pipeline.server")
    env_at_import: list = []

    def _getattr(name):
        if name == "advance_all_plans":
            env_at_import.append(os.environ.get(ROLE_TIMEOUT_ENV))
            return lambda: None
        raise AttributeError(name)

    fake_server.__getattr__ = _getattr
    monkeypatch.setitem(sys.modules, "pipeline.server", fake_server)

    assert mod.run_daemon() == 0

    # The clamp ran, and it ran while the operator env was still unset.
    assert env_when_clamped, "run_daemon() never applied the clamp"
    assert all(v is None for v in env_when_clamped)
    # By the time advance_all_plans was imported, the env was already clamped.
    assert env_at_import, "run_daemon() never imported advance_all_plans"
    assert all(float(v) == SCHEDULER_CLAMP_DEFAULT_S for v in env_at_import)
    assert len(_FakeDaemon.instances) == 1
    assert callable(_FakeDaemon.instances[0].kwargs["reconcile_fn"])


def test_run_daemon_leaves_smaller_operator_value_untouched(monkeypatch, tmp_path):
    _wire_run_daemon(monkeypatch, tmp_path)
    _stub_env(monkeypatch, role="90", scheduler=None)
    monkeypatch.setattr(server_mod, "advance_all_plans", lambda: None)

    assert mod.run_daemon() == 0
    assert os.environ[ROLE_TIMEOUT_ENV] == "90"
    assert inference_providers.resolve_role_call_timeout() == 90.0


def test_importing_scheduler_daemon_does_not_mutate_the_environment():
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("PIPELINE_", "LOCAL_AGENT_"))
    }
    env["PIPELINE_SKIP_ENV_FILE"] = "1"
    code = (
        "import os, pipeline.scheduler_daemon as m;"
        "print(os.environ.get('PIPELINE_ROLE_CALL_TIMEOUT_SECONDS', '<unset>'))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "<unset>"


# ---------------------------------------------------------------------------
# Structural guards: the deliberate-mutation comment + the no-touch list
# ---------------------------------------------------------------------------


def test_clamp_comment_states_the_sizing_arithmetic():
    """The deliberate os.environ mutation must carry the sizing comment.

    The comment may sit inside the function or immediately above its ``def``,
    so the window spans both.
    """
    module_src = (REPO_ROOT / "pipeline" / "scheduler_daemon.py").read_text()
    lines = module_src.splitlines()
    def_idx = next(
        (i for i, ln in enumerate(lines)
         if ln.lstrip().startswith("def _apply_scheduler_role_call_clamp")),
        None,
    )
    assert def_idx is not None, (
        "pipeline/scheduler_daemon.py must define _apply_scheduler_role_call_clamp"
    )
    window = "\n".join(lines[max(0, def_idx - 40):def_idx + 60])
    lowered = window.lower()
    assert any(ln.lstrip().startswith("#") for ln in window.splitlines()), (
        "the deliberate os.environ mutation must carry a comment"
    )
    # The arithmetic: 180s clamp x 3-4 stacked calls ~ 540-720s < 900s join.
    assert "180" in window
    assert "900" in window
    assert "540" in window or "720" in window
    assert "stack" in lowered
    assert "600" in window
    assert "mcp" in lowered or "interactive" in lowered
    assert "per call" in lowered or "at call time" in lowered
    # The watchdog must be described as a backstop again, not the primary bound.
    assert "backstop" in lowered


def test_clamp_never_raises_and_adds_no_error_handling():
    """The clamp only changes how long we wait - it never raises.

    A clamped-out call still raises exactly the RuntimeError complete()
    raises today; the clamp itself must not introduce new error handling.
    """
    src = inspect.getsource(mod._apply_scheduler_role_call_clamp)
    code_lines = [
        ln for ln in src.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert not any(ln.lstrip().startswith("raise ") for ln in code_lines)


def test_backend_ollama_is_untouched_by_the_clamp():
    src = (REPO_ROOT / "app" / "backend_ollama.py").read_text()
    assert "resolve_role_call_timeout" in src
    assert "PIPELINE_SCHEDULER_ROLE_CALL_TIMEOUT_SECONDS" not in src
    assert "resolve_scheduler_role_call_timeout" not in src
    assert "_apply_scheduler_role_call_clamp" not in src


def test_join_timeout_defaults_are_unchanged():
    assert mod._DEFAULT_RECONCILE_JOIN_TIMEOUT_S == 900.0
    assert mod._DEFAULT_SCAN_JOIN_TIMEOUT_S == 900.0
