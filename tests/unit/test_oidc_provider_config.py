"""Identity-provider selection from configuration (OIDC-02, ADR 0004).

The switch has no permissive value: an unknown or incomplete configuration
raises rather than falling back to the local shared secret.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.identity import IdentityError, SharedSecretProvider
from app.oidc import (
    IDENTITY_PROVIDER_ENV,
    OIDC_AUDIENCE_ENV,
    OIDC_CLOCK_SKEW_ENV,
    OIDC_ISSUER_ENV,
    OIDC_JWKS_URL_ENV,
    OidcProvider,
    build_identity_provider,
)

_OIDC_ENV = {
    IDENTITY_PROVIDER_ENV: "oidc",
    OIDC_ISSUER_ENV: "https://idp.example",
    OIDC_AUDIENCE_ENV: "pipeline",
    OIDC_JWKS_URL_ENV: "https://idp.example/jwks",
}


def _secret() -> str:
    return "operator-key"


def _rotating_reader(calls: list[int]) -> Callable[[], str]:
    """Return a reader whose value changes on every call, recording each."""

    def read() -> str:
        calls.append(len(calls))
        return f"key-{len(calls)}"

    return read


def test_missing_provider_selects_the_shared_secret_provider() -> None:
    provider = build_identity_provider(_secret, environ={})
    assert isinstance(provider, SharedSecretProvider)


def test_empty_provider_selects_the_shared_secret_provider() -> None:
    provider = build_identity_provider(_secret, environ={IDENTITY_PROVIDER_ENV: ""})
    assert isinstance(provider, SharedSecretProvider)


def test_shared_secret_provider_name_selects_the_shared_secret_provider() -> None:
    provider = build_identity_provider(
        _secret, environ={IDENTITY_PROVIDER_ENV: "shared-secret"}
    )
    assert isinstance(provider, SharedSecretProvider)


def test_oidc_with_complete_configuration_selects_the_oidc_provider() -> None:
    provider = build_identity_provider(_secret, environ=dict(_OIDC_ENV))
    assert isinstance(provider, OidcProvider)


def test_oidc_without_issuer_names_the_missing_variable() -> None:
    env = {key: value for key, value in _OIDC_ENV.items() if key != OIDC_ISSUER_ENV}
    with pytest.raises(IdentityError) as caught:
        build_identity_provider(_secret, environ=env)
    assert str(caught.value) == "invalid oidc configuration: " + OIDC_ISSUER_ENV


def test_oidc_without_issuer_never_echoes_a_configured_value() -> None:
    env = {
        IDENTITY_PROVIDER_ENV: "oidc",
        OIDC_AUDIENCE_ENV: "tenant-sentinel-audience",
        OIDC_JWKS_URL_ENV: "https://idp.example/jwks",
    }
    with pytest.raises(IdentityError) as caught:
        build_identity_provider(_secret, environ=env)
    assert "tenant-sentinel-audience" not in str(caught.value)


def test_oidc_with_non_https_jwks_url_is_rejected() -> None:
    env = dict(_OIDC_ENV, **{OIDC_JWKS_URL_ENV: "http://idp.example/jwks"})
    with pytest.raises(IdentityError):
        build_identity_provider(_secret, environ=env)


def test_oidc_with_non_numeric_clock_skew_names_the_variable() -> None:
    env = dict(_OIDC_ENV, **{OIDC_CLOCK_SKEW_ENV: "abc"})
    with pytest.raises(IdentityError) as caught:
        build_identity_provider(_secret, environ=env)
    assert OIDC_CLOCK_SKEW_ENV in str(caught.value)


def test_oidc_with_negative_clock_skew_is_rejected() -> None:
    env = dict(_OIDC_ENV, **{OIDC_CLOCK_SKEW_ENV: "-1"})
    with pytest.raises(IdentityError):
        build_identity_provider(_secret, environ=env)


def test_uppercase_oidc_value_is_unknown_rather_than_selected() -> None:
    env = dict(_OIDC_ENV, **{IDENTITY_PROVIDER_ENV: "OIDC"})
    with pytest.raises(IdentityError) as caught:
        build_identity_provider(_secret, environ=env)
    assert str(caught.value) == "invalid oidc configuration: unknown identity provider"


def test_none_provider_value_is_unknown_rather_than_disabled() -> None:
    with pytest.raises(IdentityError) as caught:
        build_identity_provider(_secret, environ={IDENTITY_PROVIDER_ENV: "none"})
    assert str(caught.value) == "invalid oidc configuration: unknown identity provider"


def test_build_does_not_consult_the_secret_reader() -> None:
    calls: list[int] = []
    build_identity_provider(_rotating_reader(calls), environ={})
    assert calls == []


def test_shared_secret_provider_consults_the_reader_on_every_authentication() -> None:
    calls: list[int] = []
    provider = build_identity_provider(_rotating_reader(calls), environ={})
    provider.authenticate("key-1")
    provider.authenticate("key-2")
    assert calls == [0, 1]
