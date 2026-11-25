"""The standalone proxy publishes a CA bundle that includes Caddy's local root.

In public TLS mode (``CADDY_TLS`` is an ACME email) Caddy still issues a
certificate for the in-network name ``proxy`` from its local authority, and it
creates that root a moment after it starts. The proxy command publishes the
system roots at once and then, in a background subshell, replaces the bundle
with system roots plus the local root once the root appears. Internal mode
still waits for the root before it writes the bundle.

The tests read the compose file as text (PyYAML is not a dependency) and run
the extracted script under ``sh`` with ``caddy`` replaced by a stub on PATH.
Every path the script touches is redirected into a temp directory, and the
60 s limit is shortened where a test would otherwise wait a full minute.
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
BUNDLE_COPY = 'cp /etc/ssl/certs/ca-certificates.crt "$$bundle"'
LOCAL_ROOT = "/data/caddy/pki/authorities/local/root.crt"
SYSTEM_BUNDLE_TEXT = "system bundle\n"
LOCAL_ROOT_TEXT = "local root\n"
NO_ROOT_LINE = (
    "the local CA root did not appear within 60s; "
    "the published bundle has the system roots only"
)

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


# ---------------------------------------------------------------------------
# Static contract
# ---------------------------------------------------------------------------


def test_public_branch_keeps_the_immediate_system_bundle_copy() -> None:
    assert BUNDLE_COPY in _service(_text(COMPOSE), "proxy")


def test_public_branch_polls_for_the_local_root_with_the_sixty_second_limit() -> None:
    script = _proxy_command_script()
    assert script.count('if [ "$i" -gt 60 ]; then') == 2, (
        "the public poller must carry the same 60 s limit as the internal wait "
        "(one check per branch)"
    )
    assert NO_ROOT_LINE in script


def test_public_poller_starts_before_the_first_wait() -> None:
    script = _proxy_command_script()
    assert ") &" in script, "the public poller must run as a background subshell"
    assert script.index(") &") < script.index('wait "$pid"'), (
        "the poller must be started before the first wait on Caddy"
    )


def test_public_poller_replaces_the_bundle_with_a_move_from_its_own_directory() -> None:
    script = _proxy_command_script()
    assert 'mv "$tmp" "$bundle"' in script, "the bundle must be replaced by mv"
    assert 'tmp="$bundle.tmp"' in script, (
        "the temp file must sit next to the bundle so mv is an atomic rename"
    )


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


def _write_stub(directory: Path, body: str) -> None:
    stub = directory / "caddy"
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)


class _Proxy:
    """The proxy script running under ``sh`` with every path in ``tmp_path``."""

    def __init__(self, tmp_path: Path, tls: str, limit: int = 2) -> None:
        self.tmp_path = tmp_path
        self.bundle = tmp_path / "root.crt"
        self.system = tmp_path / "ca-certificates.crt"
        self.local_root = tmp_path / "local-root.crt"
        self.marker = tmp_path / "stub-term.marker"
        self.stderr_path = tmp_path / "stderr.log"
        self.system.write_text(SYSTEM_BUNDLE_TEXT, encoding="utf-8")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_stub(bin_dir, STUB_TRAPS_TERM)
        script = (
            _proxy_command_script()
            .replace("/ca-public/root.crt", str(self.bundle))
            .replace("/etc/ssl/certs/ca-certificates.crt", str(self.system))
            .replace(LOCAL_ROOT, str(self.local_root))
            .replace("-gt 60", f"-gt {limit}")
        )
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "CADDY_TLS": tls,
            "STUB_MARKER": str(self.marker),
        }
        self.stderr = self.stderr_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            ["sh", "-c", script], env=env, stderr=self.stderr
        )

    def stop(self) -> int | None:
        """Send SIGTERM and wait; returns the shell's status if it exited."""
        status = None
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            status = self.process.wait(timeout=5)
        self.stderr.close()
        return status

    def stderr_text(self) -> str:
        return self.stderr_path.read_text(encoding="utf-8")


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.05)


