"""The supervisor reaps every orphan reparented to it, and only collects.

``pipeline/supervise.py`` is PID 1 in the pipeline container, so every
grandchild whose parent died - an agent killed at its wall-clock limit
orphans its ``git``/``sh``/``python`` children - is reparented to it. On
2026-11-10 those zombies reached 45,518 (43,743 of them ``git``) and filled
the container's thread table, so ``worker.start()`` raised
``RuntimeError: can't start new thread`` until the container was restarted.

The fix is one reap point per main-loop iteration: each live direct child is
collected by its own pid first, and only then is ``waitpid(-1, WNOHANG)``
swept for reparented orphans. The order is what preserves the exit status:
``Popen`` turns ``ECHILD`` ("someone else already collected this child") into
exit status 0, so a bare ``waitpid(-1)`` loop would report a child that died
of SIGKILL as a clean exit and hide the death from the restart log.

These tests drive ``Supervisor`` in process - ``Supervisor._tick()`` is one
main-loop iteration - so what they assert is the process table the supervisor
leaves behind. A test process is not PID 1, so the orphan tests turn it into a
child subreaper (``PR_SET_CHILD_SUBREAPER``); that is what makes an orphaned
grandchild reparent to the test process exactly as it reparents to the
container's PID 1, which is the production condition being reproduced.

Nothing here signals a process the supervisor spawned beyond the test's own
teardown, and no test touches ``tests/unit/test_supervise.py``'s black-box
behaviour: restart, backoff, reset-after and shutdown grace stay pinned there.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from pipeline.supervise import Child, Supervisor

# prctl(PR_SET_CHILD_SUBREAPER, 1): our orphans reparent to us, not to PID 1.
PR_SET_CHILD_SUBREAPER = 36
HAVE_WAITID = hasattr(os, "waitid")

# A child that records that it ran and then dies with a non-zero status. The
# status is the probe: 7 must survive the reaper, 0 would mean ECHILD leaked.
_EXIT_7 = """
import pathlib, sys
pathlib.Path(sys.argv[1]).write_text("exited", encoding="utf-8")
raise SystemExit(7)
"""

# The parent shells out a grandchild, then exits immediately: the grandchild is
# reparented while it is still running, which is what an agent killed at its
# wall-clock limit does to its git/sh children. The inner command is
# single-quoted so ``$$`` is expanded by the *grandchild* shell, not by the
# parent: the pid file must name the process that is reparented to us.
_ORPHANING_PARENT = "sh -c 'echo $$ > {pidfile}; sleep {seconds}' & exit 0"


def _supervisor(*children: tuple[str, list[str]], backoff: float = 3600.0) -> Supervisor:
    """A supervisor whose restart delay no test has to wait out."""
    return Supervisor(
        list(children),
        backoff=backoff,
        backoff_max=backoff,
        reset_after=0.0,
        shutdown_grace=1.0,
        poll_interval=0.01,
    )


def _spawn(supervisor: Supervisor, name: str) -> Child:
    """Start one configured child, as ``run()`` does before its first tick."""
    child = next(child for child in supervisor._children if child.name == name)
    supervisor._spawn(child)
    assert child.process is not None, f"{name} did not start"
    return child


def _tick_until(
    supervisor: Supervisor, done: Callable[[], bool], timeout: float = 20.0
) -> None:
    """Tick the main loop until ``done()`` holds, without touching the pids."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        supervisor._tick()
        if done():
            return
        time.sleep(0.01)
    raise AssertionError("the supervisor never reached the expected state")


def _stop(supervisor: Supervisor) -> None:
    """Teardown: collect this test's children so no zombie survives it."""
    for child in supervisor._children:
        process = child.process
        if process is None:
            continue
        if process.poll() is None:
            process.kill()
        process.wait()
        child.process = None


def _drain() -> None:
    """Collect anything already dead, so a sweep assertion is about this test."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:  # children exist, none has exited
            return


def _assert_collected(*pids: int) -> None:
    """``waitpid`` finds no child of ours left, dead or alive, at these pids."""
    for pid in pids:
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)


def _assert_nothing_left_to_reap() -> None:
    """A bare ``waitpid(-1)`` finds no zombie: the sweep emptied the table.

    A pid of 0 only means some other child of the test process is still
    running, which is not what this asserts; a real pid means an exited child
    - ours - was left uncollected.
    """
    try:
        pid, status = os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        return
    if pid == 0:
        return
    raise AssertionError(f"pid {pid} (status {status}) was left a zombie")


def _set_child_subreaper(enabled: bool) -> bool:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_CHILD_SUBREAPER, int(enabled), 0, 0, 0) == 0
    except (OSError, AttributeError):  # pragma: no cover - non-Linux
        return False


@pytest.fixture
def subreaper() -> Iterator[None]:
    """Make this process the orphans' new parent, as PID 1 is in the container."""
    if not _set_child_subreaper(True):
        pytest.skip("PR_SET_CHILD_SUBREAPER is unavailable on this platform")
    try:
        yield
    finally:
        _drain()
        _set_child_subreaper(False)


