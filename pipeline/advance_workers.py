"""Supervised pool of long-lived advance workers.

Why a pool: the reconcile pool in ``pipeline/advance_pool.py`` waits for EVERY
child before its pass ends, so the slowest plan in the fleet sets the latency
of every other plan. Here ``size`` threads each own one long-lived
``pipeline.advance_worker`` child and pull plans from the ``AdvanceQueue``:
each plan goes round again as soon as its own advance finishes, regardless of
what the other plans are doing.

Why one long-lived child per slot: ``pipeline.server`` costs 0.9-1.4 s to
import and a worker pays it once, so per-plan cost drops to the advance itself.

Deadlines still bite: the result line is read under ``select`` bounded by
``plan_timeout_s()``, which is called per advance so an operator override
applies without a restart. An over-deadline child is terminated, which releases
its plan ``flock`` by process death. A child that dies or recycles itself is
reaped and dropped; the next plan spawns a fresh one. A plan whose advance
failed is NOT retried here -- the next queue request or reconcile sweep
re-queues it.

Security invariant: every child spawns with ``cwd`` set to this control plane's
own source root -- the directory containing the ``pipeline`` package -- and
never a story worktree. The scheduler runs as ``python -m ...`` from its
checkout, so ``-m`` puts the cwd first on ``sys.path``; a child started inside a
worktree of this repository would import that branch's unreviewed ``pipeline``
package and run it as the scheduler. The plan name travels as a protocol field
and is validated with ``pipeline.parsers._validate_key`` before use.

The child's process group is deliberately shared: agents it launches must
survive it, so there is no ``start_new_session`` and no ``os.killpg`` here.
"""

from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pipeline
from pipeline.advance_queue import AdvanceQueue

__all__ = ["AdvanceWorkerPool"]

logger = logging.getLogger(__name__)

#: Seconds a child gets to exit after termination before it is killed outright.
_KILL_GRACE_SECONDS = 10.0
#: ``Queue.take`` wait; also bounds how promptly stop() is noticed.
_TAKE_TIMEOUT_S = 1.0
#: Read size for a result line (a result payload is a few hundred bytes).
_READ_CHUNK = 65536


def _source_root() -> Path:
    """Return the directory holding this control plane's ``pipeline`` package."""
    return Path(pipeline.__file__).resolve().parent.parent


def _worker_argv(in_fd: int, out_fd: int) -> list[str]:
    """Return the command line for one advance worker child (replaced in tests)."""
    return [sys.executable, "-m", "pipeline.advance_worker", str(in_fd), str(out_fd)]


def _close_fd(fd: int | None) -> None:
    """Close *fd*, ignoring a descriptor that is already closed."""
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


class _ReadTimeout(Exception):
    """The worker produced no complete result line within the deadline."""


class _InvalidResult(Exception):
    """The worker's result line was not a JSON object."""


