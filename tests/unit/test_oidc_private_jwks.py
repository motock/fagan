"""Private-network IdP support: the JWKS host guard's opt-in (OIDC-02).

``PIPELINE_OIDC_ALLOW_PRIVATE_JWKS`` loosens exactly one check - the internal
address rejection - for a self-hosted IdP inside a VPC. Only ``1``/``true``
enable it, and the fetch is still pinned to the resolved address.
"""

from __future__ import annotations

import socket

import pytest

from app.identity import IdentityError
from app.oidc import (
    OIDC_ALLOW_PRIVATE_JWKS_ENV,
    OIDC_AUDIENCE_ENV,
    OIDC_ISSUER_ENV,
    OIDC_JWKS_URL_ENV,
    OidcSettings,
    _oidc_settings_from_env,
    _resolve,
)

_ENV = {
    OIDC_ISSUER_ENV: "https://idp.example",
    OIDC_AUDIENCE_ENV: "pipeline",
    OIDC_JWKS_URL_ENV: "https://idp.example/jwks",
}

_LOOPBACK_URL = "https://127.0.0.1/jwks"


def _loopback_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple]:
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("127.0.0.1", 0),
        )
    ]


def test_one_enables_private_jwks() -> None:
    env = dict(_ENV, **{OIDC_ALLOW_PRIVATE_JWKS_ENV: "1"})
    assert _oidc_settings_from_env(env).allow_private_jwks is True


def test_uppercase_true_enables_private_jwks() -> None:
    env = dict(_ENV, **{OIDC_ALLOW_PRIVATE_JWKS_ENV: "TRUE"})
    assert _oidc_settings_from_env(env).allow_private_jwks is True


def test_unset_leaves_private_jwks_disabled() -> None:
    assert _oidc_settings_from_env(dict(_ENV)).allow_private_jwks is False


def test_empty_value_leaves_private_jwks_disabled() -> None:
    env = dict(_ENV, **{OIDC_ALLOW_PRIVATE_JWKS_ENV: ""})
    assert _oidc_settings_from_env(env).allow_private_jwks is False


def test_yes_leaves_private_jwks_disabled() -> None:
    env = dict(_ENV, **{OIDC_ALLOW_PRIVATE_JWKS_ENV: "yes"})
    assert _oidc_settings_from_env(env).allow_private_jwks is False


def test_on_leaves_private_jwks_disabled() -> None:
    env = dict(_ENV, **{OIDC_ALLOW_PRIVATE_JWKS_ENV: "on"})
    assert _oidc_settings_from_env(env).allow_private_jwks is False


def test_settings_reject_a_loopback_jwks_url_by_default() -> None:
    with pytest.raises(IdentityError):
        OidcSettings(issuer="https://idp.example", audience="pipeline", jwks_url=_LOOPBACK_URL)


def test_settings_accept_a_loopback_jwks_url_when_allowed() -> None:
    settings = OidcSettings(
        issuer="https://idp.example",
        audience="pipeline",
        jwks_url=_LOOPBACK_URL,
        allow_private_jwks=True,
    )
    assert settings.jwks_url == _LOOPBACK_URL


def test_resolve_refuses_a_loopback_host_by_default() -> None:
    assert _resolve(_LOOPBACK_URL) is None


def test_resolve_returns_the_pinned_loopback_address_when_allowed() -> None:
    assert _resolve(_LOOPBACK_URL, allow_private=True) == "127.0.0.1"


def test_resolve_refuses_a_name_resolving_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.oidc.socket.getaddrinfo", _loopback_getaddrinfo)
    assert _resolve("https://idp.internal/jwks") is None


def test_resolve_returns_the_pinned_address_for_a_name_when_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.oidc.socket.getaddrinfo", _loopback_getaddrinfo)
    assert _resolve("https://idp.internal/jwks", allow_private=True) == "127.0.0.1"
