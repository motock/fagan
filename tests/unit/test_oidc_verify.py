"""Tests for the JWKS-backed OIDC token verifier (``app.oidc``).

Every test runs offline: the JWKS document is built from a locally generated
key pair and ``_fetch_jwks`` is replaced by a counting fake, so the network is
never touched. ``_JWKS_CACHE`` is module state that outlives a single test, so
the autouse fixture clears it before and after each one.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from collections.abc import Callable
from typing import Self

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app import oidc
from app.identity import LOCAL_OPERATOR, ROLES, IdentityError, Principal
from app.policy import LOCAL_OPERATOR_SUBJECT, LocalPolicy

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


class _StubStream:
    """The slice of ``httpx.Response`` that the streaming ``_fetch_jwks`` reads.

    ``_fetch_jwks`` consumes the body incrementally, so a stub exposing only
    ``.content``/``.json()`` could not exercise the size cap it enforces.
    """

    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def iter_bytes(self, chunk_size: int | None = None):
        yield self._body


def _stub_stream(status_code: int, body: bytes = b"") -> Callable[..., _StubStream]:
    """Return a stand-in for ``_stream_response`` that yields a fixed response."""
    return lambda *args, **kwargs: _StubStream(status_code, body)


def _forbidden_stream(*args: object, **kwargs: object) -> None:
    """Fail the test if anything tries to open a JWKS connection."""
    raise AssertionError("a rejected JWKS host must never be contacted")


#: A genuinely global (``is_global``) address: the resolver is stubbed to it so
#: no test performs a real lookup. TEST-NET addresses will not do - CPython
#: reports 192.0.2.0/24 and 203.0.113.0/24 as non-global, so they are now
#: (correctly) refused as internal and would make a fetch-path test vacuous.
PUBLIC_ADDRESS = "93.184.216.34"


@pytest.fixture
def public_dns(monkeypatch):
    """Resolve every host to a public address instead of asking a resolver."""
    address = (PUBLIC_ADDRESS, 0)
    info = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)
    monkeypatch.setattr(oidc.socket, "getaddrinfo", lambda *args, **kwargs: [info])


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
    """Clear the JWKS caches and serve the published RSA JWK for every test."""
    oidc._JWKS_CACHE.clear()
    oidc._JWKS_FAILURES.clear()
    fake = _FakeFetch({"keys": [_rsa_jwk(rsa_key[0].public_key())]})
    monkeypatch.setattr(oidc, "_fetch_jwks", fake)
    yield fake
    oidc._JWKS_CACHE.clear()
    oidc._JWKS_FAILURES.clear()


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
        email_verified=True,
        scope="read write",
        groups=[*sorted(ROLES), "wizard"],
    )
    assert oidc.verify_token(token, settings) == Principal(
        subject=f"{ISSUER}#user-1",
        roles=ROLES,
        scopes=("read", "write"),
        email="user@example.com",
        auth_method="oidc",
    )


def test_es256_token_is_accepted(ec_key, fake_fetch, settings):
    fake_fetch.document["keys"].append(_ec_jwk(ec_key[0].public_key()))
    token = mint(ec_key[1], algorithm="ES256", kid="e1")
    assert oidc.verify_token(token, settings).subject == f"{ISSUER}#user-1"


def test_unknown_group_role_is_dropped_not_rejected(rsa_key, settings):
    token = mint(rsa_key[1], groups=["wizard"])
    assert oidc.verify_token(token, settings).roles == frozenset()


def test_exp_thirty_seconds_past_is_accepted(rsa_key, settings):
    token = mint(rsa_key[1], exp=int(time.time()) - 30)
    assert oidc.verify_token(token, settings).subject == f"{ISSUER}#user-1"


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


def test_jwks_non_200_raises_keys_unavailable(
    monkeypatch, public_dns, rsa_key, settings
):
    monkeypatch.setattr(oidc, "_fetch_jwks", _REAL_FETCH_JWKS)
    monkeypatch.setattr(oidc, "_stream_response", _stub_stream(503))
    token = mint(rsa_key[1])
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: keys unavailable"


def test_jwks_without_keys_raises_keys_unavailable(
    monkeypatch, public_dns, rsa_key, settings
):
    monkeypatch.setattr(oidc, "_fetch_jwks", _REAL_FETCH_JWKS)
    monkeypatch.setattr(oidc, "_stream_response", _stub_stream(200, b"{}"))
    token = mint(rsa_key[1])
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: keys unavailable"


def test_expired_rejection_does_not_echo_token(rsa_key, settings):
    token = mint(rsa_key[1], exp=int(time.time()) - 300)
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert token not in str(excinfo.value)


# --- review round 2: subject collision, scopes, email, config -------------


def test_token_sub_local_operator_is_not_the_local_operator(rsa_key, settings):
    token = mint(rsa_key[1], sub=LOCAL_OPERATOR_SUBJECT)
    principal = oidc.verify_token(token, settings)
    assert principal != LOCAL_OPERATOR


def test_token_sub_local_operator_is_denied_by_local_policy(rsa_key, settings):
    token = mint(rsa_key[1], sub=LOCAL_OPERATOR_SUBJECT)
    principal = oidc.verify_token(token, settings)
    assert LocalPolicy().allow(principal, "plan:approve") is False


def test_wildcard_scope_in_token_is_dropped(rsa_key, settings):
    token = mint(rsa_key[1], scope="* read")
    assert oidc.verify_token(token, settings).scopes == ("read",)


def test_unverified_email_is_not_carried(rsa_key, settings):
    token = mint(rsa_key[1], email="user@example.com", email_verified=False)
    assert oidc.verify_token(token, settings).email == ""


def test_jwks_url_pointing_at_loopback_is_rejected():
    with pytest.raises(IdentityError) as excinfo:
        oidc.OidcSettings(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_url="https://169.254.169.254/latest/meta-data/jwks",
        )
    assert str(excinfo.value) == "invalid jwks_url: must not target an internal host"


def test_repeated_fetch_failure_is_not_retried_per_call(monkeypatch, rsa_key, settings):
    fetches = []

    def failing(settings_arg):
        fetches.append(settings_arg)
        raise IdentityError("invalid token: keys unavailable")

    monkeypatch.setattr(oidc, "_fetch_jwks", failing)
    token = mint(rsa_key[1])
    with pytest.raises(IdentityError):
        oidc.verify_token(token, settings)
    with pytest.raises(IdentityError):
        oidc.verify_token(token, settings)
    assert len(fetches) == 1


def test_clock_skew_above_the_bound_is_rejected():
    with pytest.raises(IdentityError) as excinfo:
        oidc.OidcSettings(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_url=JWKS_URL,
            clock_skew_s=oidc.MAX_CLOCK_SKEW_S + 1,
        )
    assert str(excinfo.value) == (
        f"invalid clock_skew_s: must not exceed {oidc.MAX_CLOCK_SKEW_S}"
    )


# --- review round 3: log injection, SSRF resolution, streaming cap, '#' ---


def test_kid_control_characters_cannot_forge_a_log_line(rsa_key, settings, caplog):
    token = mint(rsa_key[1], kid="a\n2026-10-27 WARNING forged\x1bc")
    with caplog.at_level(logging.WARNING, logger="pipeline.identity"), pytest.raises(
        IdentityError
    ):
        oidc.verify_token(token, settings)
    assert [record.getMessage() for record in caplog.records] == [
        (
            "oidc verification rejected: reason='invalid token: unknown key' "
            "kid='a2026-10-27 WARNING forgedc' issuer='https://issuer.example'"
        )
    ]


def test_jwks_url_with_uppercase_loopback_name_and_trailing_dot_is_rejected():
    with pytest.raises(IdentityError) as excinfo:
        oidc.OidcSettings(
            issuer=ISSUER, audience=AUDIENCE, jwks_url="https://LOCALHOST./jwks"
        )
    assert str(excinfo.value) == "invalid jwks_url: must not target an internal host"


def test_jwks_host_resolving_inward_is_never_fetched(monkeypatch, rsa_key, settings):
    loopback = (
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        "",
        ("127.0.0.1", 0),
    )
    monkeypatch.setattr(oidc, "_fetch_jwks", _REAL_FETCH_JWKS)
    monkeypatch.setattr(oidc.socket, "getaddrinfo", lambda *args, **kwargs: [loopback])
    monkeypatch.setattr(oidc, "_stream_response", _forbidden_stream)
    token = mint(rsa_key[1])
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: keys unavailable"


def test_jwks_body_over_the_cap_raises_keys_unavailable(
    monkeypatch, public_dns, rsa_key, settings
):
    oversize = b"{" + b" " * oidc.JWKS_MAX_BYTES
    monkeypatch.setattr(oidc, "_fetch_jwks", _REAL_FETCH_JWKS)
    monkeypatch.setattr(oidc, "_stream_response", _stub_stream(200, oversize))
    token = mint(rsa_key[1])
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: keys unavailable"


def test_subject_containing_the_separator_is_rejected(rsa_key, settings):
    token = mint(rsa_key[1], sub=f"{LOCAL_OPERATOR_SUBJECT}#decoy")
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: unsupported subject"


def test_issuer_containing_the_separator_is_rejected():
    with pytest.raises(IdentityError) as excinfo:
        oidc.OidcSettings(
            issuer="https://a.example#x", audience=AUDIENCE, jwks_url=JWKS_URL
        )
    assert str(excinfo.value) == "invalid issuer: must not contain '#'"


# --- review round 4: SSRF allowlist, CGNAT/metadata, connection pinning ---


def test_carrier_grade_nat_address_is_internal():
    assert oidc._is_internal_address(ipaddress.ip_address("100.64.0.1")) is True


def test_alibaba_metadata_address_is_internal():
    assert oidc._is_internal_address(ipaddress.ip_address("100.100.100.200")) is True


def test_ipv4_mapped_carrier_grade_nat_address_is_internal():
    assert oidc._is_internal_address(ipaddress.ip_address("::ffff:100.64.0.1")) is True


def test_nat64_wrapped_loopback_is_internal():
    assert oidc._is_internal_address(ipaddress.ip_address("64:ff9b::7f00:1")) is True


def test_globally_routable_address_is_not_internal():
    assert oidc._is_internal_address(ipaddress.ip_address(PUBLIC_ADDRESS)) is False


def test_jwks_url_at_alibaba_metadata_address_is_rejected():
    with pytest.raises(IdentityError) as excinfo:
        oidc.OidcSettings(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_url="https://100.100.100.200/latest/meta-data/",
        )
    assert str(excinfo.value) == "invalid jwks_url: must not target an internal host"


def test_jwks_url_inside_carrier_grade_nat_is_rejected():
    with pytest.raises(IdentityError) as excinfo:
        oidc.OidcSettings(
            issuer=ISSUER, audience=AUDIENCE, jwks_url="https://100.64.0.1/jwks"
        )
    assert str(excinfo.value) == "invalid jwks_url: must not target an internal host"


def test_jwks_host_resolving_to_metadata_is_never_fetched(
    monkeypatch, rsa_key, settings
):
    metadata = (
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        "",
        ("100.100.100.200", 0),
    )
    monkeypatch.setattr(oidc, "_fetch_jwks", _REAL_FETCH_JWKS)
    monkeypatch.setattr(oidc.socket, "getaddrinfo", lambda *args, **kwargs: [metadata])
    monkeypatch.setattr(oidc, "_stream_response", _forbidden_stream)
    token = mint(rsa_key[1])
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: keys unavailable"


def test_fetch_connects_to_the_validated_address(monkeypatch, public_dns, settings):
    captured = {}

    def capture(self, request):
        captured["url_host"] = request.url.host
        captured["host_header"] = request.headers["Host"]
        captured["sni_hostname"] = request.extensions["sni_hostname"]
        raise httpx.ConnectError("stop before any connection is opened")

    monkeypatch.setattr(oidc, "_fetch_jwks", _REAL_FETCH_JWKS)
    monkeypatch.setattr(oidc.httpx.HTTPTransport, "handle_request", capture)
    with pytest.raises(IdentityError):
        oidc._fetch_jwks(settings)
    assert (
        captured["url_host"],
        captured["host_header"],
        captured["sni_hostname"],
    ) == (PUBLIC_ADDRESS, "issuer.example", "issuer.example")


def test_jwk_published_for_another_algorithm_is_not_used(fake_fetch, rsa_key, settings):
    fake_fetch.document["keys"][0]["alg"] = "RS512"
    token = mint(rsa_key[1])
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: unknown key"


def test_jwk_of_the_wrong_key_type_is_not_used(fake_fetch, ec_key, rsa_key, settings):
    fake_fetch.document["keys"] = [_ec_jwk(ec_key[0].public_key())]
    token = mint(rsa_key[1])
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: unknown key"


def test_deeply_nested_jwks_document_raises_keys_unavailable(
    monkeypatch, public_dns, rsa_key, settings
):
    nested = b"[" * 200000 + b"]" * 200000
    monkeypatch.setattr(oidc, "_fetch_jwks", _REAL_FETCH_JWKS)
    monkeypatch.setattr(oidc, "_stream_response", _stub_stream(200, nested))
    token = mint(rsa_key[1])
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: keys unavailable"


def test_hanging_resolver_returns_within_the_deadline(monkeypatch):
    def hang(*args, **kwargs):
        time.sleep(0.5)
        return []

    monkeypatch.setattr(oidc.socket, "getaddrinfo", hang)
    monkeypatch.setattr(oidc, "JWKS_DNS_TIMEOUT_S", 0.05)
    started = time.monotonic()
    resolved = oidc._resolve(JWKS_URL)
    assert (resolved, time.monotonic() - started < 0.5) == (None, True)


def test_token_lifetime_above_the_bound_raises_verification_failed(rsa_key, settings):
    token = mint(rsa_key[1], iat=int(time.time()) - 4000, exp=int(time.time()) + 300)
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: verification failed"


def test_token_lifetime_within_the_bound_is_accepted(rsa_key, settings):
    token = mint(
        rsa_key[1],
        iat=int(time.time()) - oidc.MAX_TOKEN_LIFETIME_S + 300,
        exp=int(time.time()) + 300,
    )
    assert oidc.verify_token(token, settings).subject == f"{ISSUER}#user-1"


def test_slow_stream_past_the_total_deadline_raises_keys_unavailable(
    monkeypatch, public_dns, rsa_key, settings
):
    class _TricklingStream(_StubStream):
        def iter_bytes(self, chunk_size=None):
            time.sleep(0.2)
            yield b'{"keys": []}'

    monkeypatch.setattr(oidc, "_fetch_jwks", _REAL_FETCH_JWKS)
    monkeypatch.setattr(oidc, "JWKS_TOTAL_TIMEOUT_S", 0.0)
    monkeypatch.setattr(
        oidc, "_stream_response", lambda *args, **kwargs: _TricklingStream(200, b"")
    )
    token = mint(rsa_key[1])
    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)
    assert str(excinfo.value) == "invalid token: keys unavailable"
