"""Detached review job.

``advance.py`` calls ``review_story`` inline for every ``tests_passed`` story.
One review can take 60-400s, which holds a worker - and today the whole pooled
pass - for its duration.  This module moves that work into a detached job: the
caller starts it, keeps its tick, and polls :func:`collect_review_job` for the
result.

Two invariants shape the design, both inherited from
:mod:`pipeline.merge_reverify`:

* **A job that dies without a result is a harness error, never a verdict.**
  A missing, unparsable, or unrecognised result reports ``state == "error"``;
  only the job itself can report ``"done"``.
* **The child runs from the control plane's own source root.**  The scheduler
  runs as ``python -m pipeline.scheduler_daemon`` from its checkout, so ``-m``
  puts the cwd first on ``sys.path``.  A child started inside a story worktree
  of this repository would import that branch's unreviewed ``pipeline``
  package and run it as the scheduler; the plan and story therefore travel as
  arguments, never as the cwd.

The job never writes the manifest.  It reports the review's ``status``,
``skipped`` and ``deferred`` values so the caller can apply them under the plan
lock.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pipeline
from pipeline.actor import (
    SYSTEM_SCHEDULER,
    bind_correlation_id,
    bind_principal,
    new_correlation_id,
)
from pipeline.parsers import _validate_key

__all__ = [
    "cancel_review_job",
    "collect_review_job",
    "detached_enabled",
    "main",
    "start_review_job",
]

ENV_VAR = "PIPELINE_REVIEW_DETACHED"
LOG_TAIL_CHARS = 2000
_VALID_STATES = frozenset({"done", "error"})
_NO_RESULT = "review job exited without a result"


def detached_enabled() -> bool:
    """Whether reviews should run detached instead of inline.

    False under pytest for the same reason the detached-grade primitives are
    only exported outside pytest (``pipeline/story_status.py``): pre-existing
    advance tests pin the synchronous ``review_story`` call sequence.
    Operators can also force the synchronous path with
    ``PIPELINE_REVIEW_DETACHED=0``.

    Both inputs are read on every call, so a test (or an operator shell) can
    flip the environment between calls.
    """
    if "pytest" in sys.modules:
        return False
    return os.environ.get(ENV_VAR) != "0"


def _source_root() -> Path:
    """Return the directory holding this control plane's ``pipeline`` package."""
    return Path(pipeline.__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Start / poll / cancel
# ---------------------------------------------------------------------------


def start_review_job(plan_name: str, story_key: str, state_root: Path) -> dict:
    """Spawn the detached job and return its handle.

    Returns ``{"pid", "result_path", "log_path", "started_at"}``; ``started_at``
    is an aware ISO-8601 UTC timestamp the caller feeds back to
    :func:`collect_review_job` for the timeout budget.

    Raises ``ValueError`` (naming the argument, never its value) for a key that
    could traverse a path.
    """
    try:
        _validate_key(plan_name)
    except ValueError:
        raise ValueError("invalid plan_name") from None
    try:
        _validate_key(story_key)
    except ValueError:
        raise ValueError("invalid story_key") from None

    job_dir = (Path(state_root) / "review_job" / story_key).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    # mkdir's mode is filtered by the process umask and does not re-tighten an
    # existing directory, so chmod it explicitly: the result and the log carry
    # story status and review output.
    os.chmod(job_dir, 0o700)
    result_path = job_dir / "result.json"
    log_path = job_dir / "job.log"
    # Delete any previous result BEFORE the child starts: a stale verdict left
    # behind by an earlier run must never be mistaken for this run's.
    result_path.unlink(missing_ok=True)

    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pipeline.review_job",
                plan_name,
                story_key,
                str(result_path),
            ],
            # Never a story worktree: ``-m`` would put that branch's unreviewed
            # ``pipeline`` package first on the child's sys.path.
            cwd=_source_root(),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
        )
    return {
        "pid": proc.pid,
        "result_path": str(result_path),
        "log_path": str(log_path),
        "started_at": _now().isoformat(),
    }


def _now() -> datetime:
    """Current time as an aware UTC datetime (patched in tests)."""
    return datetime.now(UTC)


def _pid_alive(pid: int) -> bool:
    """True while *pid* is a live, non-zombie process.

    ``os.kill(pid, 0)`` succeeds for zombie (defunct) processes too, so the
    ``ps`` stat column is checked as well - a zombie has exited and its result
    (if any) is already on disk.  A probe that cannot answer (permission
    denied, ``ps`` missing) counts as alive: assuming death would let the
    caller reject a job that may still be running.
    """
    try:
        os.kill(pid, 0)
        ps = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            check=False,
            capture_output=True,
            text=True,
        )
        stat = ps.stdout.strip()
        if not stat or stat.startswith("Z"):
            return False
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, subprocess.SubprocessError):
        return True
    return True


