"""Static contract for the standalone install (``deploy/standalone/``).

Like ``test_deploy_contract.py`` these tests read the artifacts as text (PyYAML
is not a dependency) and fail on assertion while the files do not exist.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STANDALONE = REPO_ROOT / "deploy" / "standalone"
COMPOSE = STANDALONE / "compose.yaml"
CADDYFILE = STANDALONE / "Caddyfile"
ENV_EXAMPLE = STANDALONE / ".env.example"
SMOKE_COMPOSE = REPO_ROOT / "deploy" / "compose.yaml"
GITIGNORE = REPO_ROOT / ".gitignore"

SECRET_NAMES = (
    "oidc_client_secret",
    "anthropic_api_key",
    "claude_code_oauth_token",
    "gh_token",
    "ollama_api_key",
)


def _text(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} does not exist"
    return path.read_text(encoding="utf-8")


def _top_block(text: str, key: str) -> str:
    """Body of a top-level ``key:`` mapping (lines indented under it)."""
    match = re.search(rf"^{key}:[ \t]*\n((?:[ \t]+.*\n|[ \t]*\n)*)", text, re.MULTILINE)
    assert match, f"no top-level {key}: block"
    return match.group(1)


def _service_names(text: str) -> list[str]:
    return re.findall(
        r"^  ([A-Za-z0-9_-]+):[ \t]*$", _top_block(text, "services"), re.MULTILINE
    )


def _service(text: str, name: str) -> str:
    body = _top_block(text, "services")
    match = re.search(
        rf"^  {re.escape(name)}:[ \t]*\n((?:    .*\n|[ \t]*\n)*)", body, re.MULTILINE
    )
    assert match, f"no service {name}"
    return match.group(1)


def _image(text: str, name: str) -> str:
    match = re.search(r"^\s+image:\s*(\S+)", _service(text, name), re.MULTILINE)
    assert match, f"service {name} has no image"
    return match.group(1)


def _handle_target(caddyfile: str, fragment: str | None) -> str | None:
    """Upstream of the handle block whose matcher names ``fragment``.

    ``fragment=None`` selects the matcher-less catch-all ``handle {``.
    """
    for header, body in re.findall(r"(handle[^\n{]*)\{([^{}]*)\}", caddyfile):
        header = header.strip()
        if header.startswith("handle @") or " @" in header:
            name = header.split("@", 1)[1].split()[0]
            defn = re.search(
                rf"^\s*@{re.escape(name)}\s+(.*)$", caddyfile, re.MULTILINE
            )
            header = defn.group(1) if defn else header
        is_catch_all = header == "handle"
        if (fragment is None and is_catch_all) or (fragment and fragment in header):
            upstream = re.search(r"reverse_proxy\s+(\S+)", body)
            return upstream.group(1) if upstream else None
    return None


def test_standalone_has_no_scripted_model_service() -> None:
    names = _service_names(_text(COMPOSE))

    assert {"pipeline", "keycloak", "proxy", "nats"} <= set(names)
    assert not [n for n in names if re.search(r"model|script", n, re.IGNORECASE)]


def test_keycloak_runs_in_production_start_mode() -> None:
    block = _service(_text(COMPOSE), "keycloak")

    assert re.search(r"\bstart\b", block)
    assert "start-dev" not in block
    assert "--import-realm" in block
    assert _image(_text(COMPOSE), "keycloak") == "quay.io/keycloak/keycloak:26.0.7"


def test_keycloak_trusts_the_proxy_and_persists_its_data() -> None:
    block = _service(_text(COMPOSE), "keycloak")

    assert "KC_PROXY_HEADERS" in block and "xforwarded" in block
    assert "KC_HTTP_ENABLED" in block
    assert "keycloak-data:/opt/keycloak/data" in block
    assert "./realm.json" in block


def test_proxy_and_nats_images_match_the_smoke_stack() -> None:
    standalone, smoke = _text(COMPOSE), _text(SMOKE_COMPOSE)

    assert _image(standalone, "proxy") == _image(smoke, "proxy")
    assert _image(standalone, "nats") == _image(smoke, "nats")


def test_pipeline_image_is_overridable_with_a_local_build_fallback() -> None:
    block = _service(_text(COMPOSE), "pipeline")

    assert "${PIPELINE_IMAGE:-pipeline-standalone:local}" in block
    assert "deploy/Dockerfile" in block
    assert "pipeline-state:/var/lib/pipeline" in block


def test_pipeline_reaches_host_ollama_on_linux_too() -> None:
    block = _service(_text(COMPOSE), "pipeline")

    assert "host.docker.internal:host-gateway" in block
    assert "PIPELINE_LOCAL_ENDPOINT_OLLAMA" in block


def test_pipeline_mounts_the_model_registry_read_only() -> None:
    block = _service(_text(COMPOSE), "pipeline")

    assert re.search(
        r"\./model_registry\.json:/app/model_registry\.local\.json:ro", block
    )
    assert "PIPELINE_MODEL_REGISTRY_PATH" in block


def test_pipeline_environment_is_derived_from_the_public_hostname() -> None:
    block = _service(_text(COMPOSE), "pipeline")

    for name in (
        "PIPELINE_OIDC_ISSUER",
        "PIPELINE_OIDC_AUDIENCE",
        "PIPELINE_OIDC_JWKS_URL",
        "PIPELINE_MCP_ALLOWED_HOSTS",
        "PIPELINE_DASHBOARD_ALLOWED_HOSTS",
        "PIPELINE_MCP_RESOURCE_URL",
        "PIPELINE_OIDC_REDIRECT_URI",
        "PIPELINE_REPO_URL_HOSTS",
    ):
        assert name in block, name
    assert "${PUBLIC_HOSTNAME}" in block
    assert "${PUBLIC_PORT:-8443}" in block
    assert "${PIPELINE_REPO_URL_HOSTS:-github.com}" in block


def test_every_secret_is_a_file_under_secrets_dir() -> None:
    body = _top_block(_text(COMPOSE), "secrets")
    entries = dict(re.findall(r"^  (\w+):[ \t]*\n\s+file:\s*(\S+)", body, re.MULTILINE))

    assert set(entries) == set(SECRET_NAMES)
    for name, path in entries.items():
        assert path == f"./secrets/{name}", name


def test_every_secret_entry_has_a_file_key() -> None:
    body = _top_block(_text(COMPOSE), "secrets")
    declared = re.findall(r"^  (\w+):", body, re.MULTILINE)

    assert (
        len(re.findall(r"^\s+file:\s*\./secrets/", body, re.MULTILINE))
        == len(declared)
        > 0
    )


def test_proxy_publishes_the_configurable_public_port() -> None:
    assert "${PUBLIC_PORT:-8443}:8443" in _service(_text(COMPOSE), "proxy")


def test_caddy_routes_mcp_and_resource_metadata_to_the_mcp_port() -> None:
    caddy = _text(CADDYFILE)

    assert _handle_target(caddy, "/mcp") == "pipeline:8080"
    assert (
        _handle_target(caddy, "/.well-known/oauth-protected-resource")
        == "pipeline:8080"
    )


def test_caddy_routes_identity_paths_to_keycloak() -> None:
    caddy = _text(CADDYFILE)

    assert _handle_target(caddy, "/realms/") == "keycloak:8080"
    assert _handle_target(caddy, "/resources/") == "keycloak:8080"


def test_caddy_catch_all_goes_to_the_dashboard() -> None:
    assert _handle_target(_text(CADDYFILE), None) == "pipeline:8000"


def test_caddy_site_and_tls_come_from_the_environment() -> None:
    caddy = _text(CADDYFILE)

    assert "{$PUBLIC_HOSTNAME}:8443" in caddy
    assert "tls {$CADDY_TLS:internal}" in caddy


def test_env_example_documents_the_operator_variables() -> None:
    env = _text(ENV_EXAMPLE)

    for name in (
        "PUBLIC_HOSTNAME",
        "PUBLIC_PORT",
        "CADDY_TLS",
        "PIPELINE_IMAGE",
        "OLLAMA_ENDPOINT",
        "PIPELINE_REPO_URL_HOSTS",
    ):
        assert re.search(rf"^#?\s*{name}=", env, re.MULTILINE), name


def test_gitignore_excludes_secrets_dir_and_env_file() -> None:
    lines = {ln.strip() for ln in _text(GITIGNORE).splitlines()}

    assert "deploy/standalone/secrets/" in lines
    assert "deploy/standalone/.env" in lines


def test_compose_has_no_literal_secret_value() -> None:
    pattern = re.compile(
        r"^\s*-?\s*([A-Z0-9_]*(?:SECRET|PASSWORD|TOKEN)[A-Z0-9_]*)\s*[:=]\s*(\S.*)$",
        re.MULTILINE,
    )
    literals = [
        key
        for key, value in pattern.findall(_text(COMPOSE))
        if not key.endswith("_FILE") and not value.strip("\"' ").startswith("${")
    ]

    assert literals == []


def test_literal_secret_detector_flags_a_hardcoded_value() -> None:
    pattern = re.compile(
        r"^\s*([A-Z0-9_]*(?:SECRET|PASSWORD|TOKEN)[A-Z0-9_]*)\s*:\s*(\S.*)$",
        re.MULTILINE,
    )

    assert pattern.findall("      KC_BOOTSTRAP_ADMIN_PASSWORD: hunter2\n") == [
        ("KC_BOOTSTRAP_ADMIN_PASSWORD", "hunter2")
    ]
