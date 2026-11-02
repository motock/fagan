"""SA-G1: ``deploy/install.sh --render-realm`` renders the Keycloak realm.

Keycloak's ``--import-realm`` substitutes ``${env.*}`` only for variables that
exist; anything left unresolved is imported as literal text, which produces
redirect URIs and client secrets that do not work. The installer therefore
renders ``deploy/standalone/realm.json`` into the state directory before the
stack starts, and compose mounts the rendered copy.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO / "deploy" / "install.sh"
COMPOSE = REPO / "deploy" / "standalone" / "compose.yaml"
TEMPLATE = REPO / "deploy" / "standalone" / "realm.json"

PLACEHOLDER_RE = re.compile(r"\$\{env\.([A-Za-z_][A-Za-z0-9_]*)\}")
HOST_LIKE_SUFFIXES = ("_URL", "_URI", "_HOST")


def placeholder_names() -> list[str]:
    """Every distinct ``${env.NAME}`` placeholder in the realm template."""
    names = sorted(set(PLACEHOLDER_RE.findall(TEMPLATE.read_text(encoding="utf-8"))))
    assert names, "the realm template should still ship ${env.*} placeholders"
    return names


def sample_value(name: str) -> str:
    if name.endswith(HOST_LIKE_SUFFIXES):
        return f"https://{name.lower()}.example.test"
    return f"sample-{name}-value"


def sample_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in placeholder_names():
        env[name] = sample_value(name)
    return env


def run_render(
    src: Path, dst: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALL_SH), "--render-realm", str(src), str(dst)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def load_clients(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["clients"]


def assert_absolute(uri: str) -> None:
    parsed = urlparse(uri)
    assert parsed.scheme in ("http", "https"), f"not an http(s) URI: {uri!r}"
    assert parsed.netloc, f"URI has no host: {uri!r}"


def test_render_substitutes_every_placeholder_with_absolute_uris(tmp_path: Path) -> None:
    dst = tmp_path / "realm.json"

    result = run_render(TEMPLATE, dst, sample_env())

    assert result.returncode == 0, result.stderr
    assert "${env." not in dst.read_text(encoding="utf-8")

    clients = load_clients(dst)
    assert clients, "the rendered realm should still define clients"
    for client in clients:
        for uri in client.get("redirectUris", []):
            assert_absolute(uri)
        for origin in client.get("webOrigins", []):
            assert_absolute(origin)

    dashboard = next(c for c in clients if c["clientId"] == "pipeline-dashboard")
    assert dashboard.get("redirectUris"), "pipeline-dashboard redirects must survive"
    for uri in dashboard["redirectUris"]:
        assert_absolute(uri)


def test_client_descriptions_fit_the_keycloak_column(tmp_path: Path) -> None:
    dst = tmp_path / "realm.json"

    result = run_render(TEMPLATE, dst, sample_env())

    assert result.returncode == 0, result.stderr
    for client in load_clients(dst):
        description = client.get("description", "")
        assert len(description) <= 255, (
            f"client {client['clientId']!r} description is {len(description)} characters"
        )


def test_rendered_file_is_private(tmp_path: Path) -> None:
    dst = tmp_path / "realm.json"

    result = run_render(TEMPLATE, dst, sample_env())

    assert result.returncode == 0, result.stderr
    assert dst.exists()
    assert os.stat(dst).st_mode & 0o777 == 0o600


def test_missing_variable_fails_by_name_and_writes_nothing(tmp_path: Path) -> None:
    dst = tmp_path / "realm.json"
    env = sample_env()
    missing = placeholder_names()[0]
    del env[missing]

    result = run_render(TEMPLATE, dst, env)

    assert result.returncode != 0
    assert f"render_realm: required variable {missing} is unset or empty" in result.stderr
    assert not dst.exists()


def test_source_without_placeholders_is_rejected(tmp_path: Path) -> None:
    src = tmp_path / "empty-realm.json"
    src.write_text('{"clients": []}\n', encoding="utf-8")
    dst = tmp_path / "realm.json"

    result = run_render(src, dst, sample_env())

    assert result.returncode != 0
    assert "no ${env.*} placeholders" in result.stderr
    assert not dst.exists()


def test_empty_variable_is_treated_as_unset(tmp_path: Path) -> None:
    dst = tmp_path / "realm.json"
    env = sample_env()
    empty = placeholder_names()[-1]
    env[empty] = ""

    result = run_render(TEMPLATE, dst, env)

    assert result.returncode != 0
    assert f"render_realm: required variable {empty} is unset or empty" in result.stderr
    assert not dst.exists()


def test_compose_and_installer_share_the_rendered_realm_contract() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    installer = INSTALL_SH.read_text(encoding="utf-8")

    assert "STANDALONE_REALM_FILE" in compose
    assert "./realm.json" not in compose, (
        "compose must mount the rendered realm, not the unresolved template"
    )
    assert "export STANDALONE_REALM_FILE=" in installer
    assert 'STANDALONE_REALM_FILE="$STATE_DIR/realm.json"' in installer

    usage = subprocess.run(
        ["bash", str(INSTALL_SH), "--render-realm"],
        cwd=REPO,
        env=sample_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert usage.returncode != 0
    assert "--render-realm" in usage.stderr
