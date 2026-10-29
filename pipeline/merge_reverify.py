"""Detached merge-gate re-verify job.

The merge gate re-runs a story's acceptance oracle and build against its
rebased worktree (:func:`pipeline.ci._reverify_acceptance` and
:func:`pipeline.ci._reverify_build`).  Running them inline holds the plan lock
and the whole scheduler pass for the duration of the suite - minutes for a
story with no acceptance fixture, where the gate re-runs the full suite.  This
module moves that work into a detached job: the caller starts it, keeps its
tick, and polls :func:`collect_merge_reverify` for the result.

Two invariants shape the design:

* **A job that dies without a result is a harness error, never a red suite.**
  A missing, unparsable, or unrecognised result reports ``state == "error"``;
  only the job itself can report ``"fail"``, and only for a red gate.
* **The child runs from the control plane's own source root.**  The scheduler
  runs as ``python -m pipeline.scheduler_daemon`` from its checkout, so ``-m``
  puts the cwd first on ``sys.path``.  A child started inside a story worktree
  of this repository would import that branch's unreviewed ``pipeline``
  package; the worktree therefore travels as an argument, never as the cwd.

The job never writes the manifest.  It reports ``last_test_check`` (which
``_reverify_acceptance`` updates on its in-memory copy of the story only) so
the caller can copy it back under the plan lock.
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
from pipeline.parsers import _validate_key

__all__ = [
    "cancel_merge_reverify",
    "collect_merge_reverify",
    "detached_enabled",
    "main",
    "start_merge_reverify",
]

ENV_VAR = "PIPELINE_MERGE_REVERIFY_DETACHED"
LOG_TAIL_CHARS = 2000
_VALID_STATES = frozenset({"pass", "fail", "error"})


def detached_enabled() -> bool:
    """Whether the merge gate should run detached instead of inline.

    False under pytest for the same reason the detached-grade primitives are
    only exported outside pytest (``pipeline/story_status.py``): pre-existing
    merge-gate tests pin the synchronous call sequence of
    ``_reverify_acceptance``/``_reverify_build``.  Operators can also force the
    synchronous path with ``PIPELINE_MERGE_REVERIFY_DETACHED=0``.

    Both inputs are read on every call, so a test (or an operator shell) can
    flip the environment between calls.
    """
    if "pytest" in sys.modules:
        return False
    return os.environ.get(ENV_VAR) != "0"


# ---------------------------------------------------------------------------
# Start / poll / cancel
# ---------------------------------------------------------------------------


def start_merge_reverify(
    plan_name: str, story_key: str, worktree: str, state_root: Path
) -> dict:
    """Spawn the detached job and return its handle.

    Returns ``{"pid", "result_path", "log_path", "started_at"}``; ``started_at``
    is an aware ISO-8601 UTC timestamp the caller feeds back to
    :func:`collect_merge_reverify` for the timeout budget.

    Raises ``ValueError`` (naming the argument, never its value) for a key that
    could traverse a path or a worktree that is not an existing directory.
    """
    try:
        _validate_key(plan_name)
    except ValueError:
        raise ValueError("invalid plan_name") from None
    try:
        _validate_key(story_key)
    except ValueError:
        raise ValueError("invalid story_key") from None
    if not Path(worktree).is_dir():
        raise ValueError("worktree must be an existing directory")

    job_dir = Path(state_root) / "merge_reverify" / story_key
    job_dir.mkdir(parents=True, exist_ok=True)
    # mkdir's mode is filtered by the process umask and does not re-tighten an
    # existing directory, so chmod it explicitly: the result and the log carry
    # story paths and test output.
    os.chmod(job_dir, 0o700)
    result_path = job_dir / "result.json"
    log_path = job_dir / "job.log"
    # Delete any previous result BEFORE the child starts: a stale "pass" left
    # behind by an earlier run must never be mistaken for this run's verdict.
    result_path.unlink(missing_ok=True)

    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pipeline.merge_reverify",
                plan_name,
                story_key,
                worktree,
                str(result_path),
            ],
            cwd=Path(pipeline.__file__).resolve().parent.parent,
            # Give the grading child its own bytecode cache.  The worktree it
            # grades is rebased in place and reused across commits, and CPython
            # validates a .pyc on the source's mtime truncated to whole seconds
            # plus its size - so a rebase that rewrites a test to the same byte
            # length inside one second leaves the worktree's __pycache__ entry
            # acceptable and the child would run the previous commit's compiled
            # test, reporting "pass" for a red suite while naming the new sha.
            # A fresh per-job prefix (removed with the job directory) can never
            # hold an earlier job's pyc, so the child always compiles the bytes
            # actually checked out.
            env={
                **os.environ,
                "PYTHONPYCACHEPREFIX": str(job_dir / "pycache"),
            },
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
        "error": "merge re-verify exited without a result",
        "log_tail": _log_tail(job.get("log_path", "")),
    }


def _timed_out(timeout_s: float) -> dict:
    return {
        "state": "error",
        "error": f"merge re-verify timed out after {timeout_s:g}s",
        "timed_out": True,
    }


def collect_merge_reverify(job: dict, timeout_s: float) -> dict | None:
    """Return the job's result once it finishes, else None.

    ``None`` means "still running, keep polling".  A job past ``timeout_s`` is
    SIGTERM'd as a process group and reported as a timed-out error; a job that
    exited without a usable result is reported as an error too - never as a
    failed gate.

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


def cancel_merge_reverify(job: dict) -> None:
    """Best-effort stop of a detached job; never raises.

    The job has no agent descendants, so killing its process group is safe.
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
# The job body (``python -m pipeline.merge_reverify``)
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
    """Run one detached re-verify and write its result; 0 on a verdict, else 1.

    Usage: ``python -m pipeline.merge_reverify <plan> <story_key> <worktree>
    <result_path>``.  ``0`` covers both a green gate (``state == "pass"``) and a
    red one (``state == "fail"``); ``1`` means the job itself failed
    (``state == "error"``).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 4:
        return 1
    plan_name, story_key, worktree, result_path = args

    result: dict[str, Any] = {
        "state": "pass",
        "error": "",
        "sha": None,
        "last_test_check": None,
    }
    story: dict | None = None
    try:
        # Imported inside the function: pipeline.server pulls in this package's
        # heavy modules, and this module must stay importable on its own.
        from pipeline import server

        manifest = json.loads(server._store.manifest_path(plan_name).read_text())
        story = manifest["stories"][story_key]
        result["sha"] = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        acc = server._reverify_acceptance(story, worktree, story_key)
        # Read AFTER the call: _reverify_acceptance refreshes this field on its
        # in-memory copy of the story, and the caller copies that fresh value
        # back into the manifest under the plan lock.
        result["last_test_check"] = story.get("last_test_check")
        if acc.get("state") == "fail":
            result["state"] = "fail"
            result["error"] = "acceptance reverify fail: " + acc.get("error", "")
        else:
            build = server._reverify_build(worktree)
            if build.get("state") == "fail":
                result["state"] = "fail"
                result["error"] = "build reverify fail: " + build.get("error", "")
    except Exception as exc:  # noqa: BLE001 - the job must always write a result
        # The class name alone: exception text can carry tokens, payloads and
        # paths, and this result is logged and surfaced to operators.
        result["state"] = "error"
        result["error"] = f"merge re-verify raised {type(exc).__name__}"
        if story is not None:
            result["last_test_check"] = story.get("last_test_check")

    _write_result(result_path, result)
    return 0 if result["state"] in {"pass", "fail"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
