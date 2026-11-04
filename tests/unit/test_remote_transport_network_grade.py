"""The remote MCP transport admits any provider that declares ``network_grade``.

A network-facing transport must only run behind a provider that verifies a
bearer credential cryptographically. That used to be spelled ``isinstance(
provider, OidcProvider)``; it is now an explicit, deny-by-default capability, so
a plugin provider can opt in while ``SharedSecretProvider`` and every provider
that stays silent are refused exactly as before.

The ASGI app double, the environ and the token fixtures are reused from the
sibling transport suites rather than copied here.
"""

from __future__ import annotations

import pytest

from app.identity import IdentityError, Principal, SharedSecretProvider
from app.oidc import OidcProvider
from pipeline.remote_transport import BearerAuthMiddleware
from tests.unit.test_remote_transport_resource_metadata import _RecordingApp
from tests.unit.test_remote_transport_serve import REMOTE_ENV


class _PluginProvider:
    """Plugin identity provider that opts in to network exposure."""

    network_grade = True

    def authenticate(self, credential: str | None) -> Principal:
        raise IdentityError("credential rejected")


class _TruthyButNotTrueProvider(_PluginProvider):
    """Truthy declaration that is not the exact ``True`` sentinel."""

    network_grade = "yes"


class _SilentProvider:
    """Provider that never declares the capability: refused by default."""

    def authenticate(self, credential: str | None) -> Principal:
        raise IdentityError("credential rejected")


def _environ() -> dict[str, str]:
    return dict(REMOTE_ENV)


def test_oidc_provider_declares_network_grade() -> None:
    assert OidcProvider.network_grade is True


def test_middleware_accepts_plugin_provider_with_exact_true() -> None:
    provider = _PluginProvider()
    middleware = BearerAuthMiddleware(
        _RecordingApp(), provider, environ=_environ()
    )
    assert isinstance(middleware, BearerAuthMiddleware)


def test_middleware_rejects_shared_secret_provider() -> None:
    with pytest.raises(TypeError, match="requires an OidcProvider"):
        BearerAuthMiddleware(
            _RecordingApp(), SharedSecretProvider(lambda: "x"), environ=_environ()
        )


def test_middleware_rejects_truthy_non_true_network_grade() -> None:
    with pytest.raises(TypeError, match="requires an OidcProvider"):
        BearerAuthMiddleware(
            _RecordingApp(), _TruthyButNotTrueProvider(), environ=_environ()
        )


def test_middleware_rejects_missing_network_grade() -> None:
    with pytest.raises(TypeError, match="requires an OidcProvider"):
        BearerAuthMiddleware(_RecordingApp(), _SilentProvider(), environ=_environ())
