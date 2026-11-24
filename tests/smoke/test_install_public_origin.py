"""Contract tests for the public origin the installer writes (port 443 has no port).

Keycloak drops the default HTTPS port from its issuer, and the pipeline compares
the configured issuer with the discovery document exactly, so an origin written
as ``https://host:443`` never matches and login is refused. The installer must
therefore write ``https://host`` when the port is 443, and keep the explicit
port for every other value (only 443 is the default).

These are dry-run only: no Docker daemon, no network, no secrets. The client
config (``token_url``) is written after the dry-run exit, so it is asserted
against the installer source instead.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL = REPO_ROOT / "deploy" / "install.sh"

HOST = "pipeline.example.com"
ORIGIN_LINE_RE = re.compile(r"^PUBLIC_ORIGIN=(.*)$", re.MULTILINE)


def _run(*args: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HOME"] = str(home)
    # Keep the dry run hermetic: no ambient operator overrides.
    for key in (
        "PIPELINE_HOSTNAME",
        "PUBLIC_HOSTNAME",
        "PUBLIC_PORT",
        "PUBLIC_ORIGIN",
        "PIPELINE_IMAGE",
        "ANTHROPIC_API_KEY",
        "GH_TOKEN",
        "PIPELINE_PLAN_APPROVAL",
    ):
        env.pop(key, None)
    return subprocess.run(
        ["bash", str(INSTALL), *args],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=120,
    )


def _dry_run(
    tmp_path: Path, port: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    result = _run(
        "--dry-run",
        "--non-interactive",
        "--state-dir",
        str(state_dir),
        "--hostname",
        HOST,
        "--port",
        port,
        home=home,
    )
    assert result.returncode == 0, result.stderr
    return result, state_dir


def _origin(state_dir: Path) -> str:
    match = ORIGIN_LINE_RE.search((state_dir / ".env").read_text(encoding="utf-8"))
    assert match, "PUBLIC_ORIGIN missing from the generated .env"
    return match.group(1)


def _realm(state_dir: Path) -> str:
    return (state_dir / "import" / "realm.json").read_text(encoding="utf-8")


def test_port_443_writes_the_origin_without_the_default_port(tmp_path: Path) -> None:
    _, state_dir = _dry_run(tmp_path, "443")

    assert _origin(state_dir) == f"https://{HOST}"


def test_port_443_realm_redirect_uri_has_no_port(tmp_path: Path) -> None:
    _, state_dir = _dry_run(tmp_path, "443")
    realm = _realm(state_dir)

    assert f"https://{HOST}/auth/callback" in realm
    assert ":443" not in realm


def test_port_443_poll_hint_has_no_port(tmp_path: Path) -> None:
    result, _ = _dry_run(tmp_path, "443")

    assert f"https://{HOST}/realms/pipeline/.well-known" in result.stdout
    assert ":443" not in result.stdout


def test_port_8443_keeps_the_explicit_port(tmp_path: Path) -> None:
    _, state_dir = _dry_run(tmp_path, "8443")

    assert _origin(state_dir) == f"https://{HOST}:8443"
    assert f"https://{HOST}:8443/auth/callback" in _realm(state_dir)


def test_port_4443_keeps_the_explicit_port(tmp_path: Path) -> None:
    # Boundary: only 443 is the default HTTPS port, so 4443 must keep its port.
    _, state_dir = _dry_run(tmp_path, "4443")

    assert _origin(state_dir) == f"https://{HOST}:4443"
    realm = _realm(state_dir)
    assert f"https://{HOST}:4443/auth/callback" in realm
    assert f"https://{HOST}/auth/callback" not in realm


def test_installer_builds_the_origin_from_host_and_port_in_one_place() -> None:
    # The token_url, MCP_URL and dashboard line used to build `https://host:port`
    # themselves; every public URL now comes from PUBLIC_ORIGIN, so the port-always
    # form is written in exactly one place, the origin's own definition.
    text = INSTALL.read_text(encoding="utf-8")

    assert '  PUBLIC_ORIGIN="https://$HOSTNAME:$PORT"' in text
    assert text.count("https://$HOSTNAME:$PORT") == 1
    assert "https://%s:%s" not in text
    assert '"token_url": "%s/realms/%s/protocol/openid-connect/token"' in text
    assert '"$PUBLIC_ORIGIN" "$REALM"' in text


DEPLOY_DOC = REPO_ROOT / "docs" / "DEPLOY_STANDALONE.md"


def test_deploy_doc_says_port_443_is_written_without_the_port() -> None:
    # Operator-facing: the doc must state the 443 rule in the form the installer
    # actually writes, so an operator reading it expects `https://<host>`.
    text = DEPLOY_DOC.read_text(encoding="utf-8")

    assert "`--port 443` is written without the port" in text
    assert "`https://<host>`" in text


def test_deploy_doc_does_not_describe_a_443_origin_with_its_port() -> None:
    # Negative: no worked example may still show the `:443` form the installer
    # no longer writes (the pipeline compares issuers exactly).
    text = DEPLOY_DOC.read_text(encoding="utf-8")

    assert f"{HOST}:443" not in text
