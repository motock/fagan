"""Shutdown-bound tests for the remote MCP transport.

The remote transport must disappear on SIGTERM within a bound, even while a
streamable-HTTP session is still open: a wedged event loop must not keep the
process alive forever.  That needs two things from the transport:

* the HTTP serving path must be reachable with an arbitrary ASGI app, so it
  can be driven from a test (a ``run_http(app, host, port)`` helper that
  ``serve()`` calls);
* uvicorn must be given ``timeout_graceful_shutdown`` and a hard-exit
  backstop must be armed when SIGTERM arrives, so a graceful shutdown that
  never returns still ends the process.

The behavioural cases drive a real child process, signal it with ``os.kill``
and measure how long it takes to disappear.  No fixed public port is used: the
parent picks a free loopback port and the child binds that one.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline import remote_transport as rt

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The graceful window the transport must grant in-flight requests.
EXPECTED_GRACEFUL_SECONDS = 10

#: Extra seconds the hard-exit backstop may add on top of the graceful window.
BACKSTOP_EXTRA_SECONDS = 5

#: Case (a): a hanging request must not keep the process past this bound.
EXIT_BOUND_SECONDS = float(EXPECTED_GRACEFUL_SECONDS + BACKSTOP_EXTRA_SECONDS)

#: Case (b): with nothing in flight the process must leave promptly.
PROMPT_EXIT_SECONDS = 5.0

#: Case (c): the backstop fires at the deadline; allow scheduler slack.
BACKSTOP_BOUND_SECONDS = float(EXPECTED_GRACEFUL_SECONDS + BACKSTOP_EXTRA_SECONDS) + 3.0

#: Names the HTTP serving helper may carry; ``run_http`` is the documented one.
RUNNER_NAMES = (
    "run_http",
    "serve_http",
    "run_http_server",
    "http_run",
    "_run_http",
    "_serve_http",
)

TRANSPORT_ENV = "PIPELINE_MCP_TRANSPORT"
HOST_ENV = "PIPELINE_MCP_HOST"
PORT_ENV = "PIPELINE_MCP_PORT"
IDENTITY_PROVIDER_ENV = "PIPELINE_IDENTITY_PROVIDER"

REMOTE_ENV = {
    TRANSPORT_ENV: "streamable-http",
    IDENTITY_PROVIDER_ENV: "oidc",
    "PIPELINE_POLICY_ENFORCE": "1",
}


# --- helpers ---------------------------------------------------------------


def _runner_name() -> str:
    """Return the name of the transport's HTTP serving helper."""
    for name in RUNNER_NAMES:
        if callable(getattr(rt, name, None)):
            return name
    pytest.fail(
        "pipeline.remote_transport must expose a run_http(app, host, port) "
        "helper that serve() calls, so the HTTP serving path can be driven "
        "with an arbitrary ASGI app"
    )


def _free_port() -> int:
    """Bind :0 to let the kernel pick an unused loopback port, then close it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_is_closed(port: int) -> bool:
    """Return whether nothing accepts connections on ``port`` any more."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return False
    except OSError:
        return True


def _log_tail(log: Path) -> str:
    try:
        return log.read_text(errors="replace")[-2000:]
    except OSError:
        return "<no log>"


#: The child program: an arbitrary ASGI app served through the transport's
#: HTTP helper, so the parent can pick the behaviour it needs.
CHILD_PROGRAM = '''\
import asyncio
import inspect
import sys

from pipeline import remote_transport as rt

KIND = sys.argv[1]
PORT = int(sys.argv[2])

NAMES = (
    "run_http",
    "serve_http",
    "run_http_server",
    "http_run",
    "_run_http",
    "_serve_http",
)


def _runner():
    for name in NAMES:
        candidate = getattr(rt, name, None)
        if callable(candidate):
            return candidate
    raise SystemExit("pipeline.remote_transport exposes no HTTP serving helper")


def _call(runner, app, host, port):
    """Call the helper with (app, host, port), adapting to its signature."""
    try:
        params = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        return runner(app, host, port)
    args = [app]
    kwargs = {}
    for name, value in (("host", host), ("port", port)):
        param = params.get(name)
        if param is None:
            continue
        if param.kind is inspect.Parameter.KEYWORD_ONLY:
            kwargs[name] = value
        else:
            args.append(value)
    return runner(*args, **kwargs)


class App:
    """ASGI app whose behaviour the parent selects by kind."""

    def __init__(self, kind):
        self.kind = kind

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    if self.kind == "wedged":
                        # The graceful shutdown can never finish; only a
                        # hard-exit backstop can end this process.
                        await asyncio.sleep(3600)
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        elif scope["type"] == "http":
            if self.kind == "hang":
                await asyncio.sleep(3600)
            await send(
                {"type": "http.response.start", "status": 200, "headers": []}
            )
            await send({"type": "http.response.body", "body": b"ok"})


_call(_runner(), App(KIND), "127.0.0.1", PORT)
'''


def _start_child(tmp_path: Path, kind: str, port: int) -> tuple[subprocess.Popen, Path]:
    """Start the child server and return it with its log path."""
    script = tmp_path / "child.py"
    script.write_text(CHILD_PROGRAM)
    log = tmp_path / "child.log"
    handle = log.open("wb")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(script), kind, str(port)],
            stdout=subprocess.DEVNULL,
            stderr=handle,
            cwd=str(tmp_path),
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": str(REPO_ROOT),
                "PIPELINE_SKIP_ENV_FILE": "1",
                "PYTHONUNBUFFERED": "1",
            },
        )
    finally:
        handle.close()
    return proc, log


