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


def _realm_placeholders() -> set[str]:
    """Return every ``${env.VAR}`` the realm file references."""
    return set(PLACEHOLDER_RE.findall(_text(REALM)))


def test_every_realm_placeholder_is_supplied_to_the_idp() -> None:
    """An unsupplied placeholder silently becomes a committed secret."""
    missing = _realm_placeholders() - set(_idp_environment())
    assert not missing, (
        f"realm.json references {sorted(missing)} but the idp service does not "
        "supply them; Keycloak would import the literal placeholder as the value"
    )


def test_every_supplied_realm_value_is_required_not_defaulted() -> None:
    """``${VAR:?}`` fails the stack; ``${VAR:-}`` would ship a placeholder."""
    environment = _idp_environment()
    for name in sorted(_realm_placeholders()):
        value = environment[name]
        match = REQUIRED_INTERPOLATION_RE.search(value)
        assert match is not None and match.group(1) == name, (
            f"{name} must be interpolated as ${{{name}:?...}} - got {value!r}; "
            "a defaulted value re-opens the public-secret hole"
        )


def test_realm_declares_the_direct_grant_client() -> None:
    """Guard the precondition: the hole only matters while this client exists."""
    realm = json.loads(_text(REALM))
    client = next(
        (item for item in realm.get("clients", []) if item.get("clientId") == "pipeline-smoke-user"),
        None,
    )
    assert client is not None, "realm.json no longer declares pipeline-smoke-user"
    assert client.get("directAccessGrantsEnabled") is True
    assert PLACEHOLDER_RE.fullmatch(client.get("secret", "")), (
        "pipeline-smoke-user must reference an ${env.*} placeholder, never a literal"
    )
