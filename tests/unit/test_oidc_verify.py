"""Tests for the JWKS-backed OIDC token verifier (``app.oidc``).

Every test runs offline: the JWKS document is built from a locally generated
key pair and ``_fetch_jwks`` is replaced by a counting fake, so the network is
never touched. ``_JWKS_CACHE`` is module state that outlives a single test, so
the autouse fixture clears it before and after each one.
"""

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app import oidc
from app.identity import ROLES, IdentityError, Principal

ISSUER = "https://issuer.example"
AUDIENCE = "pipeline"
JWKS_URL = "https://issuer.example/jwks"

#: Marker for "mint a token without this claim".
_REMOVE = object()

#: The real fetcher, captured before the autouse fixture replaces it: the two
#: JWKS-failure tests have to exercise the status/parse checks inside it.
_REAL_FETCH_JWKS = oidc._fetch_jwks


def _private_pem(key) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _rsa_jwk(public_key) -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    jwk.update({"kid": "k1", "alg": "RS256", "use": "sig"})
    return jwk


def _ec_jwk(public_key) -> dict:
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(public_key))
    jwk.update({"kid": "e1", "alg": "ES256", "use": "sig"})
    return jwk


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _hs256_with_secret(secret: bytes, claims: dict, kid: str = "k1") -> str:
    """Mint an HS256 token by hand.

    PyJWT refuses to sign HMAC with an asymmetric PEM, which is exactly why
    the confusion attack is interesting: an attacker forges the header and
    computes the MAC themselves.
    """
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    segments = [_b64(json.dumps(header).encode()), _b64(json.dumps(claims).encode())]
    signing_input = ".".join(segments).encode("ascii")
    segments.append(_b64(hmac.new(secret, signing_input, hashlib.sha256).digest()))
    return ".".join(segments)


class _FakeFetch:
    """Counting stand-in for ``_fetch_jwks`` so cache hits are observable."""

    def __init__(self, document: dict) -> None:
        self.document = document
        self.call_count = 0

    def __call__(self, settings: oidc.OidcSettings) -> dict:
        self.call_count += 1
        return self.document


class _StubResponse:
    """The slice of ``httpx.Response`` that ``_fetch_jwks`` reads."""

    def __init__(self, status_code: int, document) -> None:
        self.status_code = status_code
        self._document = document

    def json(self):
        return self._document


@pytest.fixture(scope="session")
def rsa_key():
    """A 2048-bit RSA key pair as ``(private_key, pem_bytes)``."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, _private_pem(key)


@pytest.fixture(scope="session")
def ec_key():
    """An EC P-256 key pair as ``(private_key, pem_bytes)`` for the ES256 case."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key, _private_pem(key)


@pytest.fixture(autouse=True)
def fake_fetch(monkeypatch, rsa_key):
    """Clear the JWKS cache and serve the published RSA JWK for every test."""
    oidc._JWKS_CACHE.clear()
    fake = _FakeFetch({"keys": [_rsa_jwk(rsa_key[0].public_key())]})
    monkeypatch.setattr(oidc, "_fetch_jwks", fake)
    yield fake
    oidc._JWKS_CACHE.clear()


@pytest.fixture
def settings():
    return oidc.OidcSettings(issuer=ISSUER, audience=AUDIENCE, jwks_url=JWKS_URL)


def mint(pem: bytes, algorithm: str = "RS256", kid: str = "k1", **overrides) -> str:
    """Mint a token for ``ISSUER``/``AUDIENCE``, with per-claim overrides."""
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-1",
        "exp": now + 300,
        "iat": now,
    }
    for name, value in overrides.items():
        if value is _REMOVE:
            claims.pop(name, None)
        else:
            claims[name] = value
    return jwt.encode(claims, pem, algorithm=algorithm, headers={"kid": kid})


# --- positive cases -------------------------------------------------------


def test_valid_rs256_token_yields_principal(rsa_key, settings):
    token = mint(
        rsa_key[1],
        email="user@example.com",
        scope="read write",
        groups=[*sorted(ROLES), "wizard"],
    )
    assert oidc.verify_token(token, settings) == Principal(
        subject="user-1",
        roles=ROLES,
        scopes=("read", "write"),
        email="user@example.com",
        auth_method="oidc",
    )


