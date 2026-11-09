"""Supervise the container's long-running children.

``deploy/entrypoint.sh`` starts exactly one process: this supervisor. It owns
the dashboard, the MCP transport and (usually) the scheduler daemon, so that

* one child dying does not leave the container half-dead: the death is logged
  and the child is restarted after a bounded backoff, and
* a stop signal always ends PID 1: SIGTERM/SIGINT is forwarded to every child,
  stragglers are SIGKILLed after ``SHUTDOWN_GRACE_SECONDS``, everything is
  reaped, and the supervisor exits 0.

The supervisor is configured through the environment, like the rest of the
deployment:

``SUPERVISE_CHILDREN``
    JSON list of ``{"name": ..., "argv": [...]}`` objects. ``--child
    NAME=COMMAND`` (repeatable, COMMAND split like a shell command line) is the
    entrypoint's form and takes precedence. With neither, the production
    children are started: the dashboard and the MCP transport, plus the
    scheduler daemon when ``PIPELINE_SCHEDULER_DAEMON=1``.
``SUPERVISE_BACKOFF_SECONDS`` / ``SUPERVISE_BACKOFF_MAX_SECONDS``
    The first restart delay (default 2s), doubling per consecutive failure up to
    the cap (default 30s), so a crash loop cannot spin.
``SUPERVISE_RESET_SECONDS``
    A child that stayed up this long (default 60s) counts as healthy and its
    backoff starts over.
``SHUTDOWN_GRACE_SECONDS``
    How long children get to exit after the forwarded signal (default 10s).

Invalid configuration fails before anything starts, on stderr, with a non-zero
exit: a supervisor that cannot parse its children must not look healthy.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time

DEFAULT_BACKOFF_SECONDS = 2.0
DEFAULT_BACKOFF_MAX_SECONDS = 30.0
DEFAULT_RESET_SECONDS = 60.0
DEFAULT_SHUTDOWN_GRACE_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.1
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def log(message: str, *args: object) -> None:
    """Write one supervisor log line to stderr, unbuffered."""
    print("supervise: " + (message % args if args else message), file=sys.stderr, flush=True)


def _number(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"supervise: {name} must be a number, got {raw!r}") from None
    if value < 0:
        raise SystemExit(f"supervise: {name} must not be negative, got {raw!r}")
    return value


def _production_children() -> list[dict[str, object]]:
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port = os.environ.get("DASHBOARD_PORT", "8000")
    children: list[dict[str, object]] = [
        {
            "name": "dashboard",
            "argv": ["python", "-m", "uvicorn", "app.dashboard:app", "--host", host, "--port", port],
        },
        {"name": "MCP transport", "argv": ["python", "app/pipeline_mcp_server.py"]},
    ]
    if os.environ.get("PIPELINE_SCHEDULER_DAEMON", "1") == "1":
        children.append(
            {"name": "scheduler daemon", "argv": ["python", "-m", "pipeline.scheduler_daemon"]}
        )
    return children


def _from_args(argv: list[str]) -> list[dict[str, object]]:
    """Parse repeated ``--child NAME=COMMAND`` arguments."""
    children: list[dict[str, object]] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if not arg.startswith("--child"):
            raise SystemExit(f"supervise: unknown argument {arg!r}")
        if "=" in arg and arg.startswith("--child="):
            value = arg.partition("=")[2]
            index += 1
        else:
            if index + 1 >= len(argv):
                raise SystemExit("supervise: --child needs NAME=COMMAND")
            value = argv[index + 1]
            index += 2
        name, separator, command = value.partition("=")
        if not separator or not name or not command.strip():
            raise SystemExit(f"supervise: --child needs NAME=COMMAND, got {value!r}")
        children.append({"name": name, "argv": shlex.split(command)})
    return children


def _from_environment() -> list[dict[str, object]]:
    raw = os.environ.get("SUPERVISE_CHILDREN", "").strip()
    if not raw:
        return _production_children()
    try:
        children = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"supervise: SUPERVISE_CHILDREN is not valid JSON: {exc}") from None
    if not isinstance(children, list) or not children:
        raise SystemExit("supervise: SUPERVISE_CHILDREN must be a non-empty list")
    return children


def _validate(children: list[dict[str, object]]) -> list[tuple[str, list[str]]]:
    validated = []
    for child in children:
        if not isinstance(child, dict):
            raise SystemExit(f"supervise: each child must be an object, got {child!r}")
        name, argv = child.get("name"), child.get("argv")
        if not isinstance(name, str) or not name:
            raise SystemExit(f"supervise: child name must be a non-empty string, got {name!r}")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise SystemExit(f"supervise: {name}'s argv must be a non-empty list of strings")
        validated.append((name, argv))
    return validated


class Child:
    """One supervised process and its restart bookkeeping."""

    def __init__(self, name: str, argv: list[str]) -> None:
        self.name = name
        self.argv = argv
        self.process: subprocess.Popen[bytes] | None = None
        self.started_at = 0.0
        self.restart_at: float | None = None
        self.delay = 0.0


class Supervisor:
    def __init__(
        self,
        children: list[tuple[str, list[str]]],
        *,
        backoff: float,
        backoff_max: float,
        reset_after: float,
        shutdown_grace: float,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._children = [Child(name, argv) for name, argv in children]
        self._backoff = backoff
        self._backoff_max = backoff_max
        self._reset_after = reset_after
        self._shutdown_grace = shutdown_grace
        self._poll_interval = poll_interval
        self._stop_signal: int | None = None

    def run(self) -> int:
        for number in STOP_SIGNALS:
            signal.signal(number, self._request_stop)
        for child in self._children:
            self._spawn(child)
        while self._stop_signal is None:
            self._check_children(time.monotonic())
            if self._stop_signal is None:
                time.sleep(self._poll_interval)
        return self._shutdown()

    def _request_stop(self, number: int, _frame: object) -> None:
        self._stop_signal = number

    def _spawn(self, child: Child) -> None:
        try:
            child.process = subprocess.Popen(
                child.argv, stdin=subprocess.DEVNULL, close_fds=True
            )
        except OSError as exc:
            delay = self._back_off(child, healthy=False)
            log("%s failed to start (%s); restarting in %.1fs", child.name, exc, delay)
            return
        child.started_at = time.monotonic()
        child.restart_at = None
        log("started %s (pid %s)", child.name, child.process.pid)

    def _check_children(self, now: float) -> None:
        for child in self._children:
            if child.process is None:
                if child.restart_at is not None and now >= child.restart_at:
                    self._spawn(child)
                continue
            status = child.process.poll()
            if status is not None:
                child.process = None
                uptime = now - child.started_at
                delay = self._back_off(child, healthy=uptime >= self._reset_after)
                log(
                    "%s exited with status %s after %.1fs; restarting in %.1fs",
                    child.name,
                    status,
                    uptime,
                    delay,
                )

    def _back_off(self, child: Child, *, healthy: bool) -> float:
        """Advance the child's restart delay and schedule its next start."""
        if child.delay == 0.0 or healthy:
            child.delay = self._backoff
        delay = child.delay
        child.delay = min(delay * 2, self._backoff_max)
        child.restart_at = time.monotonic() + delay
        return delay

    def _shutdown(self) -> int:
        number = self._stop_signal or signal.SIGTERM
        log("received %s; stopping %d children", signal.Signals(number).name, len(self._children))
        running = [child for child in self._children if child.process is not None]
        for child in running:
            try:
                child.process.send_signal(number)
            except ProcessLookupError:  # exited between poll and signal
                pass
        deadline = time.monotonic() + self._shutdown_grace
        for child in running:
            try:
                child.process.wait(timeout=max(deadline - time.monotonic(), 0.0))
            except subprocess.TimeoutExpired:
                pass
        for child in running:
            if child.process.poll() is None:
                log(
                    "%s did not exit within %.1fs; killing pid %s",
                    child.name,
                    self._shutdown_grace,
                    child.process.pid,
                )
                child.process.kill()
        for child in running:
            child.process.wait()
            child.process = None
        log("all children reaped; exiting")
        return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    children = _from_args(args) if args else _from_environment()
    supervisor = Supervisor(
        _validate(children),
        backoff=_number("SUPERVISE_BACKOFF_SECONDS", DEFAULT_BACKOFF_SECONDS),
        backoff_max=_number("SUPERVISE_BACKOFF_MAX_SECONDS", DEFAULT_BACKOFF_MAX_SECONDS),
        reset_after=_number("SUPERVISE_RESET_SECONDS", DEFAULT_RESET_SECONDS),
        shutdown_grace=_number(
            "SHUTDOWN_GRACE_SECONDS", DEFAULT_SHUTDOWN_GRACE_SECONDS
        ),
    )
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
