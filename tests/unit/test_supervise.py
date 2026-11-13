"""The container supervisor owns the three long-running children.

``pipeline.supervise`` replaces the "wait for the first child to exit" loop
in ``deploy/entrypoint.sh``. Under the old loop one dying child took the whole
container down and left the rest of the service half-dead; under the
supervisor a child that exits is logged and restarted after a bounded backoff,
and a stop signal always ends PID 1.

These tests drive the supervisor as a process, with fake children built from
``sys.executable -c '...'``, so the graded behaviour is the observable one:
when a child starts, when it is restarted, when it is signalled, and when the
supervisor itself exits. Nothing here reads the supervisor's source.

The supervisor is configured entirely through the environment, which is how
the rest of the deployment is configured (``DASHBOARD_PORT``,
``PIPELINE_SCHEDULER_DAEMON``, ``SHUTDOWN_GRACE_SECONDS``):

``SUPERVISE_CHILDREN``
    JSON list of the children to run. Each entry is a JSON object with a
    ``name`` (used in the log lines) and an ``argv`` (the command as a list of
    strings). When it is unset the supervisor runs the three production
    children (dashboard, MCP transport, scheduler daemon).
``SUPERVISE_BACKOFF_SECONDS``
    The first restart delay. The delay doubles per consecutive failure and is
    capped, so a crash loop cannot spin.
``SUPERVISE_BACKOFF_MAX_SECONDS``
    The cap for the doubling backoff.
``SUPERVISE_RESET_SECONDS``
    A child that stayed up this long counts as healthy: its backoff is reset
    to the first delay again.
``SHUTDOWN_GRACE_SECONDS``
    How long a child may take to exit after SIGTERM before it is SIGKILLed.

No fixed public port is used anywhere: the fake children only ever write to
files under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from itertools import pairwise
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SUPERVISE = (sys.executable, str(REPO_ROOT / "pipeline" / "supervise.py"))

# A child that starts, records itself and then waits. The fake children never
# open a socket: the ports in the dashboard/MCP command lines are irrelevant to
# what is being graded here.
_RECORD_AND_SLEEP = """
import os, signal, sys, time
state = sys.argv[1]
with open(state, "a", encoding="utf-8") as handle:
    handle.write("start %d %.6f\\n" % (os.getpid(), time.time()))
    handle.flush()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(0.05)
"""

# A child that ignores SIGTERM entirely: only SIGKILL stops it.
_IGNORE_SIGTERM = """
import os, signal, sys, time
state = sys.argv[1]
with open(state, "a", encoding="utf-8") as handle:
    handle.write("start %d %.6f\\n" % (os.getpid(), time.time()))
    handle.flush()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGINT, signal.SIG_IGN)
while True:
    time.sleep(0.05)
"""

# A child that exits immediately, every time it is started.
_EXIT_IMMEDIATELY = """
import os, sys, time
state = sys.argv[1]
with open(state, "a", encoding="utf-8") as handle:
    handle.write("start %d %.6f\\n" % (os.getpid(), time.time()))
    handle.flush()