def _wait_for_text(path: Path, expected: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not (path.exists() and path.read_text(encoding="utf-8") == expected):
        assert time.monotonic() < deadline, f"{path.name} never reached {expected!r}"
        time.sleep(0.05)


@sh_required
@posix_only
def test_public_mode_publishes_the_system_bundle_immediately(tmp_path: Path) -> None:
    proxy = _Proxy(tmp_path, tls="public")
    try:
        _wait_for(proxy.bundle)
        assert proxy.bundle.read_text(encoding="utf-8") == SYSTEM_BUNDLE_TEXT
        assert proxy.process.poll() is None, "the shell must keep running"
    finally:
        proxy.stop()


@sh_required
@posix_only
def test_public_mode_appends_a_local_root_created_after_startup(tmp_path: Path) -> None:
    proxy = _Proxy(tmp_path, tls="public", limit=30)
    try:
        _wait_for(proxy.bundle)
        proxy.local_root.write_text(LOCAL_ROOT_TEXT, encoding="utf-8")
        _wait_for_text(proxy.bundle, SYSTEM_BUNDLE_TEXT + LOCAL_ROOT_TEXT)
    finally:
        proxy.stop()


@sh_required
@posix_only
def test_public_mode_replace_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    proxy = _Proxy(tmp_path, tls="public", limit=30)
    try:
        _wait_for(proxy.bundle)
        proxy.local_root.write_text(LOCAL_ROOT_TEXT, encoding="utf-8")
        _wait_for_text(proxy.bundle, SYSTEM_BUNDLE_TEXT + LOCAL_ROOT_TEXT)
    finally:
        proxy.stop()
    leftovers = sorted(
        p.name for p in tmp_path.iterdir() if p.name.startswith("root.crt.")
    )
    assert leftovers == [], f"temp file left behind: {leftovers}"


@sh_required
@posix_only
def test_public_mode_keeps_system_roots_and_reports_when_no_root_appears(
    tmp_path: Path,
) -> None:
    proxy = _Proxy(tmp_path, tls="public", limit=2)
    try:
        _wait_for(proxy.bundle)
        _wait_for_text_stderr(proxy, NO_ROOT_LINE + "\n")
        assert proxy.bundle.read_text(encoding="utf-8") == SYSTEM_BUNDLE_TEXT
        assert proxy.process.poll() is None, (
            "a missing local root must not stop the shell or Caddy"
        )
    finally:
        proxy.stop()
    assert proxy.stderr_text() == NO_ROOT_LINE + "\n"


def _wait_for_text_stderr(proxy: _Proxy, expected: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while proxy.stderr_text() != expected:
        assert time.monotonic() < deadline, f"stderr never reached {expected!r}"
        time.sleep(0.05)


@sh_required
@posix_only
def test_sigterm_reaches_caddy_while_the_public_poller_runs(tmp_path: Path) -> None:
    proxy = _Proxy(tmp_path, tls="public", limit=30)
    try:
        _wait_for(proxy.bundle)
        proxy.process.send_signal(signal.SIGTERM)
        started = time.monotonic()
        status = proxy.process.wait(timeout=5)
        elapsed = time.monotonic() - started
    finally:
        if proxy.process.poll() is None:
            proxy.process.kill()
            proxy.process.wait()
        proxy.stderr.close()
    assert elapsed < 5
    assert proxy.marker.read_text(encoding="utf-8").strip() == "TERM", (
        "stub never saw SIGTERM"
    )
    assert status == 0


@sh_required
@posix_only
def test_internal_mode_writes_the_bundle_only_after_the_root_appears(
    tmp_path: Path,
) -> None:
    proxy = _Proxy(tmp_path, tls="internal", limit=30)
    try:
        time.sleep(1.5)
        assert not proxy.bundle.exists(), "internal mode must wait for the root"
        proxy.local_root.write_text(LOCAL_ROOT_TEXT, encoding="utf-8")
        _wait_for_text(proxy.bundle, SYSTEM_BUNDLE_TEXT + LOCAL_ROOT_TEXT)
    finally:
        proxy.stop()
    assert proxy.stderr_text() == "", "internal mode must not print the public line"
