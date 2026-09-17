"""Tests for the repo-root ``Dockerfile`` that packages the MCP server.

awesome-mcp-servers now requires every listed server to also be indexed on
Glama (glama.ai/mcp/servers), which builds the server from a ``Dockerfile``
and runs the standard MCP introspection exchange (``initialize``, then
``tools/list``) against it.  This module grades that packaging story: the
``Dockerfile`` plus proof that a container built from it answers a real
JSON-RPC exchange over stdio.

Two tiers:

1. **Static checks** (always run, no Docker required).  They read the
   ``Dockerfile`` as text and assert the shape the story pins down: the
   ``python:3.12-slim`` base, ``COPY requirements.txt`` before
   ``RUN pip install``, and an exec-form ``CMD`` that runs
   ``app/pipeline_mcp_server.py`` directly (stdio transport, no HTTP
   wrapper).

2. **Live build + probe** (skipped gracefully when Docker is unusable).
   A real ``docker build`` must succeed -- a build failure is a defect, so
   that path FAILS rather than skips -- and the resulting container must
   answer ``initialize`` and ``tools/list`` on stdin/stdout.

These tests are RED until ``Dockerfile`` exists at the repo root (the
static checks fail loudly with a "not implemented yet" message) and the
live probe passes.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"

IMAGE_TAG = "fagan-mcp-test"
ENTRYPOINT = "app/pipeline_mcp_server.py"

# The exact content the story pins down for the Dockerfile.
EXPECTED_LINES = [
    "FROM python:3.12-slim",
    "WORKDIR /app",
    "COPY requirements.txt .",
    "RUN pip install --no-cache-dir -r requirements.txt",
    "COPY . .",
    'CMD ["python", "app/pipeline_mcp_server.py"]',
]

# Conservative floor: the real server exposes 23 tools at time of writing,
# so this does not need updating every time a tool is added or removed.
MIN_TOOL_COUNT = 15

READ_TIMEOUT = 20.0
BUILD_TIMEOUT = 180
RMI_TIMEOUT = 60

INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-probe", "version": "0.1"},
    },
}
INITIALIZED_NOTIFICATION = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
    "params": {},
}
TOOLS_LIST_REQUEST = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {},
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _dockerfile_text() -> str:
    """The Dockerfile under test, failing loudly (not with a traceback) if absent."""
    if not DOCKERFILE.exists():
        pytest.fail(
            f"{DOCKERFILE} does not exist yet -- the repo-root Dockerfile is "
            "not implemented (this is the expected RED state before the "
            "implementation dispatch)"
        )
    return DOCKERFILE.read_text(encoding="utf-8")


def _nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _line_index(lines: list[str], needle: str) -> int:
    """Index of the first line containing ``needle``, or -1 when absent."""
    for index, line in enumerate(lines):
        if needle in line:
            return index
    return -1


def _copy_precedes_install(lines: list[str]) -> bool:
    """True only when ``COPY requirements.txt`` comes before ``RUN pip install``."""
    copy_index = _line_index(lines, "COPY requirements.txt")
    run_index = _line_index(lines, "RUN pip install")
    return copy_index != -1 and run_index != -1 and copy_index < run_index


def _docker_usable() -> bool:
    """True when the docker binary exists AND the daemon answers ``docker info``."""
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        # Binary present but not runnable / daemon not running / hung.
        return False
    return probe.returncode == 0


def _send(proc: subprocess.Popen, payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def _read_line(proc: subprocess.Popen, timeout: float = READ_TIMEOUT) -> str:
    """Read one non-blank stdout line, bounded by ``timeout`` seconds.

    Uses ``select`` on the raw fd so a hung container cannot wedge the test
    suite; the text wrapper is bypassed so no read-ahead can swallow bytes.
    """
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout
    buf = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no stdout line within {timeout}s (got {buf!r})")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise TimeoutError(f"no stdout line within {timeout}s (got {buf!r})")
        chunk = os.read(fd, 1)
        if not chunk:
            raise EOFError(f"container stdout closed early (got {buf!r})")
        buf += chunk
        if buf.endswith(b"\n"):
            line = buf.decode("utf-8", "replace").strip()
            if line:
                return line
            buf = b""


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _drain_stderr(proc: subprocess.Popen) -> str:
    if proc.stderr is None:
        return ""
    try:
        return proc.stderr.read() or ""
    except (OSError, ValueError):
        return ""


# --------------------------------------------------------------------------
# tier 1: static Dockerfile checks (no Docker required)
# --------------------------------------------------------------------------


def test_dockerfile_exists_and_is_not_empty() -> None:
    text = _dockerfile_text()
    assert text.strip(), "Dockerfile exists but is empty"


def test_base_image_is_python_312_slim() -> None:
    lines = _nonempty_lines(_dockerfile_text())
    from_lines = [line for line in lines if line.upper().startswith("FROM ")]
    assert from_lines, "Dockerfile has no FROM line"
    assert from_lines[0] == "FROM python:3.12-slim", (
        f"expected base image 'FROM python:3.12-slim', got {from_lines[0]!r}"
    )


def test_requirements_copied_before_pip_install() -> None:
    lines = _nonempty_lines(_dockerfile_text())
    assert _line_index(lines, "COPY requirements.txt") != -1, (
        "Dockerfile must COPY requirements.txt into the image"
    )
    assert _line_index(lines, "RUN pip install") != -1, (
        "Dockerfile must RUN pip install for the requirements"
    )
    assert _copy_precedes_install(lines), (
        "COPY requirements.txt must appear BEFORE RUN pip install so the "
        "dependency layer is cached independently of the source copy"
    )


def test_cmd_runs_stdio_entrypoint() -> None:
    lines = _nonempty_lines(_dockerfile_text())
    cmd_lines = [line for line in lines if line.upper().startswith("CMD ")]
    assert cmd_lines, "Dockerfile has no CMD line"
    cmd = cmd_lines[0]
    assert cmd.startswith("CMD ["), (
        f"CMD must use exec form (JSON array) so the stdio server gets the "
        f"signals/stdin it expects, got {cmd!r}"
    )
    assert ENTRYPOINT in cmd, f"CMD must run {ENTRYPOINT}, got {cmd!r}"
    assert "python" in cmd, f"CMD must invoke python, got {cmd!r}"


def test_dockerfile_matches_expected_content() -> None:
    """The story pins the Dockerfile content exactly; grade it as a block."""
    lines = _nonempty_lines(_dockerfile_text())
    width = len(EXPECTED_LINES)
    found = any(
        lines[start : start + width] == EXPECTED_LINES
        for start in range(len(lines) - width + 1)
    )
    assert found, (
        "Dockerfile does not contain the expected instruction block in order.\n"
        f"expected:\n{chr(10).join(EXPECTED_LINES)}\n"
        f"actual:\n{chr(10).join(lines)}"
    )


def test_ordering_predicate_rejects_reversed_and_missing_lines() -> None:
    """Negative cases for the ordering check, so it cannot pass vacuously."""
    assert _copy_precedes_install(
        ["FROM python:3.12-slim", "COPY requirements.txt .", "RUN pip install -r requirements.txt"]
    )
    assert not _copy_precedes_install(
        ["FROM python:3.12-slim", "RUN pip install -r requirements.txt", "COPY requirements.txt ."]
    )
    assert not _copy_precedes_install(["FROM python:3.12-slim", "COPY . ."])
    assert not _copy_precedes_install([])
    assert _line_index(["a", "b"], "b") == 1
    assert _line_index(["a", "b"], "zzz") == -1


# --------------------------------------------------------------------------
# tier 2: live build + MCP introspection (skipped when Docker is unusable)
# --------------------------------------------------------------------------


def test_docker_usable_probe_handles_missing_binary_and_dead_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skip gate must be False for every "docker is unusable" shape."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert _docker_usable() is False
    assert calls == [], "must not shell out when the binary is missing"

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _docker_usable() is True
    assert calls == [["docker", "info"]]

    def run_nonzero(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "Cannot connect to the Docker daemon")

    monkeypatch.setattr(subprocess, "run", run_nonzero)
    assert _docker_usable() is False

    def run_timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=10)

    monkeypatch.setattr(subprocess, "run", run_timeout)
    assert _docker_usable() is False

    def run_missing(argv, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", run_missing)
    assert _docker_usable() is False


def test_docker_build_and_mcp_introspection() -> None:
    """Build the image and prove the container answers initialize + tools/list."""
    if shutil.which("docker") is None:
        pytest.skip("docker binary is not installed in this environment")
    if not _docker_usable():
        pytest.skip("docker daemon is not reachable (docker info failed)")

    build = subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, "-f", "Dockerfile", "."],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT,
        check=False,
    )
    assert build.returncode == 0, (
        "docker build failed -- a real build failure is a defect, not an "
        "environment gap:\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )

    proc = subprocess.Popen(
        ["docker", "run", "--rm", "-i", IMAGE_TAG],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        # 1. initialize -> read its response before sending anything else.
        _send(proc, INITIALIZE_REQUEST)
        init_response = json.loads(_read_line(proc))
        assert init_response.get("jsonrpc") == "2.0", init_response
        assert init_response.get("id") == 1, init_response
        assert "result" in init_response, (
            f"initialize returned no result: {init_response}"
        )
        assert "protocolVersion" in init_response["result"], (
            f"initialize result has no protocolVersion: {init_response['result']}"
        )

        # 2. notification (no response) followed by tools/list.
        _send(proc, INITIALIZED_NOTIFICATION)
        _send(proc, TOOLS_LIST_REQUEST)
        tools_response = json.loads(_read_line(proc))
        assert tools_response.get("jsonrpc") == "2.0", tools_response
        assert tools_response.get("id") == 2, tools_response
        assert "result" in tools_response, (
            f"tools/list returned no result: {tools_response}"
        )
        tools = tools_response["result"]["tools"]
        assert isinstance(tools, list), f"tools/list result['tools'] is not a list: {tools!r}"
        assert len(tools) >= MIN_TOOL_COUNT, (
            f"expected at least {MIN_TOOL_COUNT} tools, got {len(tools)}"
        )
        assert all(isinstance(tool, dict) and tool.get("name") for tool in tools), (
            f"every tool must be an object with a non-empty name: {tools!r}"
        )
    except Exception as exc:
        _terminate(proc)
        stderr = _drain_stderr(proc)
        raise AssertionError(
            f"MCP stdio probe against the built image failed: {exc!r}\n"
            f"container stderr:\n{stderr}"
        ) from exc
    finally:
        _terminate(proc)
        subprocess.run(
            ["docker", "rmi", "-f", IMAGE_TAG],
            capture_output=True,
            text=True,
            timeout=RMI_TIMEOUT,
            check=False,
        )
