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
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pipeline

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