sys.exit(3)
"""


def _children(spec: list[dict[str, object]]) -> str:
    return json.dumps(spec)


def _child(name: str, script: str, state: Path) -> dict[str, object]:
    return {"name": name, "argv": [sys.executable, "-c", script, str(state)]}


def _env(tmp_path: Path, children: list[dict[str, object]], **extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "SUPERVISE_CHILDREN": _children(children),
    }
    env.update(extra)
    return env


def _start(env: dict[str, str]) -> subprocess.Popen[str]:
    """Start the supervisor with its own process group.

    The group is used so a test can signal the supervisor alone; the fake
    children are in the same group, so a group-wide signal would reach them
    as well, which is exactly what the supervisor must not rely on.
    """
    return subprocess.Popen(
        list(SUPERVISE),
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _starts(state: Path) -> list[float]:
    """The timestamps of every recorded child start, in order.

    Each line is ``start <pid> <unix-time>``; the timestamp is what the backoff
    tests measure, the pid is what the shutdown tests check for liveness.
    """
    if not state.exists():
        return []
    stamps = []
    for line in state.read_text(encoding="utf-8").splitlines():
        if not line.startswith("start "):
            continue
        stamps.append(float(line.split()[2]))
    return stamps


def _wait_for_starts(state: Path, count: int, timeout: float) -> list[float]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stamps = _starts(state)
        if len(stamps) >= count:
            return stamps
        time.sleep(0.05)
    return _starts(state)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate(proc: subprocess.Popen[str], timeout: float = 15.0) -> None:
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        proc.kill()


def test_supervisor_restarts_a_child_that_exits_and_logs_the_restart(tmp_path: Path) -> None:
    """A child that exits once is started again, and the restart is logged."""
    state = tmp_path / "child.state"
    env = _env(
        tmp_path,
        [_child("fake", _EXIT_IMMEDIATELY, state)],
        SUPERVISE_BACKOFF_SECONDS="0.2",
        SUPERVISE_BACKOFF_MAX_SECONDS="0.2",
    )
    proc = _start(env)
    try:
        stamps = _wait_for_starts(state, 2, timeout=20.0)
        assert len(stamps) >= 2, (
            "the supervisor did not restart the child that exited; "
            f"starts recorded: {stamps!r}"
        )
        assert proc.poll() is None, (
            "the supervisor exited instead of restarting the dead child; "
            f"exit status {proc.returncode!r}"
        )
        output = _drain(proc)
        assert "fake" in output, (
            f"the supervisor did not name the child it restarted: {output!r}"
        )
        assert "restart" in output.lower(), (
            f"the supervisor did not log the restart: {output!r}"
        )
    finally:
        _terminate(proc)


def test_supervisor_exits_on_sigterm_within_the_grace_period(tmp_path: Path) -> None:
    """SIGTERM ends the supervisor, and the children stop with it."""
    state = tmp_path / "child.state"
    grace = "5"
    env = _env(tmp_path, [_child("fake", _RECORD_AND_SLEEP, state)], SHUTDOWN_GRACE_SECONDS=grace)
    proc = _start(env)
    try:
        stamps = _wait_for_starts(state, 1, timeout=20.0)
        assert stamps, "the supervisor never started the child"
        child_pid = int(state.read_text(encoding="utf-8").split()[1])

        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        try:
            status = proc.wait(timeout=float(grace) + 10.0)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "the supervisor did not exit after SIGTERM within "
                f"{float(grace) + 10.0:.1f}s"
            )
        elapsed = time.monotonic() - started

        assert elapsed <= float(grace) + 5.0, (
            f"the supervisor took {elapsed:.1f}s to exit after SIGTERM, which is "
            f"longer than the {grace}s grace period plus a small epsilon"
        )
        assert status == 0, (
            f"a signalled supervisor must exit 0, got {status!r}"
        )
        deadline = time.monotonic() + 5.0
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_alive(child_pid), (
            f"the child {child_pid} was still running after the supervisor exited"
        )
    finally:
        _terminate(proc)


def test_supervisor_sigkills_a_child_that_ignores_sigterm(tmp_path: Path) -> None:
    """A child that ignores SIGTERM is SIGKILLed, and its pid is gone."""
    state = tmp_path / "child.state"
    grace = "2"
    env = _env(
        tmp_path,
        [_child("stubborn", _IGNORE_SIGTERM, state)],
        SHUTDOWN_GRACE_SECONDS=grace,
    )
    proc = _start(env)
    try:
        stamps = _wait_for_starts(state, 1, timeout=20.0)
        assert stamps, "the supervisor never started the child"
        child_pid = int(state.read_text(encoding="utf-8").split()[1])
        assert _pid_alive(child_pid), "the child was not running before the signal"

        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        try:
            status = proc.wait(timeout=float(grace) + 15.0)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "the supervisor did not exit after SIGTERM; it must SIGKILL the "
                "child that ignores SIGTERM"
            )
        elapsed = time.monotonic() - started

        assert elapsed <= float(grace) + 10.0, (
            f"the supervisor took {elapsed:.1f}s to exit, longer than the {grace}s "
            "grace period plus the time needed to kill a straggler"
        )
        assert status == 0, (
            f"a signalled supervisor must exit 0, got {status!r}"
        )
        deadline = time.monotonic() + 5.0
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_alive(child_pid), (
            f"the child {child_pid} survived the supervisor's shutdown; it must "
            "be SIGKILLed and reaped"
        )
    finally:
        _terminate(proc)


def test_supervisor_backs_off_between_restarts(tmp_path: Path) -> None:
    """A child that always dies is not restarted faster than the backoff."""
    state = tmp_path / "child.state"
    backoff = "1.5"
    env = _env(
        tmp_path,
        [_child("crasher", _EXIT_IMMEDIATELY, state)],
        SUPERVISE_BACKOFF_SECONDS=backoff,
        SUPERVISE_BACKOFF_MAX_SECONDS=backoff,
    )
    proc = _start(env)
    try:
        time.sleep(6.0)
        stamps = _starts(state)
        assert stamps, "the supervisor never started the child"
        gaps = [b - a for a, b in pairwise(stamps)]
        assert gaps, "the child was never restarted"
        assert min(gaps) >= float(backoff) * 0.8, (
            "the supervisor restarted the crashing child faster than the "
            f"backoff of {backoff}s; gaps: {gaps!r}"
        )
        # A bounded backoff cannot spin: the number of restarts in a fixed
        # window has to stay small.
        assert len(stamps) <= 6, (
            f"the supervisor restarted the crashing child {len(stamps)} times in "
            f"6s; the backoff is not bounding the restart rate: {gaps!r}"
        )
    finally:
        _terminate(proc)


# A flaky child: runs 0 and 1 exit at once, run 2 stays up longer than
# SUPERVISE_RESET_SECONDS before exiting, and every later run exits at once.
# Two consecutive failures first make the un-reset backoff 2s, so the third,
# healthy exit is where a reset is observable: its restart waits the first
# delay (0.5s) instead of the grown one (2s).
_RESET_AFTER_A_HEALTHY_RUN = """
import os, sys, time
state = sys.argv[1]
try:
    lines = open(state, encoding="utf-8").read().splitlines()
    runs = len(lines)