def test_es256_token_is_accepted(ec_key, fake_fetch, settings):
    fake_fetch.document["keys"].append(_ec_jwk(ec_key[0].public_key()))
    token = mint(ec_key[1], algorithm="ES256", kid="e1")
    assert oidc.verify_token(token, settings).subject == "user-1"


def test_unknown_group_role_is_dropped_not_rejected(rsa_key, settings):
    token = mint(rsa_key[1], groups=["wizard"])
    assert oidc.verify_token(token, settings).roles == frozenset()


def test_exp_thirty_seconds_past_is_accepted(rsa_key, settings):
    token = mint(rsa_key[1], exp=int(time.time()) - 30)
    assert oidc.verify_token(token, settings).subject == "user-1"


def test_second_verification_uses_cached_jwks(rsa_key, fake_fetch, settings):
    token = mint(rsa_key[1])
    oidc.verify_token(token, settings)
    oidc.verify_token(token, settings)
    assert fake_fetch.call_count == 1


# --- negative cases -------------------------------------------------------


def test_none_token_raises_missing_credential(settings):
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(None, settings)
    assert str(excinfo.value) == "missing credential"


def test_empty_token_raises_missing_credential(settings):
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token("", settings)
    assert str(excinfo.value) == "missing credential"


def test_non_jwt_string_raises_malformed(settings):
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token("not-a-jwt", settings)
    assert str(excinfo.value) == "invalid token: malformed"


def test_alg_none_raises_unsupported_algorithm(rsa_key, settings):
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "user-1", "exp": int(time.time()) + 300},
        None,
        algorithm="none",
    )
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: unsupported algorithm"


def test_hs256_signed_with_public_key_raises_unsupported_algorithm(rsa_key, settings):
    public_pem = rsa_key[0].public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    token = _hs256_with_secret(
        public_pem,
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "user-1", "exp": int(time.time()) + 300},
    )
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: unsupported algorithm"


def test_expired_token_raises_verification_failed(rsa_key, settings):
    token = mint(rsa_key[1], exp=int(time.time()) - 300)
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: verification failed"


def test_future_nbf_raises_verification_failed(rsa_key, settings):
    token = mint(rsa_key[1], nbf=int(time.time()) + 300)
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: verification failed"


def test_wrong_issuer_raises_verification_failed(rsa_key, settings):
    token = mint(rsa_key[1], iss="https://evil.example")
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: verification failed"


def test_wrong_audience_raises_verification_failed(rsa_key, settings):
    token = mint(rsa_key[1], aud="someone-else")
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: verification failed"


def test_missing_exp_raises_verification_failed(rsa_key, settings):
    token = mint(rsa_key[1], exp=_REMOVE)
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: verification failed"


def test_missing_sub_raises_verification_failed(rsa_key, settings):
    token = mint(rsa_key[1], sub=_REMOVE)
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: verification failed"


def test_unknown_kid_raises_unknown_key(rsa_key, settings):
    token = mint(rsa_key[1], kid="nope")
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: unknown key"


def test_token_signed_by_different_key_raises_verification_failed(rsa_key, settings):
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = mint(_private_pem(impostor))
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: verification failed"


def test_jwks_non_200_raises_keys_unavailable(monkeypatch, rsa_key, settings):
    monkeypatch.setattr(oidc, "_fetch_jwks", _REAL_FETCH_JWKS)
    monkeypatch.setattr(oidc.httpx, "get", lambda *args, **kwargs: _StubResponse(503, None))
    token = mint(rsa_key[1])
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: keys unavailable"


def test_jwks_without_keys_raises_keys_unavailable(monkeypatch, rsa_key, settings):
    monkeypatch.setattr(oidc, "_fetch_jwks", _REAL_FETCH_JWKS)
    monkeypatch.setattr(oidc.httpx, "get", lambda *args, **kwargs: _StubResponse(200, {}))
    token = mint(rsa_key[1])
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: keys unavailable"


def test_expired_rejection_does_not_echo_token(rsa_key, settings):
    token = mint(rsa_key[1], exp=int(time.time()) - 300)
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert token not in str(excinfo.value)
