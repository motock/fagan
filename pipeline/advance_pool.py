"""Run one plan's advance in a child process, with a timeout that really bites.

Why a child process at all: ``_scoped_repo_root`` in ``server.py`` swaps the
module-global ``REPO_ROOT`` per plan and ~100 call sites read it, so two plans
cannot be advanced concurrently inside one interpreter. A process per advance
isolates that global. It also makes the deadline enforceable: the advance holds
the per-plan ``flock`` for its whole tick, and terminating the child releases it
by process death (the fd is not inheritable, so no agent grandchild keeps it
alive). An over-deadline thread in the daemon would be abandoned while still
holding the lock.

Security invariant: every child spawns with ``cwd`` set to this control plane's
own source root -- the directory containing the ``pipeline`` package -- and
never a story worktree. The daemon is launched as ``python -m
pipeline.scheduler_daemon`` from its checkout, so ``-m`` puts the cwd first on
``sys.path``; a child started inside a worktree of this repository would import
that branch's unreviewed ``pipeline`` package and run it as the scheduler. The
target plan/worktree is passed as an argument instead.

The child's process group is deliberately shared: agents it launches must
survive it, so there is no ``start_new_session`` and no ``os.killpg`` here.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pipeline
from pipeline import actor
from pipeline.scheduler_timeouts import _reconcile_join_timeout_seconds

__all__ = ["advance_plan_subprocess"]

logger = logging.getLogger(__name__)

_TEMP_PREFIX = "advance-"
_RESULT_NAME = "result.json"
_KILL_GRACE_SECONDS = 10


def _child_argv(plan_name: str, result_path: str) -> list[str]:
    """Return the command line for one advance child (replaced in tests)."""
    return [sys.executable, "-m", "pipeline.advance_one", plan_name, result_path]


def _source_root() -> Path:
    """Return the directory holding this control plane's ``pipeline`` package."""
    return Path(pipeline.__file__).resolve().parent.parent


def _invalid_plan_name(plan_name: str) -> bool:
    """Return whether *plan_name* is unsafe to hand to a child process."""
    from pipeline.parsers import _validate_key

    try:
        _validate_key(plan_name)
    except ValueError:
        return True
    return False


def _read_result(result_path: str, returncode: int | None) -> dict[str, Any]:
    """Return the child's result payload, or a failure dict naming its exit code."""
    try:
        with open(result_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "error": f"advance child exited with code {returncode} and no result",
        }
    return payload