def _proc_stat(pid: int) -> tuple[str, int] | None:
    """``(state, ppid)`` for ``pid`` from /proc, or None once it is gone."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = raw.rsplit(")", 1)[-1].split()
    if len(fields) < 2:
        return None
    return fields[0], int(fields[1])


def _wait_for_exit(pid: int, timeout: float = 20.0) -> None:
    """Block until ``pid`` has exited, *without* collecting its status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if HAVE_WAITID:
            try:
                info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                info = None
            if info is not None:
                return
        stat = _proc_stat(pid)
        if stat is not None and stat[0] == "Z":
            return
        time.sleep(0.01)
    raise AssertionError(f"pid {pid} never exited")


def _wait_for_ppid(pid: int, ppid: int, timeout: float = 20.0) -> None:
    """Block until ``pid`` is a child of ``ppid``: the orphan has reparented."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stat = _proc_stat(pid)
        if stat is not None and stat[1] == ppid:
            return
        time.sleep(0.01)
    raise AssertionError(f"pid {pid} was never reparented to {ppid}")


def _read_pid(pidfile: Path, timeout: float = 20.0) -> int:
    """The pid the grandchild writes before it starts sleeping."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pidfile.exists():
            text = pidfile.read_text(encoding="utf-8").strip()
            if text.isdigit() and int(text) > 0:
                return int(text)
        time.sleep(0.01)
    raise AssertionError(f"{pidfile} never held a pid")


def test_direct_child_death_keeps_its_status_and_is_restarted(tmp_path: Path) -> None:
    """A child that exits 7 is recorded as 7 - never as the reaper's ECHILD 0."""
    marker = tmp_path / "exited"
    supervisor = _supervisor(("fake", [sys.executable, "-c", _EXIT_7, str(marker)]), backoff=0.0)
    try:
        child = _spawn(supervisor, "fake")
        first = child.process.pid
        _tick_until(supervisor, lambda: child.status is not None)
        assert child.status == 7

        _tick_until(supervisor, lambda: child.process is None)
        _tick_until(
            supervisor,
            lambda: child.process is not None and child.process.pid != first,
        )
        assert child.status is None  # cleared by the restart, as before
    finally:
        _stop(supervisor)
        _drain()


def test_orphaned_grandchild_leaves_no_zombie_after_one_tick(
    tmp_path: Path, subreaper: object
) -> None:
    """A grandchild that outlives its parent is collected by the same tick."""
    _drain()
    pidfile = tmp_path / "grandchild.pid"
    script = _ORPHANING_PARENT.format(pidfile=pidfile, seconds=0.2)
    supervisor = _supervisor(("parent", ["sh", "-c", script]))
    try:
        parent = _spawn(supervisor, "parent").process.pid
        grandchild = _read_pid(pidfile)
        _wait_for_ppid(grandchild, os.getpid())
        _wait_for_exit(grandchild)

        supervisor._tick()

        _assert_collected(parent, grandchild)
        _assert_nothing_left_to_reap()
    finally:
        _stop(supervisor)
        _drain()


def test_orphan_exiting_after_the_tick_is_reaped_on_the_next_tick(
    tmp_path: Path, subreaper: object
) -> None:
    """An orphan still running at one tick is left alone, then reaped later."""
    _drain()
    pidfile = tmp_path / "grandchild.pid"
    script = _ORPHANING_PARENT.format(pidfile=pidfile, seconds=2.0)
    supervisor = _supervisor(("parent", ["sh", "-c", script]))
    try:
        parent = _spawn(supervisor, "parent").process.pid
        grandchild = _read_pid(pidfile)
        _wait_for_ppid(grandchild, os.getpid())

        supervisor._tick()
        _assert_collected(parent)
        # Still running: nothing to collect yet, and nothing is signalled.
        assert os.waitpid(grandchild, os.WNOHANG) == (0, 0)

        _wait_for_exit(grandchild)
        supervisor._tick()

        _assert_collected(grandchild)
        _assert_nothing_left_to_reap()
    finally:
        _stop(supervisor)
        _drain()


def test_tick_with_no_children_raises_nothing() -> None:
    """Zero children: the sweep finds no child of ours and simply stops."""
    _drain()
    supervisor = _supervisor()
    supervisor._tick()
    assert supervisor._children == []
    _assert_nothing_left_to_reap()


def test_tick_with_no_exits_raises_nothing() -> None:
    """A tick over live children collects nothing and disturbs nothing."""
    _drain()
    supervisor = _supervisor(("idle", ["sh", "-c", "sleep 30"]))
    try:
        child = _spawn(supervisor, "idle")
        pid = child.process.pid
        supervisor._tick()
        supervisor._tick()
        assert child.status is None
        assert child.process is not None and child.process.pid == pid
        assert os.waitpid(pid, os.WNOHANG) == (0, 0)
    finally:
        _stop(supervisor)
        _drain()


def test_unrelated_running_child_is_neither_reaped_nor_signalled() -> None:
    """The loop sweeps the table but only ever collects: a stranger survives."""
    _drain()
    stranger = subprocess.Popen(["sh", "-c", "sleep 30"], stdin=subprocess.DEVNULL)
    supervisor = _supervisor(("kept", ["sh", "-c", "sleep 30"]))
    try:
        kept = _spawn(supervisor, "kept")
        for _ in range(3):
            supervisor._tick()
            time.sleep(0.02)
        assert stranger.poll() is None, "the reaper signalled a process it did not spawn"
        assert os.waitpid(stranger.pid, os.WNOHANG) == (0, 0)
        assert kept.status is None
        assert kept.process is not None and kept.process.poll() is None
    finally:
        _stop(supervisor)
        stranger.kill()
        stranger.wait()
        _drain()
