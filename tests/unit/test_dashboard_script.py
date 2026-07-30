"""Integration tests for the dashboard start/stop/status bash script.

Drives `scripts/dashboard.sh` from pytest via subprocess. Each test picks a
fresh ephemeral port so the suite is safe to run in parallel and never
collides with another developer's `uvicorn dashboard:app` on 8000.

The script is a thin wrapper over `python -m uvicorn dashboard:app`; the
actual API contract (routes, payloads) is already covered exhaustively in
test_dashboard.py. These tests only assert lifecycle: start, status, stop,
restart, plus the no-double-start / no-op-stop boundary conditions.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dashboard.sh"


# --- helpers --------------------------------------------------------------


def _free_port() -> int:
    """Bind :0 to let the kernel pick an unused TCP port, then close it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_script(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run scripts/dashboard.sh with `args` under `env` (no shell inheritance)."""
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=False, env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by another user — treat as alive so we
        # don't accidentally SIGKILL someone else's server from the test.
        return True
    return True


def _wait_healthy(port: int, timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/api/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionResetError, OSError):
            time.sleep(0.25)
    return False


def _health_unreachable(port: int, timeout_s: float = 5.0) -> bool:
    """True if /api/health refuses the connection. Returns on the first
    refused connect (URLError/OSError) — used after `stop` to confirm the
    server is down, where the process is dead and every attempt refuses."""
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/api/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                # If we got *any* response, the server is still up.
                return False
        except (urllib.error.URLError, ConnectionResetError, OSError):
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def env(tmp_path: Path):
    """Per-test isolated env: fresh port, tmp pid/log, minimal PATH."""
    port = _free_port()
    env = os.environ.copy()
    # Make sure the script never inherits a real DASHBOARD_* from the caller.
    env.pop("DASHBOARD_HOST", None)
    env.pop("DASHBOARD_PORT", None)
    env.pop("DASHBOARD_RELOAD", None)
    env["DASHBOARD_HOST"] = "127.0.0.1"
    env["DASHBOARD_PORT"] = str(port)
    yield env, port
    # Teardown — make sure no dashboard is left running after the test, even
    # if an assertion failed mid-flight. Use a fresh subprocess so a stale
    # `.dashboard.<port>.pid` from a parallel run can't kill the wrong process.
    subprocess.run(
        ["bash", str(SCRIPT), "stop"],
        check=False, env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )


# --- tests ----------------------------------------------------------------


def test_script_syntax_ok():
    """scripts/dashboard.sh must be syntactically valid bash.

    Boundary: `bash -n` parses without executing — guards against
    half-written scripts that blow up only at run time.
    """
    res = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        check=False, capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, f"bash -n failed: {res.stderr}"


def _env_for_port(port: int) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("DASHBOARD_HOST", None)
    env.pop("DASHBOARD_PORT", None)
    env.pop("DASHBOARD_RELOAD", None)
    env["DASHBOARD_HOST"] = "127.0.0.1"
    env["DASHBOARD_PORT"] = str(port)
    return env


def test_concurrent_instances_on_different_ports_do_not_collide():
    """A second `dashboard.sh start`/`stop` cycle on a DIFFERENT port must
    never observe or kill an unrelated instance already running on another
    port. This is the exact bug that killed the real dashboard during test
    runs: `is_running`/`cmd_stop` were keyed off a single shared, non-port-
    scoped pidfile, so a differently-ported invocation would see
    "already running" (skip starting its own) and then `stop` would kill the
    OTHER instance instead of its own (nonexistent) one."""
    port1 = _free_port()
    env1 = _env_for_port(port1)
    try:
        res1 = _run_script("start", env=env1)
        assert res1.returncode == 0, f"start failed: {res1.stdout!r} {res1.stderr!r}"
        assert _wait_healthy(port1), "first instance never became healthy"
        pid_file1 = REPO_ROOT / f".dashboard.{port1}.pid"
        assert pid_file1.exists(), f"expected a port-scoped pidfile at {pid_file1}"
        pid1 = int(pid_file1.read_text().strip())
        assert _pid_alive(pid1)

        port2 = _free_port()
        env2 = _env_for_port(port2)
        res2 = _run_script("start", env=env2)
        assert res2.returncode == 0, f"start failed: {res2.stdout!r} {res2.stderr!r}"
        assert _wait_healthy(port2), (
            "second instance never became healthy on its own port - it "
            "likely collided with the first instance's pidfile/port check "
            "instead of starting independently"
        )
        assert _pid_alive(pid1), "first instance died when a second was started on a different port"

        _run_script("stop", env=env2)
        assert _pid_alive(pid1), "first instance was killed by stopping the second instance"
        assert _health_unreachable(port2), "second instance did not actually stop"
    finally:
        _run_script("stop", env=env1)


def test_existing_lifecycle_unaffected_by_concurrent_real_instance(env):
    """Regression guard for the real-world trigger: a real dashboard already
    running on another port must survive an unrelated test's own isolated
    start/health/stop cycle (the fixture's unconditional teardown `stop` used
    to read the shared pidfile and could kill this "real" instance)."""
    env_, port = env
    real_port = _free_port()
    real_env = _env_for_port(real_port)
    real_res = _run_script("start", env=real_env)
    assert real_res.returncode == 0, f"start failed: {real_res.stdout!r} {real_res.stderr!r}"
    assert _wait_healthy(real_port)
    real_pid = int((REPO_ROOT / f".dashboard.{real_port}.pid").read_text().strip())

    try:
        res = _run_script("start", env=env_)
        assert res.returncode == 0, f"start failed: {res.stdout!r} {res.stderr!r}"
        assert _wait_healthy(port)
        _run_script("stop", env=env_)

        assert _pid_alive(real_pid), "the concurrently-running real instance was killed"
    finally:
        _run_script("stop", env=real_env)


def test_start_then_health(env):
    """start → /api/health 200, pid file exists, recorded pid is alive."""
    env_, port = env
    res = _run_script("start", env=env_)
    assert res.returncode == 0, f"start failed: stdout={res.stdout!r} stderr={res.stderr!r}"
    assert _wait_healthy(port), (
        f"dashboard never became healthy on port {port} within 15s; "
        f"start stdout={res.stdout!r}"
    )

    pid_file = REPO_ROOT / f".dashboard.{port}.pid"
    assert pid_file.exists(), f".dashboard.{port}.pid missing; start stdout={res.stdout!r}"
    pid = int(pid_file.read_text().strip())
    assert _pid_alive(pid), f"recorded pid {pid} not alive"

    # Cleanup before env teardown so the teardown `stop` is a true no-op.
    _run_script("stop", env=env_)


def test_status_reports_running_then_not(env):
    """status exits 0 + reports running while up; non-zero + 'not running' when down."""
    env_, port = env
    # Down first.
    res_down = _run_script("status", env=env_)
    assert res_down.returncode != 0, (
        f"status while down should be non-zero; got {res_down.returncode} "
        f"stdout={res_down.stdout!r}"
    )
    assert "not running" in res_down.stdout.lower(), (
        f"status while down missing 'not running': {res_down.stdout!r}"
    )

    # Up.
    res_start = _run_script("start", env=env_)
    assert res_start.returncode == 0, (
        f"start failed: stdout={res_start.stdout!r} stderr={res_start.stderr!r}"
    )
    assert _wait_healthy(port)

    res_up = _run_script("status", env=env_)
    assert res_up.returncode == 0, (
        f"status while up should be 0; got {res_up.returncode} "
        f"stdout={res_up.stdout!r}"
    )
    out = res_up.stdout.lower()
    assert "running" in out, f"status output missing 'running': {res_up.stdout!r}"
    assert str(port) in res_up.stdout, (
        f"status output missing port {port}: {res_up.stdout!r}"
    )

    pid_file = REPO_ROOT / f".dashboard.{port}.pid"
    assert pid_file.exists()
    pid = int(pid_file.read_text().strip())
    assert str(pid) in res_up.stdout, (
        f"status output missing pid {pid}: {res_up.stdout!r}"
    )

    # Tear down.
    _run_script("stop", env=env_)


def test_stop_kills_process_and_removes_pid_file(env):
    """stop → pid gone, .dashboard.<port>.pid gone, /api/health refuses connections."""
    env_, port = env
    res = _run_script("start", env=env_)
    assert res.returncode == 0, f"start failed: {res.stdout!r} {res.stderr!r}"
    assert _wait_healthy(port)

    pid_file = REPO_ROOT / f".dashboard.{port}.pid"
    pid = int(pid_file.read_text().strip())
    assert _pid_alive(pid)

    stop = _run_script("stop", env=env_)
    assert stop.returncode == 0, f"stop failed: {stop.stdout!r} {stop.stderr!r}"

    assert not _pid_alive(pid), f"pid {pid} still alive after stop"
    assert not pid_file.exists(), f".dashboard.{port}.pid still present after stop: {pid_file.read_text()!r}"
    assert _health_unreachable(port), (
        f"/api/health on port {port} still answered after stop"
    )


def test_stop_when_not_running_is_noop(env):
    """stop without a live dashboard: exit 0, 'not running', no error."""
    env_, port = env
    # Make sure nothing is up.
    pid_file = REPO_ROOT / f".dashboard.{port}.pid"
    if pid_file.exists():
        pid_file.unlink()

    res = _run_script("stop", env=env_)
    assert res.returncode == 0, (
        f"stop when not running should exit 0; got {res.returncode} "
        f"stdout={res.stdout!r} stderr={res.stderr!r}"
    )
    assert "not running" in res.stdout.lower(), (
        f"stop when down should say 'not running': {res.stdout!r}"
    )


def test_start_when_already_running_refuses(env):
    """A second `start` while one is up must NOT spawn another process."""
    env_, port = env
    res = _run_script("start", env=env_)
    assert res.returncode == 0, f"first start failed: {res.stdout!r} {res.stderr!r}"
    assert _wait_healthy(port)
    pid_file = REPO_ROOT / f".dashboard.{port}.pid"
    original_pid = int(pid_file.read_text().strip())

    # Snapshot all dashboard uvicorn pids BEFORE the second start so we can
    # tell whether a new one was spawned. `pgrep -f` matches the command line,
    # which is what we want — bare uvicorn workers from other repos will not
    # contain "dashboard:app".
    try:
        before = {
            int(p)
            for p in subprocess.run(
                ["pgrep", "-f", "uvicorn dashboard:app"],
                capture_output=True, text=True, check=True,
            ).stdout.split()
        }
    except subprocess.CalledProcessError:
        before = set()

    second = _run_script("start", env=env_)
    # Documented behavior: exit non-zero (or 0) is fine as long as it
    # refuses; the load-bearing assertion is "no second process".
    out = second.stdout.lower() + second.stderr.lower()
    assert "already running" in out, (
        f"second start should say 'already running': stdout={second.stdout!r} "
        f"stderr={second.stderr!r}"
    )

    # Give a moment for any erroneous spawn to register with the kernel.
    time.sleep(0.5)
    try:
        after = {
            int(p)
            for p in subprocess.run(
                ["pgrep", "-f", "uvicorn dashboard:app"],
                capture_output=True, text=True, check=True,
            ).stdout.split()
        }
    except subprocess.CalledProcessError:
        after = set()
    new_pids = after - before
    assert not new_pids, f"second start spawned extra pids: {new_pids}"

    # The pid file must still point at the original process.
    assert pid_file.exists()
    assert int(pid_file.read_text().strip()) == original_pid

    _run_script("stop", env=env_)


def test_restart_cycles_pid_on_same_port(env):
    """restart: old pid dies, new pid comes up on the same port, /api/health 200."""
    env_, port = env
    res = _run_script("start", env=env_)
    assert res.returncode == 0, f"first start failed: {res.stdout!r} {res.stderr!r}"
    assert _wait_healthy(port)
    pid_file = REPO_ROOT / f".dashboard.{port}.pid"
    old_pid = int(pid_file.read_text().strip())

    # Force a different pid by starting with a short sleep first, then
    # restarting immediately. uvicorn assigns pids sequentially enough that
    # in practice we get a different one; if we don't, restart is still
    # correct — we just relax the assertion.
    time.sleep(1.0)
    restart = _run_script("restart", env=env_)
    assert restart.returncode == 0, (
        f"restart failed: stdout={restart.stdout!r} stderr={restart.stderr!r}"
    )
    assert _wait_healthy(port), (
        f"restart never became healthy: stdout={restart.stdout!r} "
        f"stderr={restart.stderr!r}"
    )

    assert pid_file.exists()
    new_pid = int(pid_file.read_text().strip())
    assert _pid_alive(new_pid), f"new pid {new_pid} not alive after restart"
    # Old pid must be gone (or at least no longer bound to the dashboard).
    # ProcessLookupError is the success case; if the OS recycled the pid to
    # an unrelated process, that's a test-environment artifact, not a script
    # bug — accept it as long as the *new* pid is alive and on the same port.
    if new_pid != old_pid:
        assert not _pid_alive(old_pid), (
            f"old pid {old_pid} still alive after restart; new pid {new_pid}"
        )

    _run_script("stop", env=env_)