def _parse_started_at(job: dict) -> datetime | None:
    """The job's start time as an aware UTC datetime, or None if unreadable."""
    raw = job.get("started_at")
    if not isinstance(raw, str):
        return None
    try:
        started = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if started.tzinfo is None:
        return started.replace(tzinfo=UTC)
    return started


def _log_tail(log_path: Any) -> str:
    try:
        return Path(str(log_path)).read_text(errors="replace")[-LOG_TAIL_CHARS:]
    except OSError:
        return ""


def _no_result(job: dict) -> dict:
    return {
        "state": "error",
        "error": _NO_RESULT,
        "log_tail": _log_tail(job.get("log_path", "")),
    }


def _timed_out(timeout_s: float) -> dict:
    return {
        "state": "error",
        "error": f"review job timed out after {timeout_s:g}s",
        "timed_out": True,
    }


def collect_review_job(job: dict, timeout_s: float) -> dict | None:
    """Return the job's result once it finishes, else None.

    ``None`` means "still running, keep polling".  A job past ``timeout_s`` is
    SIGTERM'd as a process group and reported as a timed-out error; a job that
    exited without a usable result is reported as an error too - never as a
    verdict.

    ``job`` is never mutated: the caller's ``started_at`` stays the real start
    time, so a second poll still sees the original age (and kills/reports
    again) instead of looking freshly started.
    """
    pid = job.get("pid")
    if isinstance(pid, int) and _pid_alive(pid):
        started = _parse_started_at(job)
        age = (_now() - started).total_seconds() if started else None
        # A backwards clock (negative age) and an age exactly at the budget do
        # not time out: only a job strictly older than timeout_s does.
        if age is None or age <= timeout_s:
            return None
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return _timed_out(timeout_s)

    result_path = job.get("result_path")
    if not result_path:
        return _no_result(job)
    try:
        data = json.loads(Path(str(result_path)).read_text())
    except (OSError, ValueError):
        return _no_result(job)
    if not isinstance(data, dict) or data.get("state") not in _VALID_STATES:
        return _no_result(job)
    return data


def cancel_review_job(job: dict) -> None:
    """Best-effort stop of a detached job; never raises.

    The job has a single interpreter child and no agent descendants, so killing
    its process group is safe.
    """
    pid = job.get("pid") if isinstance(job, dict) else None
    if not isinstance(pid, int):
        return
    try:
        alive = _pid_alive(pid)
    except Exception:  # noqa: BLE001 - cancel is best-effort by contract
        return
    if not alive:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        # The process group is already gone (or not signalable): nothing to
        # stop, and a cancel must never raise into the caller's tick.
        return


# ---------------------------------------------------------------------------
# The job body (``python -m pipeline.review_job``)
# ---------------------------------------------------------------------------


def _write_result(result_path: str, result: dict) -> None:
    """Write *result* atomically with mode 0600."""
    directory = Path(result_path).parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".result.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(result, fh)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, result_path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    """Run one detached review and write its result; 0 on a result, else 1.

    Usage: ``python -m pipeline.review_job <plan> <story_key> <result_path>``.
    ``1`` means the job itself failed (``state == "error"``); ``2`` means bad
    invocation (nothing written).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        return 2
    plan_name, story_key, result_path = args
    try:
        _validate_key(plan_name)
        _validate_key(story_key)
    except ValueError:
        return 2

    try:
        # Imported inside the function: pipeline.server pulls in this package's
        # heavy modules, and this module must stay importable on its own.
        import pipeline.server

        # Identity is re-bound here (SYSTEM_SCHEDULER + a fresh correlation id)
        # so the child's records and policy decisions look exactly like the
        # scheduler's own.
        with bind_principal(SYSTEM_SCHEDULER), bind_correlation_id(new_correlation_id()):
            rv = pipeline.server.review_story(plan_name, story_key)
    except Exception as exc:  # noqa: BLE001 - reported by class name, exit 1
        # Class name only: the message can carry filesystem paths or a token.
        _write_result(
            result_path,
            {"state": "error", "error": f"review job raised {type(exc).__name__}"},
        )
        return 1

    _write_result(
        result_path,
        {
            "state": "done",
            "status": rv.get("status"),
            "skipped": rv.get("skipped"),
            "deferred": rv.get("deferred"),
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