def _terminate(proc: subprocess.Popen, plan_name: str, timeout_s: float) -> dict[str, Any]:
    """Stop an over-deadline *proc* and return the timeout payload."""
    proc.terminate()
    try:
        proc.wait(timeout=_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    logger.warning(
        "advance child timed out",
        extra={"plan": plan_name, "timeout_s": timeout_s},
    )
    return {"ok": False, "error": "advance timed out", "timed_out": True}


def advance_plan_subprocess(plan_name: str, timeout_s: float) -> dict[str, Any]:
    """Advance *plan_name* in a child process and return its result payload.

    Returns the child's result dict unchanged on success; otherwise
    ``{"ok": False, "error": ...}``, with ``"timed_out": True`` when the
    deadline was hit and the child had to be terminated.
    """
    if _invalid_plan_name(plan_name):
        return {"ok": False, "error": "invalid plan name"}

    tmpdir = tempfile.mkdtemp(prefix=_TEMP_PREFIX)
    os.chmod(tmpdir, 0o700)
    result_path = str(Path(tmpdir) / _RESULT_NAME)
    try:
        proc = subprocess.Popen(
            _child_argv(plan_name, result_path),
            cwd=str(_source_root()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return _terminate(proc, plan_name, timeout_s)
        return _read_result(result_path, proc.returncode)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --- Bounded pool for the scheduler's reconcile pass (TP-02) --------------
#
# ``PipelineService.advance_all_plans`` used to advance every manifest in turn,
# so one reconcile pass cost the SUM of every plan's work and could exceed the
# watchdog's 900s reconcile deadline -- the pass was abandoned mid-flight and
# the daemon restarted 300s later with the work still running. The pool runs one
# child per plan instead, so a pass costs roughly its slowest plan.
#
# Scope is deliberate: the children re-bind ``system:scheduler`` in
# ``pipeline/advance_one.py``, so pooling the ``advance_all_plans`` MCP tool for
# any other caller would escalate that caller to the scheduler's privileges.
# ``pooled_advance_enabled`` therefore admits only the bound
# ``SYSTEM_SCHEDULER`` with more than one worker configured.

#: Plans the scheduler advances concurrently. Every value is read per call, so
#: an operator override takes effect on the next tick without a restart.
_ADVANCE_WORKERS_ENV = "PIPELINE_ADVANCE_WORKERS"
#: Default 1 = the original serial in-process loop: an unconfigured deployment
#: keeps exactly the behaviour it had before this module grew a pool.
_DEFAULT_ADVANCE_WORKERS = 1
#: Ceiling on the pool. Each worker is a child process holding its own plan
#: flock plus an agent process tree, so an unbounded value is an OOM, not a
#: throughput setting.
_MAX_ADVANCE_WORKERS = 32

#: Per-plan wall clock for one child.
_PLAN_TIMEOUT_ENV = "PIPELINE_ADVANCE_PLAN_TIMEOUT_SECONDS"
_DEFAULT_PLAN_TIMEOUT_S = 600.0

#: Cursor rotated by one plan per pass so no plan is always advanced last (and
#: therefore most exposed to the pass deadline).
_rotation = 0


def _advance_workers() -> int:
    """Return the reconcile pool size, read from the environment on every call.

    Malformed, non-positive and zero values log a WARNING and give the default
    of 1 (the serial loop) instead of raising: a typo in an operator's
    environment must never take the reconcile pass down. Values above
    ``_MAX_ADVANCE_WORKERS`` are capped rather than rejected for the same
    reason. ``int()`` is used rather than ``float()`` on purpose: this is a
    count of processes, so ``"3.5"`` is malformed, not 3 or 4.
    """
    raw = os.environ.get(_ADVANCE_WORKERS_ENV)
    if raw is None:
        return _DEFAULT_ADVANCE_WORKERS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s must be an integer, got %r; using default %d",
            _ADVANCE_WORKERS_ENV,
            raw,
            _DEFAULT_ADVANCE_WORKERS,
        )
        return _DEFAULT_ADVANCE_WORKERS
    if value < 1:
        logger.warning(
            "%s must be at least 1, got %r; using default %d",
            _ADVANCE_WORKERS_ENV,
            raw,
            _DEFAULT_ADVANCE_WORKERS,
        )
        return _DEFAULT_ADVANCE_WORKERS
    return min(value, _MAX_ADVANCE_WORKERS)


def _plan_timeout_seconds() -> float:
    """Return the per-plan child timeout, read on every call.

    The finiteness test has to come BEFORE the positivity test: ``nan <= 0`` is
    False, so a bare ``value <= 0`` guard lets ``nan`` through and the timeout
    then depends on argument order in ``min()``. A non-finite or non-positive
    override degrades to the default with a WARNING.
    """
    raw = os.environ.get(_PLAN_TIMEOUT_ENV)
    if raw is None:
        return _DEFAULT_PLAN_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s must be a number, got %r; using default %gs",
            _PLAN_TIMEOUT_ENV,
            raw,
            _DEFAULT_PLAN_TIMEOUT_S,
        )
        return _DEFAULT_PLAN_TIMEOUT_S
    if not math.isfinite(value) or value <= 0:
        logger.warning(
            "%s must be a finite positive number, got %r; using default %gs",
            _PLAN_TIMEOUT_ENV,
            raw,
            _DEFAULT_PLAN_TIMEOUT_S,
        )
        return _DEFAULT_PLAN_TIMEOUT_S
    return value


def pooled_advance_enabled() -> bool:
    """Return whether this reconcile pass may advance plans in child processes.

    True only for the bound ``SYSTEM_SCHEDULER`` principal with more than one
    worker configured. ``current_principal()`` falls back to
    ``LOCAL_OPERATOR`` when nothing is bound, which is exactly the case that
    must stay serial: the children run as ``system:scheduler``, so pooling an
    ``advance_all_plans`` MCP call from any other caller would escalate it.

    ``actor`` is consulted through the module (not imported by value) so tests
    can bind and unbind principals without reloading this module.
    """
    workers = _advance_workers()
    if workers <= 1:
        return False
    return actor.current_principal() == actor.SYSTEM_SCHEDULER


def _deadline() -> float:
    """Return the monotonic instant by which this whole pass must be done.

    The pass runs inside the daemon's reconcile watchdog, which abandons the
    call after ``_reconcile_join_timeout_seconds()`` and restarts 300s later.
    Stopping 60s early (never less than a 30s window) leaves room to collect
    the futures and log the pass instead of being killed with work in flight.
    """
    return time.monotonic() + max(30.0, _reconcile_join_timeout_seconds() - 60.0)


def _advance_one_pooled(plan_name: str, deadline: float) -> dict[str, Any]:
    """Advance one plan in a child process, bounded by *deadline*.

    ``remaining`` is computed here, at task start, not once before submission:
    a plan that only gets a worker at the end of the window must not inherit the
    full per-plan timeout and run past the deadline. A ``remaining`` of zero or
    less skips the plan outright rather than spawning a child with a timeout of
    0, whose meaning (kill immediately vs. wait forever) depends on the runner.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"ok": True, "skipped": "deadline"}
    return advance_plan_subprocess(plan_name, min(_plan_timeout_seconds(), remaining))


def _log_pass(plans: int, workers: int, elapsed_s: float, timed_out: int) -> None:
    """Log one reconcile pass: the counts an operator needs to tune the pool.

    The key=value pairs sit in the message so a plain ``grep 'timed_out='`` on
    the daemon log finds every truncated pass, and they are repeated as
    structured ``extra`` fields for log processors that read the record.
    """
    logger.info(
        "pooled advance pass complete plans=%d workers=%d elapsed_s=%.3f timed_out=%d",
        plans,
        workers,
        elapsed_s,
        timed_out,
        extra={
            "plans": plans,
            "workers": workers,
            "elapsed_s": round(elapsed_s, 3),
            "timed_out": timed_out,
        },
    )


def advance_all_plans_pooled() -> dict[str, Any]:
    """Advance every plan in a bounded pool of child processes.

    Returns ``{"ok": True, "plans": {name: result}}`` with an entry for every
    plan listed at call time. One plan's failure is recorded against that plan
    only; it never costs another plan its tick.

    The plan list is read from the live store inside the function: a top-level
    ``pipeline.server`` import would close an import cycle, and the list has to
    reflect the manifests that exist now, not at import time.
    """
    global _rotation

    from pipeline import server

    plans = list(server._store.list_manifests())
    if not plans:
        _log_pass(0, _advance_workers(), 0.0, 0)
        return {"ok": True, "plans": {}}

    # Rotate by one plan per pass so the plan advanced last pass is advanced
    # first next pass: the tail of the window is where the deadline bites.
    # ``plans`` is a fresh list (the store may hand back its own), so the
    # slice-and-concat rotation cannot permute state other callers read.
    start = _rotation % len(plans)
    ordered = plans[start:] + plans[:start]
    _rotation += 1

    workers = min(_advance_workers(), len(ordered))
    deadline = _deadline()
    started = time.monotonic()
    results: dict[str, Any] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_advance_one_pooled, name, deadline): name
            for name in ordered
        }
        for future, name in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001 (one plan must not sink the pass)
                # The exception text carries paths and payloads, so only the
                # class name is recorded. ``str(exc)`` here would leak both
                # into the result payload and into anything that logs it.
                logger.warning(
                    "pooled advance raised",
                    extra={"plan": name, "actor": actor.actor_id(),
                           "exception": type(exc).__name__},
                )
                results[name] = {
                    "ok": False,
                    "error": f"advance raised {type(exc).__name__}",
                }

    timed_out = sum(
        1 for result in results.values()
        if isinstance(result, dict) and result.get("skipped") == "deadline"
    )
    _log_pass(len(ordered), workers, time.monotonic() - started, timed_out)
    return {"ok": True, "plans": results}
