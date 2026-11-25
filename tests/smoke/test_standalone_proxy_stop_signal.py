"""The standalone proxy's shell forwards stop signals to Caddy.

``proxy`` in ``deploy/standalone/compose.yaml`` runs a ``sh -c`` script as PID 1.
PID 1 ignores signals it has no handler for, so without a trap ``docker stop``
never reaches Caddy and the container is SIGKILLed after the full stop timeout.

The tests read the compose file as text (PyYAML is not a dependency). The
behaviour test runs the extracted script under ``sh`` with ``caddy`` replaced by
a stub on PATH and the CA bundle paths redirected into a temp directory.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "deploy" / "standalone" / "compose.yaml"
CADDY_RUN = "caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &"
BUNDLE_COPY = 'cp /etc/ssl/certs/ca-certificates.crt "$$bundle"'

# The script body is indented eight spaces under the `- |` line (the YAML block
# scalar); compose escapes every `$` as `$$`.
SCRIPT_INDENT = " " * 8


def _text(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} does not exist"
    return path.read_text(encoding="utf-8")


def _top_block(text: str, key: str) -> str:
    """Body of a top-level ``key:`` mapping (lines indented under it)."""
    match = re.search(rf"^{key}:[ \t]*\n((?:[ \t]+.*\n|[ \t]*\n)*)", text, re.MULTILINE)
    assert match, f"no top-level {key}: block"
    return match.group(1)


def _service(text: str, name: str) -> str:
    body = _top_block(text, "services")
    match = re.search(
        rf"^  {re.escape(name)}:[ \t]*\n((?:    .*\n|[ \t]*\n)*)", body, re.MULTILINE
    )
    assert match, f"no service {name}"
    return match.group(1)


def _proxy_command_script() -> str:
    """The ``sh -c`` script of the proxy command, with compose escaping undone."""
    lines = _service(_text(COMPOSE), "proxy").splitlines()
    assert "      - |" in lines, "proxy command is no longer a `- |` block scalar"
    start = lines.index("      - |") + 1
    body: list[str] = []
    for line in lines[start:]:
        if line.strip() and not line.startswith(SCRIPT_INDENT):
            break
        body.append(line[len(SCRIPT_INDENT) :] if line.strip() else "")
    return "\n".join(body).replace("$$", "$") + "\n"


def _proxy_command_text() -> str:
    return _service(_text(COMPOSE), "proxy")


# ---------------------------------------------------------------------------
# Static contract
# ---------------------------------------------------------------------------


def test_proxy_shell_traps_term_and_int_and_kills_the_caddy_pid() -> None:
    script = _proxy_command_script()
    assert re.search(
        r"""^trap 'kill -TERM "\$pid" 2>/dev/null' TERM INT$""", script, re.MULTILINE
    ), f"no TERM/INT forwarding trap in:\n{script}"


def test_proxy_trap_is_installed_before_the_first_wait() -> None:
    script = _proxy_command_script()
    trap_at = script.index("trap '")
    assert trap_at < script.index('wait "$pid"'), "trap must precede the first wait"


def test_proxy_trap_follows_the_caddy_pid_capture() -> None:
    script = _proxy_command_script()
    assert script.index("pid=$!") < script.index("trap '"), (
        "the trap references $pid, so it must be installed after `pid=$!`"
    )


def test_proxy_shell_waits_for_caddy_after_a_forwarded_signal() -> None:
    script = _proxy_command_script()
    assert 'kill -0 "$pid"' in script, (
        "after the trap fires the shell must keep waiting while Caddy is alive, "
        "so it exits only once Caddy has finished its graceful shutdown"
    )


def test_proxy_shell_exits_with_caddys_status() -> None:
    script = _proxy_command_script()
    assert script.rstrip().endswith('exit "$status"'), (
        "shell must exit with Caddy's status"
    )


def test_proxy_command_still_starts_caddy_in_the_background() -> None:
    assert CADDY_RUN in _proxy_command_script()


def test_proxy_command_still_publishes_the_ca_bundle() -> None:
    assert BUNDLE_COPY in _proxy_command_text()


def test_proxy_command_keeps_the_internal_ca_wait_limit() -> None:
    assert 'if [ "$i" -gt 60 ]; then' in _proxy_command_script()


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------

sh_required = pytest.mark.skipif(
    shutil.which("sh") is None, reason="sh is not available"
)
posix_only = pytest.mark.skipif(os.name != "posix", reason="signals and sh need POSIX")

STUB_TRAPS_TERM = """#!/bin/sh
trap 'echo TERM > "$STUB_MARKER"; exit 0' TERM
while :; do sleep 0.1; done
"""

STUB_EXITS_ON_ITS_OWN = """#!/bin/sh
exit 3
"""


def _write_stub(directory: Path, body: str) -> None:
    stub = directory / "caddy"
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)


def _redirected_script(tmp_path: Path) -> tuple[str, Path]:
    """The proxy script with its bundle paths pointed into ``tmp_path``."""
    ca_bundle = tmp_path / "ca-certificates.crt"
    ca_bundle.write_text("system bundle\n", encoding="utf-8")
    out_bundle = tmp_path / "root.crt"
    script = (
        _proxy_command_script()
        .replace("/ca-public/root.crt", str(out_bundle))
        .replace("/etc/ssl/certs/ca-certificates.crt", str(ca_bundle))
    )
    return script, out_bundle


def _run_proxy_script(
    tmp_path: Path, stub_body: str
) -> tuple[subprocess.Popen, Path, Path]:
    script, out_bundle = _redirected_script(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, stub_body)
    marker = tmp_path / "stub-term.marker"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "CADDY_TLS": "public",
        "STUB_MARKER": str(marker),
    }
    process = subprocess.Popen(["sh", "-c", script], env=env)
    return process, out_bundle, marker


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.05)


@sh_required
@posix_only
def test_term_to_the_shell_stops_caddy_and_the_shell_exits_within_5s(
    tmp_path: Path,
) -> None:
    process, out_bundle, marker = _run_proxy_script(tmp_path, STUB_TRAPS_TERM)
    try:
        # The bundle is written after the trap is installed, so its appearance
        # means the forwarding trap is live.
        _wait_for(out_bundle)
        process.send_signal(signal.SIGTERM)
        started = time.monotonic()
        status = process.wait(timeout=5)
        elapsed = time.monotonic() - started
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert elapsed < 5
    assert marker.read_text(encoding="utf-8").strip() == "TERM", (
        "stub never saw SIGTERM"
    )
    assert status == 0


@sh_required
@posix_only
def test_int_to_the_shell_also_stops_caddy(tmp_path: Path) -> None:
    process, out_bundle, marker = _run_proxy_script(tmp_path, STUB_TRAPS_TERM)
    try:
        _wait_for(out_bundle)
        process.send_signal(signal.SIGINT)
        status = process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert marker.read_text(encoding="utf-8").strip() == "TERM"
    assert status == 0


@sh_required
@posix_only
def test_shell_exits_with_caddys_status_when_caddy_exits_on_its_own(
    tmp_path: Path,
) -> None:
    process, _, _ = _run_proxy_script(tmp_path, STUB_EXITS_ON_ITS_OWN)
    try:
        status = process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert status == 3


@sh_required
@posix_only
def test_bundle_is_published_without_a_signal(tmp_path: Path) -> None:
    process, out_bundle, _ = _run_proxy_script(tmp_path, STUB_TRAPS_TERM)
    try:
        _wait_for(out_bundle)
        assert out_bundle.read_text(encoding="utf-8") == "system bundle\n"
        assert process.poll() is None, "the shell must keep running while Caddy runs"
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