class _Child:
    """One advance worker child plus the pipe ends the parent keeps."""

    def __init__(self, proc: subprocess.Popen, in_write: int, out_read: int) -> None:
        self.proc = proc
        self._in_write: int | None = in_write
        self._out_read: int | None = out_read
        self._buffer = b""
        self._lock = threading.Lock()

    # -- protocol ---------------------------------------------------------

    def send(self, plan_name: str) -> None:
        """Write one ``{"plan": ...}`` request line to the child."""
        if self._in_write is None:
            raise OSError("advance worker input is closed")
        payload = json.dumps({"plan": plan_name}) + "\n"
        os.write(self._in_write, payload.encode("utf-8"))

    def read_line(self, timeout_s: float) -> str | None:
        """Return the next result line, or ``None`` when the child closed the pipe.

        Raises ``_ReadTimeout`` when no complete line arrives in *timeout_s*.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = self._buffer[:newline]
                self._buffer = self._buffer[newline + 1 :]
                return line.decode("utf-8", "replace")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ReadTimeout
            if self._out_read is None:
                return None
            try:
                ready, _, _ = select.select([self._out_read], [], [], remaining)
            except InterruptedError:
                continue
            except (OSError, ValueError):
                return None
            if not ready:
                raise _ReadTimeout
            try:
                chunk = os.read(self._out_read, _READ_CHUNK)
            except (OSError, ValueError):
                return None
            if not chunk:
                return None
            self._buffer += chunk

    # -- lifecycle --------------------------------------------------------

    def poll(self) -> int | None:
        """Return the child's exit code, or ``None`` while it is running."""
        return self.proc.poll()

    def close_input(self) -> None:
        """Close the parent's write end, so the child reads EOF and exits."""
        with self._lock:
            fd, self._in_write = self._in_write, None
        _close_fd(fd)

    def close(self) -> None:
        """Close both pipe ends this side of the parent owns."""
        self.close_input()
        with self._lock:
            fd, self._out_read = self._out_read, None
        _close_fd(fd)

    def terminate(self) -> int | None:
        """Stop the child and return its exit code (``None`` if it survives).

        Termination (not a process-group kill) keeps the shared process group
        intact, so agents the advance already launched stay alive.
        """
        if self.proc.poll() is not None:
            return self.proc.returncode
        try:
            self.proc.terminate()
            return self.proc.wait(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            return self.proc.poll()
        try:
            self.proc.kill()
        except OSError:
            return self.proc.poll()
        try:
            return self.proc.wait(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return None


class AdvanceWorkerPool:
    """``size`` threads, each owning at most one long-lived advance child."""

    def __init__(
        self,
        size: int,
        queue: AdvanceQueue,
        plan_timeout_s: Callable[[], float],
    ) -> None:
        if size < 1:
            raise ValueError("advance worker pool size must be at least 1")
        self._size = size
        self._queue = queue
        self._plan_timeout_s = plan_timeout_s
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._children: list[_Child] = []
        self._advances = 0
        self._timeouts = 0
        self._respawns = 0
        self._spawns = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start ``size`` daemon worker threads."""
        for index in range(self._size):
            thread = threading.Thread(
                target=self._run,
                name=f"advance-worker-{index}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def stop(self, timeout_s: float = 15.0) -> None:
        """End every worker thread and child; never raises."""
        self._stopping.set()
        try:
            self._stop_workers(timeout_s)
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning(
                "advance worker pool stop failed",
                extra={"error": type(exc).__name__},
            )

    def _stop_workers(self, timeout_s: float) -> None:
        """Drain the queue, end the threads, then kill whatever still runs."""
        try:
            self._queue.close()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning(
                "closing the advance queue failed during pool stop",
                extra={"error": type(exc).__name__},
            )
        with self._lock:
            children = list(self._children)
            threads = list(self._threads)

        # EOF on its input ends a worker child's own loop.
        for child in children:
            child.close_input()

        deadline = time.monotonic() + max(timeout_s, 0.0)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

        for child in children:
            if child.poll() is None:
                logger.warning("terminating an advance worker on pool stop")
                child.terminate()

        # A terminated child's read returns EOF, so the owning thread can now
        # finish its bookkeeping (it still calls ``queue.finish``).
        for thread in threads:
            if thread.is_alive():
                thread.join(1.0)

    def stats(self) -> dict[str, int]:
        """Return the counters since start (advances, timeouts, respawns, spawns)."""
        with self._lock:
            return {
                "advances": self._advances,
                "timeouts": self._timeouts,
                "respawns": self._respawns,
                "spawns": self._spawns,
            }

    # -- worker thread ----------------------------------------------------

    def _run(self) -> None:
        """Serve plans from the queue until the pool stops."""
        child: _Child | None = None
        while not self._stopping.is_set():
            plan = self._queue.take(_TAKE_TIMEOUT_S)
            if plan is None:
                continue
            try:
                child = self._advance(child, plan)
            except Exception as exc:  # noqa: BLE001 - a slot must never die
                logger.warning(
                    "advance worker slot failed",
                    extra={"plan": plan, "error": type(exc).__name__},
                )
                if child is not None:
                    self._drop(child)
                child = None
            finally:
                self._queue.finish(plan)
        if child is not None:
            self._drop(child)

    def _advance(self, child: _Child | None, plan: str) -> _Child | None:
        """Advance *plan* on *child*; return the child to reuse or ``None``."""
        from pipeline.parsers import _validate_key

        try:
            _validate_key(plan)
        except ValueError:
            logger.warning("refusing an invalid plan name", extra={"plan_valid": False})
            return child

        if child is not None and child.poll() is not None:
            self._recycle(child, plan)
            child = None
        if child is None:
            child = self._spawn()

        timeout_s = self._plan_timeout_s()
        try:
            child.send(plan)
            line = child.read_line(timeout_s)
        except _ReadTimeout:
            return self._on_timeout(child, plan, timeout_s)
        except OSError as exc:
            logger.warning(
                "advance worker write failed",
                extra={"plan": plan, "error": type(exc).__name__},
            )
            self._recycle(child, plan)
            return None

        if line is None:
            self._recycle(child, plan)
            return None
        try:
            payload = json.loads(line)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            logger.warning(
                "advance worker returned an unparsable result",
                extra={"plan": plan},
            )
            self._recycle(child, plan)
            return None

        with self._lock:
            self._advances += 1
        return child

    def _spawn(self) -> _Child:
        """Start one worker child rooted at the control plane's source root."""
        child_in_read, child_in_write = os.pipe()
        child_out_read, child_out_write = os.pipe()
        try:
            proc = subprocess.Popen(
                _worker_argv(child_in_read, child_out_write),
                pass_fds=(child_in_read, child_out_write),
                cwd=str(_source_root()),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
        except BaseException:
            for fd in (child_in_read, child_in_write, child_out_read, child_out_write):
                _close_fd(fd)
            raise
        # The child owns its ends from here; the parent keeps only its own.
        _close_fd(child_in_read)
        _close_fd(child_out_write)
        child = _Child(proc, child_in_write, child_out_read)
        with self._lock:
            self._children.append(child)
            self._spawns += 1
        logger.info("advance worker spawned", extra={"pid": proc.pid})
        return child

    def _on_timeout(self, child: _Child, plan: str, timeout_s: float) -> None:
        """Terminate an over-deadline child, count it, and drop it."""
        logger.warning(
            "advance worker timed out",
            extra={"plan": plan, "timeout_s": timeout_s},
        )
        child.close_input()
        child.terminate()
        child.close()
        # Count only once the child is reaped: ``timeouts`` is a published
        # counter, so a reader that sees it incremented must also see the
        # process dead (``poll() is not None``), not merely signalled.
        with self._lock:
            self._timeouts += 1

    def _recycle(self, child: _Child, plan: str) -> None:
        """Reap a child that died or recycled itself, and drop it.

        Exit code 0 is the worker's own recycle signal; anything else is a
        crash worth a warning. The plan is not retried here -- the next queue
        request or reconcile sweep re-queues it.
        """
        exit_code = child.terminate()
        with self._lock:
            self._respawns += 1
        if exit_code == 0:
            logger.info(
                "advance worker recycled",
                extra={"plan": plan, "exit_code": exit_code},
            )
        else:
            logger.warning(
                "advance worker exited unexpectedly",
                extra={"plan": plan, "exit_code": exit_code},
            )
        child.close()

    def _drop(self, child: _Child) -> None:
        """Stop and forget *child* without attributing it to a plan."""
        child.close_input()
        child.terminate()
        child.close()