def _wait_for_port(port: int, log: Path, timeout: float = 30.0) -> None:
    """Block until the child accepts connections on ``port``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"child never listened on 127.0.0.1:{port}\n{_log_tail(log)}")


def _wait_for_exit(proc: subprocess.Popen, timeout: float) -> int | None:
    """Return the child's exit code, or ``None`` if it outlived ``timeout``."""
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


# --- the shutdown bound ---------------------------------------------------


def test_graceful_shutdown_window_is_ten_seconds() -> None:
    value = getattr(rt, "GRACEFUL_SHUTDOWN_SECONDS", None)
    assert isinstance(value, int) and not isinstance(value, bool), (
        "pipeline.remote_transport must define GRACEFUL_SHUTDOWN_SECONDS"
    )
    assert value == EXPECTED_GRACEFUL_SECONDS


def test_sigterm_exits_within_the_bound_while_a_request_hangs(tmp_path: Path) -> None:
    """A request that never finishes must not keep the process alive."""
    port = _free_port()
    proc, log = _start_child(tmp_path, "hang", port)
    try:
        _wait_for_port(port, log)
        held = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            held.sendall(b"GET /mcp HTTP/1.1\r\nHost: localhost\r\n\r\n")
            time.sleep(0.5)
            start = time.monotonic()
            os.kill(proc.pid, signal.SIGTERM)
            # The graceful window is honoured: the open session is not dropped
            # the instant the signal lands.
            time.sleep(3.0)
            assert proc.poll() is None, (
                f"exited before the graceful window elapsed\n{_log_tail(log)}"
            )
            code = _wait_for_exit(proc, EXIT_BOUND_SECONDS - 3.0)
            elapsed = time.monotonic() - start
        finally:
            held.close()
    finally:
        _kill(proc)
    assert code is not None, (
        f"process still alive {elapsed:.1f}s after SIGTERM\n{_log_tail(log)}"
    )
    assert elapsed <= EXIT_BOUND_SECONDS, f"took {elapsed:.1f}s to exit"
    assert _port_is_closed(port), "the listening port stayed open after exit"


def test_sigterm_exits_promptly_when_no_request_is_open(tmp_path: Path) -> None:
    """With nothing in flight the transport must leave at once."""
    port = _free_port()
    proc, log = _start_child(tmp_path, "quick", port)
    try:
        _wait_for_port(port, log)
        time.sleep(0.3)
        start = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)
        code = _wait_for_exit(proc, PROMPT_EXIT_SECONDS)
        elapsed = time.monotonic() - start
    finally:
        _kill(proc)
    assert code is not None, (
        f"process still alive {elapsed:.1f}s after SIGTERM\n{_log_tail(log)}"
    )
    assert elapsed < PROMPT_EXIT_SECONDS, f"took {elapsed:.1f}s to exit"


def test_hard_exit_backstop_ends_a_wedged_graceful_shutdown(tmp_path: Path) -> None:
    """A graceful shutdown that never returns must still end the process."""
    port = _free_port()
    proc, log = _start_child(tmp_path, "wedged", port)
    try:
        _wait_for_port(port, log)
        time.sleep(0.3)
        start = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)
        code = _wait_for_exit(proc, BACKSTOP_BOUND_SECONDS)
        elapsed = time.monotonic() - start
    finally:
        _kill(proc)
    assert code is not None, (
        f"no hard-exit backstop fired within {BACKSTOP_BOUND_SECONDS:.0f}s\n"
        f"{_log_tail(log)}"
    )
    assert elapsed >= 5.0, (
        f"the graceful path was not wedged (exited after {elapsed:.1f}s)"
    )
    assert elapsed <= BACKSTOP_BOUND_SECONDS, f"took {elapsed:.1f}s to exit"
    assert code != 0, "a wedged shutdown must not exit as a clean shutdown"


# --- the serving path -----------------------------------------------------


def test_serve_gives_uvicorn_the_graceful_shutdown_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uvicorn must be told how long a graceful shutdown may take."""
    import uvicorn

    seen: list[dict[str, Any]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> None:
        seen.append(kwargs)

    class _RecordingConfig(uvicorn.Config):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(uvicorn, "run", _fake_run)
    monkeypatch.setattr(uvicorn, "Config", _RecordingConfig)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: None)
    monkeypatch.setattr(
        rt, "build_identity_provider", lambda *a, **k: SimpleNamespace(network_grade=True)
    )

    rt.serve(SimpleNamespace(middleware=[], streamable_http_app=lambda **k: object()),
             environ=dict(REMOTE_ENV))

    timeouts = [
        kwargs.get("timeout_graceful_shutdown")
        for kwargs in seen
        if kwargs.get("timeout_graceful_shutdown") is not None
    ]
    assert timeouts, f"uvicorn was given no graceful shutdown timeout: {seen}"
    assert timeouts == [rt.GRACEFUL_SHUTDOWN_SECONDS]


def test_serve_routes_through_the_http_serving_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """serve() must reach the HTTP serving path with the configured endpoint."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    name = _runner_name()
    monkeypatch.setattr(rt, name, lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(
        rt, "build_identity_provider", lambda *a, **k: SimpleNamespace(network_grade=True)
    )

    rt.serve(
        SimpleNamespace(middleware=[], streamable_http_app=lambda **k: object()),
        environ={**REMOTE_ENV, HOST_ENV: "0.0.0.0", PORT_ENV: "9123"},
    )

    assert len(calls) == 1, f"serve() must call {name}() exactly once"
    args, kwargs = calls[0]
    assert isinstance(args[0], rt.BearerAuthMiddleware)
    assert "0.0.0.0" in args or kwargs.get("host") == "0.0.0.0"
    assert 9123 in args or kwargs.get("port") == 9123
