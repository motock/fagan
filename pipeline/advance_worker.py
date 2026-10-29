"""Long-lived advance worker: advance many plans without re-paying the import.

``pipeline.server`` costs 0.9-1.4 s to import, and ``pipeline.advance_one``
advances exactly one plan per process -- so a daemon that spawns a child per
plan pays that import per plan. This worker pays it once and then serves many
advances from the same interpreter.

Why one advance at a time is safe here: ``REPO_ROOT`` in ``server.py`` is a
process global, and ``_scoped_repo_root`` swaps and restores it around each
advance, exactly as the serial in-process loop already relies on. Only two
CONCURRENT advances in one process would see each other's ``REPO_ROOT``, and
this worker never runs two at once: it answers one request before reading the
next.

Protocol: ``argv`` is ``[in_fd, out_fd]``, two non-negative integers naming
inherited pipe descriptors. One JSON request object per line on ``in_fd``
(``{"plan": <name>}``), exactly one JSON result line per request on
``out_fd``. EOF ends the loop and returns 0; a malformed invocation returns 2
and writes nothing. Results go only to ``out_fd`` -- fd 1 and 2 remain the
advance's own diagnostics channel.

Security invariant: the plan name is validated with
``pipeline.parsers._validate_key`` before it reaches the advance, and nothing
here spawns a child. A worker started as ``python -m pipeline.advance_worker``
puts its cwd first on ``sys.path``, so it must be started from this control
plane's source root and never from a story worktree.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pipeline
from pipeline.actor import (
    SYSTEM_SCHEDULER,
    bind_correlation_id,
    bind_principal,
    new_correlation_id,
)

__all__ = ["main"]

logger = logging.getLogger(__name__)

_MAX_TASKS_ENV = "PIPELINE_ADVANCE_WORKER_MAX_TASKS"
_DEFAULT_MAX_TASKS = 200
_ENV_FILE_NAME = ".pipeline.env"


def _source_root() -> Path:
    """Return the directory holding this control plane's ``pipeline`` package."""
    return Path(pipeline.__file__).resolve().parent.parent


def _env_file_path() -> Path:
    """Return the operator env file this worker watches for edits."""
    return _source_root() / _ENV_FILE_NAME


def _mtime_ns(path: Path) -> int | None:
    """Return *path*'s ``st_mtime_ns``, or ``None`` when it does not exist."""
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _max_tasks() -> int:
    """Return the recycle threshold, warning and defaulting when unparseable."""
    raw = os.environ.get(_MAX_TASKS_ENV)
    if raw is None:
        return _DEFAULT_MAX_TASKS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        logger.warning(
            "ignoring invalid advance worker task cap; using the default",
            extra={"variable": _MAX_TASKS_ENV, "default_max_tasks": _DEFAULT_MAX_TASKS},
        )
        return _DEFAULT_MAX_TASKS
    return value


def _parse_fds(argv: list[str]) -> tuple[int, int] | None:
    """Return ``(in_fd, out_fd)`` when *argv* names two pipe fds, else ``None``."""
    if len(argv) != 2:
        return None
    try:
        in_fd, out_fd = (int(arg) for arg in argv)
    except ValueError:
        return None
    if in_fd < 0 or out_fd < 0:
        return None
    return in_fd, out_fd


def _valid_plan(request: Any) -> str | None:
    """Return the plan name in *request* when it is safe to advance, else ``None``."""
    if not isinstance(request, dict):
        return None
    plan = request.get("plan")
    if not isinstance(plan, str):
        return None
    from pipeline.parsers import _validate_key

    try:
        _validate_key(plan)
    except ValueError:
        return None
    return plan


def _advance(plan: str) -> dict[str, Any]:
    """Advance *plan* under the scheduler identity; report failures by class."""
    # Import-cycle: pipeline.server must be imported before other pipeline
    # modules, and it is resolved per call so tests can patch advance_pipeline.
    import pipeline.server

    try:
        with bind_principal(SYSTEM_SCHEDULER), bind_correlation_id(new_correlation_id()):
            return pipeline.server.advance_pipeline(plan)
    except Exception as exc:  # noqa: BLE001 - reported by class name
        # Class name only: the message can carry filesystem paths.
        logger.warning(
            "advance raised while serving a request",
            extra={"plan": plan, "error_class": type(exc).__name__},
        )
        return {"ok": False, "error": f"advance raised {type(exc).__name__}"}


def _write_line(out_fd: int, payload: dict[str, Any]) -> None:
    """Write *payload* as exactly one JSON line to *out_fd* and flush it."""
    line = json.dumps(payload, default=str) + "\n"
    data = line.encode("utf-8")
    while data:
        written = os.write(out_fd, data)
        data = data[written:]


def main(argv: list[str] | None = None) -> int:
    """Serve advance requests from inherited pipes; return the process exit code."""
    # Import-cycle: pipeline.server must be imported before other pipeline modules.
    import pipeline.server
    import pipeline.telemetry

    args = list(sys.argv[1:] if argv is None else argv)
    fds = _parse_fds(args)
    if fds is None:
        logger.warning("advance worker invoked with invalid arguments", extra={"argc": len(args)})
        return 2
    in_fd, out_fd = fds

    # Agents an advance launches must never hold the protocol open past this
    # process, so neither descriptor is inheritable by a child.
    os.set_inheritable(in_fd, False)
    os.set_inheritable(out_fd, False)

    from pipeline.advance_one import configure_child

    configure_child()

    max_tasks = _max_tasks()
    env_path = _env_file_path()
    env_mtime_ns = _mtime_ns(env_path)
    answered = 0

    with os.fdopen(in_fd, "r", encoding="utf-8", closefd=True) as reader:
        for raw_line in reader:
            request: Any = None
            try:
                request = json.loads(raw_line)
            except ValueError:
                request = None
            plan = _valid_plan(request)
            if plan is None:
                logger.info("rejecting an invalid advance worker request")
                _write_line(out_fd, {"ok": False, "error": "invalid request"})
            else:
                _write_line(out_fd, _advance(plan))
            answered += 1
            pipeline.telemetry.flush()
            if answered >= max_tasks:
                logger.info(
                    "advance worker recycling after reaching its request cap",
                    extra={"answered": answered, "max_tasks": max_tasks},
                )
                return 0
            # An operator edit to the env file must take effect without a
            # daemon restart. Absent at start and still absent, or deleted
            # mid-run, is not an edit: only a changed *present* file recycles.
            current_mtime_ns = _mtime_ns(env_path)
            if current_mtime_ns is not None and current_mtime_ns != env_mtime_ns:
                logger.info(
                    "advance worker recycling after an operator env file change",
                    extra={"answered": answered},
                )
                return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
