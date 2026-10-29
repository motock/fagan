"""Child-process entrypoint: run exactly one plan's advance and report the result.

``advance_all_plans`` advances plan by plan, and it must: ``server.py``'s
``_scoped_repo_root`` swaps the module-global ``REPO_ROOT`` for the plan being
advanced, and ~100 call sites read that global. Two concurrent in-process
advances would therefore see each other's ``REPO_ROOT``.

Running one plan's advance here, in its own interpreter, gives isolation by
construction. It also makes a timeout real: the advance holds the per-plan
``flock`` for its whole tick, and killing this process releases that lock
because the file descriptor is not inheritable (no agent grandchild keeps it
alive). Abandoning a thread instead leaves the lock held by the daemon.

Protocol: ``argv`` is ``[plan_name, result_path]``. The result path must be
absolute, in an existing directory, and the payload is written atomically with
mode 0600 because it is a handoff to the pool runner in the parent process.
Exit codes: 0 success, 1 advance raised, 2 bad invocation (nothing written).

Identity is re-bound here (``SYSTEM_SCHEDULER`` + a fresh correlation id) so the
child's records and policy decisions look exactly like the scheduler's own.
``configure_child`` holds the per-process setup (logging, the scheduler role call
clamp, the ``service.instance.id`` resource attribute, telemetry) that every
process advancing on the daemon's behalf needs, so the long-lived advance worker
in ``pipeline.advance_worker`` shares exactly the same initialization.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

from pipeline.actor import (
    SYSTEM_SCHEDULER,
    bind_correlation_id,
    bind_principal,
    new_correlation_id,
)

__all__ = ["configure_child", "main"]


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    """Write *payload* to *path* atomically, readable only by the owner.

    A temp file in the same directory plus ``os.replace`` means a reader never
    observes a partially written file -- the parent may read it the instant
    this process exits, and a kill mid-write must not leave truncated JSON.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".advance-", suffix=".json")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _invalid_arguments(args: list[str]) -> bool:
    """Return whether *args* is unacceptable as ``[plan_name, result_path]``."""
    if len(args) != 2:
        return True
    plan_name, raw_result_path = args
    from pipeline.parsers import _validate_key

    try:
        _validate_key(plan_name)
    except ValueError:
        return True
    result_path = Path(raw_result_path)
    return not result_path.is_absolute() or not result_path.parent.is_dir()


def configure_child() -> None:
    """Apply the per-process setup a scheduler child needs before advancing.

    Shared by every process that advances a plan on the daemon's behalf (one
    plan per process here, and the long-lived advance worker). It is
    deliberately idempotent: logging is reconfigured from the environment, the
    scheduler role call clamp is re-applied, and telemetry is (re)initialised
    for this process id.
    """
    import pipeline.log_format
    import pipeline.scheduler_timeouts
    import pipeline.telemetry

    pipeline.log_format.configure_logging_from_env()
    # The child re-reads .pipeline.env on import and that file WINS over the
    # inherited environment, so the clamp the daemon applied must be re-applied
    # here or this process would fall back to the interactive per-call budget.
    pipeline.scheduler_timeouts._apply_scheduler_role_call_clamp()

    existing = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    instance = f"{socket.gethostname()}-{os.getpid()}"
    os.environ["OTEL_RESOURCE_ATTRIBUTES"] = (
        f"{existing},service.instance.id={instance}" if existing else f"service.instance.id={instance}"
    )
    pipeline.telemetry.configure()
    pipeline.telemetry.configure_metrics()


def main(argv: list[str] | None = None) -> int:
    """Run one plan advance; return the process exit code."""
    # Import-cycle: pipeline.server must be imported before other pipeline
    # modules, and it is resolved per call so tests can patch advance_pipeline.
    import pipeline.server

    args = list(sys.argv[1:] if argv is None else argv)
    if _invalid_arguments(args):
        return 2

    plan_name, raw_result_path = args
    result_path = Path(raw_result_path)

    import pipeline.telemetry

    configure_child()

    try:
        with bind_principal(SYSTEM_SCHEDULER), bind_correlation_id(new_correlation_id()):
            try:
                result = pipeline.server.advance_pipeline(plan_name)
            except Exception as exc:  # noqa: BLE001 - reported by class name, exit 1
                # Class name only: the message can carry filesystem paths.
                _write_result(
                    result_path,
                    {"ok": False, "error": f"advance raised {type(exc).__name__}"},
                )
                return 1
        _write_result(result_path, result)
        return 0
    finally:
        pipeline.telemetry.flush()


if __name__ == "__main__":
    sys.exit(main())
