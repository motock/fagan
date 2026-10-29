"""Contract: every ``${env.X}`` the realm file needs is passed to the IdP.

``deploy/keycloak/realm.json`` is imported with ``--import-realm``, and Keycloak
substitutes ``${env.VAR}`` only when ``VAR`` exists in the IdP container's
environment. A placeholder whose variable is absent is *not* an error: the
literal string stays behind as the client secret. For a confidential client with
``directAccessGrantsEnabled`` - the smoke run's operator-login client - that
means the client secret is the string committed in this repository, which any
reader can use to mint the owner principal's token.

So the wiring is asserted here, for every placeholder in the realm file, not
just the one that happened to be missing. The test parses ``compose.yaml`` as
text (the same no-PyYAML constraint as ``test_deploy_contract.py``): it reads the
``idp`` service block and collects the keys of its ``environment`` mapping.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
COMPOSE = DEPLOY / "compose.yaml"
REALM = DEPLOY / "keycloak" / "realm.json"
SMOKE = DEPLOY / "smoke" / "smoke.sh"

#: ``${env.VAR}`` - the only form Keycloak substitutes in an imported realm.
PLACEHOLDER_RE = re.compile(r"\$\{env\.([A-Z][A-Z0-9_]*)\}")

#: A compose interpolation that fails loudly instead of defaulting to empty:
#: ``${VAR:?message}``. An optional ``${VAR:-}`` would re-open the same hole.
REQUIRED_INTERPOLATION_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?")


def _text(path: Path) -> str:
    assert path.exists(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def _service_block(service: str) -> list[str]:
    """Return the ``compose.yaml`` lines of *service*, without its header."""
    lines = _text(COMPOSE).splitlines()
    header = re.compile(rf"^  {re.escape(service)}:\s*$")
    start = next(
        (index for index, line in enumerate(lines) if header.match(line)),
        None,
    )
    assert start is not None, f"compose.yaml declares no {service} service"
    block: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= 2:
            break
        block.append(line)
    return block


def _idp_environment() -> dict[str, str]:
    """Return the ``idp`` service's environment mapping.

    Only the keys and raw values are needed, so an indentation scan is enough:
    ``environment:`` sits at four spaces inside the service, its keys at six.
    """
    block = _service_block("idp")
    env_start = next(
        (index for index, line in enumerate(block) if re.match(r"^    environment:\s*$", line)),
        None,
    )
    assert env_start is not None, "the idp service declares no environment mapping"
    env: dict[str, str] = {}
    for line in block[env_start + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= 4:
            break
        key, _, value = line.strip().partition(":")
        env[key] = value.strip()
    return env


def test_every_realm_placeholder_is_passed_to_the_idp():
    """No ``${env.X}`` in the realm may be missing from the IdP environment."""
    referenced = set(PLACEHOLDER_RE.findall(_text(REALM)))
    assert referenced, "the realm file should interpolate its secrets from the environment"
    supplied = set(_idp_environment())
    missing = sorted(referenced - supplied)
    assert not missing, (
        f"realm.json references {missing} but the idp service does not pass it: "
        "Keycloak would leave the literal placeholder as the client secret"
    )


def test_every_realm_secret_placeholder_is_required_not_defaulted():
    """Each one must be ``${VAR:?}``, so an unset value fails the stack loudly."""
    referenced = set(PLACEHOLDER_RE.findall(_text(REALM)))
    env = _idp_environment()
    for name in sorted(referenced):
        assert name in env, f"{name} is missing from the idp environment"
        assert REQUIRED_INTERPOLATION_RE.search(env[name]), (
            f"{name} must be interpolated as ${{{name}:?message}} so an unset "
            "secret cannot silently become the literal placeholder"
        )


def test_realm_carries_no_literal_secret():
    """The realm only ever names secrets through placeholders."""
    realm = json.loads(_text(REALM))
    for client in realm.get("clients", []):
        secret = client.get("secret")
        if secret is None:
            continue
        assert PLACEHOLDER_RE.fullmatch(secret), f"{client.get('clientId')} carries a literal secret"


def test_the_direct_grant_client_secret_is_the_one_the_smoke_run_uses():
    """The value the smoke script reads is the value the realm interpolates."""
    used = set(re.findall(r"PIPELINE_SMOKE_USER_CLIENT_SECRET", _text(SMOKE)))
    assert used, "smoke.sh must obtain the direct-grant client secret"
    assert "PIPELINE_SMOKE_USER_CLIENT_SECRET" in set(PLACEHOLDER_RE.findall(_text(REALM)))
    assert "PIPELINE_SMOKE_USER_CLIENT_SECRET" in _idp_environment()
