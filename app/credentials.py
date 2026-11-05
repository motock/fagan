"""The credential provider seam: one place to obtain operator credentials.

Core **reads** credentials; it never stores or writes them. A provider is
selected ONLY by the operator-controlled environment variable
``PIPELINE_CREDENTIALS_PLUGIN=module:attr`` through :mod:`app.plugin_loader`, so
an operator-supplied module (for example an enterprise distribution reading
tenant-scoped values from a secret store) can supply credentials without a core
change per backend. Unset, :class:`EnvFileProvider` preserves the open-source
behaviour exactly. A bad plugin raises and never falls back to ambient
environment values.

The dispatch rule: a dispatched agent child is prompt-injectable, so it must
never receive a raw operator credential except through ONE named injection
function per credential class, and the raw string is obtained with
:meth:`SecretValue.reveal` only inside that function. Everything else treats a
:class:`SecretValue` as opaque and unprintable.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Protocol

from app.plugin_loader import load_plugin, plugin_spec, require_methods

_LOGGER = logging.getLogger("app.credentials")

# A purpose, a namespace and a scope key are lowercase dotted identifiers.
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}\Z")
# An ``env`` credential names an environment variable.
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}\Z")

_MAX_SCOPE_VALUE = 256

_LEGACY_ENV = {
    ("model", "anthropic_api_key"): "PIPELINE_ANTHROPIC_API_KEY",
    ("model", "claude_oauth_token"): "CLAUDE_CODE_OAUTH_TOKEN",
}

_REDACTED = "<redacted>"

# Provider instances keyed by the plugin spec that built them, so a seam is
# imported once per spec and one spec cannot masquerade as another.
_PLUGIN_PROVIDERS: dict[str, CredentialProvider] = {}


class CredentialError(RuntimeError):
    """A credential cannot be resolved.

    Messages never contain a credential value: they name the reference, the
    provider or the failure class, never the secret.
    """


class SecretValue:
    """An operator credential value that refuses to print or serialise itself.

    The raw string is only reachable through :meth:`reveal`, which the
    credential seam calls in one named injection function per credential class.
    """

    __slots__ = ("_v",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("a credential value must be a non-empty string")
        self._v = value

    def reveal(self) -> str:
        """Return the raw credential value — for injection, never for logging."""
        return self._v

    def __repr__(self) -> str:
        return _REDACTED

    def __str__(self) -> str:
        return _REDACTED

    def __format__(self, spec: str) -> str:
        # The format spec is deliberately ignored: padding a redaction is noise.
        return _REDACTED

    def __bool__(self) -> bool:
        return True

    def __reduce__(self):
        raise TypeError("SecretValue cannot be serialised or copied")

    def __reduce_ex__(self, protocol):
        raise TypeError("SecretValue cannot be serialised or copied")

    def __copy__(self):
        raise TypeError("SecretValue cannot be serialised or copied")

    def __deepcopy__(self, memo):
        raise TypeError("SecretValue cannot be serialised or copied")


def _invalid(field: str) -> CredentialError:
    """Build a validation error that names only ``field``, never its value."""
    return CredentialError(f"invalid credential reference: {field}")


def _check_pattern(field: str, value: object, pattern: re.Pattern[str]) -> str:
    """Return ``value`` if it is a string matching ``pattern``, else raise."""
    if not isinstance(value, str) or pattern.match(value) is None:
        raise _invalid(field)
    return value


@dataclass(frozen=True)
class CredentialRef:
    """What a caller wants: a purpose, a name, and optionally a tenant scope."""

    purpose: str
    name: str
    namespace: str = "default"
    scope: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _check_pattern("purpose", self.purpose, _IDENTIFIER_RE)
        _check_pattern("namespace", self.namespace, _IDENTIFIER_RE)
        name_pattern = _ENV_NAME_RE if self.purpose == "env" else _IDENTIFIER_RE
        _check_pattern("name", self.name, name_pattern)
        if isinstance(self.scope, (str, bytes)) or not isinstance(
            self.scope, (tuple, list)
        ):
            raise _invalid("scope")
        for entry in self.scope:
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                raise _invalid("scope key")
            _check_pattern("scope key", entry[0], _IDENTIFIER_RE)
        for entry in self.scope:
            value = entry[1]
            if (
                not isinstance(value, str)
                or len(value) > _MAX_SCOPE_VALUE
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise _invalid("scope value")


@dataclass(frozen=True)
class Credential:
    """A credential value plus the provider's optional expiry (epoch seconds)."""

    value: SecretValue
    expires_at: float | None = None


