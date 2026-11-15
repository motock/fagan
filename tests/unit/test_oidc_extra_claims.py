"""Operator-configured extra claims on the verified principal (``app.oidc``).

``PIPELINE_OIDC_EXTRA_CLAIMS`` names token claims to carry on the principal as
immutable ``(name, value)`` pairs. Core never reads them. The default is
empty, so an unset variable changes nothing about verification.
"""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import oidc
from app.identity import IdentityError, Principal

ISSUER = "https://issuer.example"
AUDIENCE = "pipeline"
JWKS_URL = "https://issuer.example/jwks"

_ENV = {
    oidc.OIDC_ISSUER_ENV: ISSUER,
    oidc.OIDC_AUDIENCE_ENV: AUDIENCE,
    oidc.OIDC_JWKS_URL_ENV: JWKS_URL,
}


def _private_pem(key) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture(scope="module")
def rsa_key():
    """A 2048-bit RSA key pair as ``(private_key, pem_bytes)``."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, _private_pem(key)


@pytest.fixture(autouse=True)
def fake_fetch(monkeypatch, rsa_key):
    """Serve the published RSA JWK for every test; no network."""
    oidc._JWKS_CACHE.clear()
    oidc._JWKS_FAILURES.clear()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key[0].public_key()))
    jwk.update({"kid": "k1", "alg": "RS256", "use": "sig"})
    monkeypatch.setattr(
        oidc, "_fetch_jwks", lambda settings, force_refresh=False: {"keys": [jwk]}
    )
    yield
    oidc._JWKS_CACHE.clear()
    oidc._JWKS_FAILURES.clear()


def _settings(extra_claims: tuple[str, ...] = ()) -> oidc.OidcSettings:
    return oidc.OidcSettings(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS_URL,
        extra_claims=extra_claims,
    )


def _mint(pem: bytes, **claims) -> str:
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-1",
        "exp": now + 300,
        "iat": now,
    }
    payload.update(claims)
    return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": "k1"})


# --- positive cases -------------------------------------------------------


def test_unset_extra_claims_setting_yields_empty_tuple(rsa_key):
    principal = oidc.verify_token(_mint(rsa_key[1], tier="gold"), _settings())

    assert principal.extra_claims == ()


def test_unconfigured_path_matches_principal_without_extra_claims(rsa_key):
    token = _mint(
        rsa_key[1],
        groups=["author"],
        scope="read",
        email="u@example.com",
        email_verified=True,
    )

    principal = oidc.verify_token(token, _settings())

    assert principal == Principal(
        subject=f"{ISSUER}#user-1",
        roles=frozenset({"author"}),
        scopes=("read",),
        email="u@example.com",
        auth_method="oidc",
        principal_type="human",
    )


def test_listed_claims_are_carried_in_configured_order(rsa_key):
    token = _mint(rsa_key[1], tier="gold", region="eu")
    settings = _settings(("region", "tier"))

    principal = oidc.verify_token(token, settings)

    assert principal.extra_claims == (("region", "eu"), ("tier", "gold"))


def test_list_value_is_carried_as_tuple(rsa_key):
    token = _mint(rsa_key[1], flags=["a", "b"])

    principal = oidc.verify_token(token, _settings(("flags",)))

    assert principal.extra_claims == (("flags", ("a", "b")),)


def test_dict_value_is_carried_as_sorted_item_tuple(rsa_key):
    token = _mint(rsa_key[1], meta={"b": 2, "a": {"z": [1]}})

    principal = oidc.verify_token(token, _settings(("meta",)))

    assert principal.extra_claims == (("meta", (("a", (("z", (1,)),)), ("b", 2))),)


def test_unlisted_and_absent_claims_are_not_carried(rsa_key):
    token = _mint(rsa_key[1], tier="gold", secret_note="x")

    principal = oidc.verify_token(token, _settings(("tier", "missing")))

    assert principal.extra_claims == (("tier", "gold"),)


def test_principal_with_extra_claims_stays_hashable(rsa_key):
    token = _mint(rsa_key[1], flags=["a"], meta={"k": "v"})

    principal = oidc.verify_token(token, _settings(("flags", "meta")))

    assert hash(principal) == hash(
        oidc.verify_token(token, _settings(("flags", "meta")))
    )


def test_carried_values_cannot_be_mutated(rsa_key):
    token = _mint(rsa_key[1], flags=["a"])

    principal = oidc.verify_token(token, _settings(("flags",)))

    with pytest.raises(TypeError):
        principal.extra_claims[0][1][0] = "changed"  # type: ignore[index]


# --- negative cases -------------------------------------------------------


def test_claim_count_over_limit_is_refused_without_echo(rsa_key):
    names = tuple(f"c{i}" for i in range(257))
    token = _mint(rsa_key[1], **{name: "v" for name in names})

    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, _settings(names))

    assert str(excinfo.value) == "claim too large"


def test_serialised_size_over_limit_is_refused_without_echo(rsa_key, monkeypatch):
    # The token cap would refuse this token first; lift it so the claim-size
    # check is the one that fires.
    monkeypatch.setattr(oidc, "MAX_TOKEN_LENGTH", 64 * 1024)
    secret_marker = "SECRET-MARKER-VALUE"
    token = _mint(rsa_key[1], blob=secret_marker + "a" * (17 * 1024))

    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, _settings(("blob",)))

    assert str(excinfo.value) == "claim too large"
    assert secret_marker not in str(excinfo.value)


def test_claim_names_over_limit_are_not_refused_when_absent(rsa_key):
    names = tuple(f"c{i}" for i in range(257))
    token = _mint(rsa_key[1], tier="gold")

    principal = oidc.verify_token(token, _settings(names))

    assert principal.extra_claims == ()


@pytest.mark.parametrize("raw", ["1bad", "has space", "dash-name", "dot.name"])
def test_invalid_claim_name_is_refused_at_settings_load(raw):
    env = {**_ENV, oidc.OIDC_EXTRA_CLAIMS_ENV: f"good_name,{raw}"}

    with pytest.raises(IdentityError) as excinfo:
        oidc._oidc_settings_from_env(env)

    assert (
        str(excinfo.value)
        == "invalid oidc configuration: " + oidc.OIDC_EXTRA_CLAIMS_ENV
    )
    assert raw not in str(excinfo.value)


def test_valid_claim_names_are_parsed_at_settings_load():
    env = {**_ENV, oidc.OIDC_EXTRA_CLAIMS_ENV: " tier , _region,,flags_2 "}

    settings = oidc._oidc_settings_from_env(env)

    assert settings.extra_claims == ("tier", "_region", "flags_2")


def test_unset_extra_claims_env_loads_as_empty_tuple():
    assert oidc._oidc_settings_from_env(dict(_ENV)).extra_claims == ()