except FileNotFoundError:
    runs = 0
with open(state, "a", encoding="utf-8") as handle:
    handle.write("start %d %.6f\\n" % (os.getpid(), time.time()))
    handle.flush()
if runs == 2:
    time.sleep(1.5)
sys.exit(3)
"""


def test_supervisor_resets_the_backoff_after_a_healthy_child(tmp_path: Path) -> None:
    """A child that stayed up long enough gets the first delay again."""
    state = tmp_path / "child.state"
    env = _env(
        tmp_path,
        [_child("flaky", _RESET_AFTER_A_HEALTHY_RUN, state)],
        SUPERVISE_BACKOFF_SECONDS="0.5",
        SUPERVISE_BACKOFF_MAX_SECONDS="30",
        SUPERVISE_RESET_SECONDS="1",
    )
    proc = _start(env)
    try:
        stamps = _wait_for_starts(state, 5, timeout=40.0)
        assert len(stamps) == 5, (
            f"the supervisor did not restart the flaky child five times: {stamps!r}"
        )
        gaps = [b - a for a, b in pairwise(stamps)]
        # start1 -> start2 is the first failure's 0.5s delay.
        assert gaps[0] >= 0.4, (
            f"the supervisor restarted the child faster than the backoff: {gaps!r}"
        )
        # start3 -> start4 follows the healthy run (1.5s uptime). Without the
        # reset the grown 2s delay would apply (>=3.4s); with it the first
        # delay (0.5s) applies, so ~2.0s. The 3.0s bound leaves room for
        # runner scheduling overhead while staying below the un-reset 3.4s.
        assert gaps[2] < 3.0, (
            "a healthy run must reset the backoff to the first delay: "
            f"{gaps!r}"
        )
        # start4 -> start5 follows the immediate exit after that reset: the
        # doubled first delay (1s), never the un-reset growth (4s). The 3.0s
        # bound leaves room for runner scheduling overhead below that 4s.
        assert gaps[3] < 3.0, (
            "the restarted backoff must grow from the first delay again: "
            f"{gaps!r}"
        )
    finally:
        _terminate(proc)


def test_supervisor_reports_a_missing_configuration(tmp_path: Path) -> None:
    """An unusable child specification fails loudly instead of idling."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "SUPERVISE_CHILDREN": "{not json",
    }
    proc = subprocess.Popen(
        list(SUPERVISE),
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            status = proc.wait(timeout=20.0)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "the supervisor stayed alive with an unusable child "
                "specification; it must fail instead of idling"
            )
        output = _drain(proc)
        assert status != 0, (
            f"the supervisor exited 0 with an unusable child specification: {output!r}"
        )
        assert output.strip(), "the supervisor failed without saying why"
    finally:
        _terminate(proc)


def _drain(proc: subprocess.Popen[str]) -> str:
    """The supervisor's output so far, without blocking forever."""
    if proc.stdout is None:  # pragma: no cover - defensive
        return ""
    try:
        proc.stdout.flush()
    except ValueError:  # pragma: no cover - defensive
        pass
    import fcntl

    flags = fcntl.fcntl(proc.stdout, fcntl.F_GETFL)
    fcntl.fcntl(proc.stdout, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    chunks = []
    try:
        while True:
            # os.read keeps the codec layer out of the non-blocking path: a
            # text-mode read on a stream that has no data raises
            # "can't concat NoneType to bytes" inside the incremental decoder.
            try:
                chunk = os.read(proc.stdout.fileno(), 65536)
            except BlockingIOError:  # pragma: no cover - the normal end
                break
            if not chunk:
                break
            chunks.append(chunk.decode("utf-8", "replace"))
    except (BlockingIOError, ValueError):  # pragma: no cover - defensive
        pass
    finally:
        try:
            fcntl.fcntl(proc.stdout, fcntl.F_SETFL, flags)
        except (OSError, ValueError):  # pragma: no cover - defensive
            pass
    return "".join(chunks)