class CredentialProvider(Protocol):
    """The seam an operator plugin implements."""

    def get(self, ref: CredentialRef) -> Credential | None:
        """Return the credential for ``ref``, or None when it is not configured."""
        ...


class EnvFileProvider:
    """The open-source default: read a variable from the environment.

    It deliberately ignores ``namespace`` and ``scope``: the default deployment
    is single-tenant, so there is nothing to select between.
    """

    def __init__(self, environ=None) -> None:
        # Stored as given (never copied) so the process environment is read at
        # call time; an explicit ``{}`` means "no variables", not "ambient".
        self._environ = environ

    def get(self, ref: CredentialRef) -> Credential | None:
        env = os.environ if self._environ is None else self._environ
        # ``ref.namespace`` and ``ref.scope`` are deliberately ignored: the
        # open-source deployment is single-tenant, so there is nothing to
        # select between. An enterprise provider overrides this seam.
        if ref.purpose == "env":
            variable = ref.name
        else:
            variable = _LEGACY_ENV.get((ref.purpose, ref.name))
        if variable is None:
            return None
        raw = str(env.get(variable, "")).strip()
        if not raw:
            return None
        return Credential(SecretValue(raw))


def credential_provider(environ=None) -> CredentialProvider:
    """Return the configured provider: the operator plugin, else the default.

    The plugin must come from ``PIPELINE_CREDENTIALS_PLUGIN``; a bad spec raises
    :class:`~app.plugin_loader.PluginError` rather than falling back to the
    environment provider. A provider is cached per spec only after it has been
    validated.
    """
    spec = plugin_spec("credentials", environ)
    if spec is None:
        return EnvFileProvider(environ)
    if spec in _PLUGIN_PROVIDERS:
        return _PLUGIN_PROVIDERS[spec]
    factory = load_plugin("credentials", environ)
    provider = factory()
    require_methods(provider, ("get",), "credentials")
    _PLUGIN_PROVIDERS[spec] = provider
    return provider


def _log_outcome(ref: CredentialRef, outcome: str, level: int, **extra) -> None:
    """Log exactly one structured record for a resolve, never a value."""
    _LOGGER.log(
        level,
        "credential lookup",
        extra={
            "credential_purpose": ref.purpose,
            "credential_name": ref.name,
            "credential_namespace": ref.namespace,
            "credential_outcome": outcome,
            **extra,
        },
    )


def resolve(
    ref: CredentialRef,
    *,
    environ=None,
    now: float | None = None,
) -> SecretValue | None:
    """Resolve ``ref`` to a :class:`SecretValue`, or None when it is unset.

    Every return or failure logs exactly one record carrying the
    ``credential_*`` fields. Provider failures and expired values raise
    :class:`CredentialError`; None means "not configured", which is different
    from an error.
    """
    provider = credential_provider(environ)
    try:
        result = provider.get(ref)
    except CredentialError:
        _log_outcome(ref, "error", logging.WARNING)
        raise
    except Exception as exc:  # noqa: BLE001 - a provider is arbitrary code
        _LOGGER.warning(
            "credential provider failed",
            extra={
                "credential_purpose": ref.purpose,
                "credential_name": ref.name,
                "credential_namespace": ref.namespace,
                "credential_outcome": "error",
                "error_type": type(exc).__name__,
            },
        )
        raise CredentialError("credential provider failed") from None
    if result is None:
        _log_outcome(ref, "unset", logging.INFO)
        return None
    if not isinstance(result, Credential):
        _log_outcome(ref, "invalid", logging.WARNING)
        raise CredentialError("credential provider returned an invalid result")
    if result.expires_at is not None:
        current = now if now is not None else time.time()
        if current >= result.expires_at:
            _log_outcome(ref, "expired", logging.WARNING)
            raise CredentialError("credential expired")
    _log_outcome(ref, "ok", logging.INFO)
    return result.value
