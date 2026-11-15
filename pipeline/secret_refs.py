"""Resolve ``secret://provider/name`` references to raw secret values.

A reference names a provider and a name inside it. Three providers exist:

* ``env`` reads an environment variable, and only in the default namespace.
* ``file`` reads a private file under ``PIPELINE_SECRET_FILE_ROOT``. The file
  must resolve inside the root (symlink escapes are refused) and must not be
  readable by group or others.
* any other provider comes from an operator plugin selected by
  ``PIPELINE_SECRET_PROVIDER_PLUGINS`` (see :mod:`app.plugin_loader`). A plugin
  object exposes a ``name`` string and ``get(name) -> str | None``.

Every refusal raises :class:`SecretRefError` with a message that names the
failure class only, never the reference or the value. This module returns raw
strings; :mod:`app.credentials` wraps them in a redacting ``SecretValue``.
Keeping it free of ``app.credentials`` imports avoids a cycle.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass

from app.plugin_loader import PluginError, load_plugin_list, require_methods

SCHEME = "secret://"
FILE_ROOT_ENV = "PIPELINE_SECRET_FILE_ROOT"
PLUGIN_ENV = "PIPELINE_SECRET_PROVIDER_PLUGINS"
PLUGIN_SEAM = "secret_provider"

# The store layer's default namespace name. Kept as a literal so this module
# does not import the store layer; a test pins the equality.
_ENV_NAMESPACE = "default"

_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}\Z")
_NAME_RE = re.compile(r"^[A-Za-z0-9_./-]{1,128}\Z")
_BUILTIN_PROVIDERS = frozenset({"env", "file"})
_READ_CHUNK = 65536

# Provider registries keyed by the raw plugin spec that built them, so the
# operator's plugin modules are imported and instantiated once per spec.
_PLUGIN_REGISTRIES: dict[str, dict[str, object]] = {}


class SecretRefError(ValueError):
    """A secret reference is malformed, refused, or cannot be read."""


@dataclass(frozen=True)
class SecretRef:
    """A parsed reference: the provider that holds it and its name there."""

    provider: str
    name: str


def parse_body(body: object) -> SecretRef:
    """Parse ``provider/name`` (the reference without its scheme)."""
    if not isinstance(body, str):
        raise SecretRefError("secret reference must be a string")
    provider, separator, name = body.partition("/")
    if not separator or _PROVIDER_RE.match(provider) is None:
        raise SecretRefError("invalid secret reference")
    if _NAME_RE.match(name) is None or name.startswith("/") or ".." in name:
        raise SecretRefError("invalid secret reference")
    return SecretRef(provider=provider, name=name)


def parse_ref(text: object) -> SecretRef:
    """Parse a full ``secret://provider/name`` reference."""
    if not isinstance(text, str) or not text.startswith(SCHEME):
        raise SecretRefError("secret reference must use the secret:// scheme")
    return parse_body(text[len(SCHEME) :])


def read_secret(ref: SecretRef, *, environ=None) -> str | None:
    """Return the raw value for ``ref``, or None when it is not configured.

    Refusals raise :class:`SecretRefError`; None is reserved for "not configured".
    """
    env = os.environ if environ is None else environ
    if ref.provider == "env":
        return _read_env(ref.name, env)
    if ref.provider == "file":
        return _read_file(ref.name, env)
    provider = _plugin_providers(env).get(ref.provider)
    if provider is None:
        raise SecretRefError("unknown secret provider")
    try:
        value = provider.get(ref.name)
    except Exception:  # noqa: BLE001 - a plugin is arbitrary code
        raise SecretRefError("secret provider failed") from None
    if value is None:
        return None
    if not isinstance(value, str):
        raise SecretRefError("secret provider returned an invalid value")
    return value or None


def _read_env(name: str, env) -> str | None:
    # Imported here: ``pipeline.actor`` pulls in the identity layer, which must
    # not be loaded just to validate a reference.
    from pipeline.actor import current_namespace

    if current_namespace() != _ENV_NAMESPACE:
        raise SecretRefError("env secret provider is not available in this namespace")
    return str(env.get(name, "")).strip() or None


def _read_file(name: str, env) -> str | None:
    root_text = str(env.get(FILE_ROOT_ENV, "")).strip()
    if not root_text:
        raise SecretRefError("file secret provider is not configured")
    root = os.path.realpath(root_text)
    if not os.path.isdir(root):
        raise SecretRefError("file secret root is not a directory")
    # realpath follows every symlink, so a link that leaves the root is caught
    # by the containment check rather than by naming each link here.
    path = os.path.realpath(os.path.join(root, *name.split("/")))
    if path == root or os.path.commonpath([root, path]) != root:
        raise SecretRefError("secret path escapes the file secret root")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise SecretRefError("secret file cannot be opened") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecretRefError("secret is not a regular file")
        if info.st_mode & 0o077:
            raise SecretRefError("secret file is accessible to group or others")
        chunks = []
        while chunk := os.read(fd, _READ_CHUNK):
            chunks.append(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretRefError("secret file is not valid UTF-8") from None
    return text.rstrip("\r\n") or None


def _plugin_providers(env) -> dict[str, object]:
    spec = str(env.get(PLUGIN_ENV, "")).strip()
    if not spec:
        return {}
    if spec in _PLUGIN_REGISTRIES:
        return _PLUGIN_REGISTRIES[spec]
    try:
        factories = load_plugin_list(PLUGIN_SEAM, env)
    except PluginError:
        raise SecretRefError("secret provider configuration is invalid") from None
    registry: dict[str, object] = {}
    for factory in factories:
        try:
            provider = factory()
            require_methods(provider, ("get",), PLUGIN_SEAM)
        except Exception:  # noqa: BLE001 - a plugin is arbitrary code
            raise SecretRefError("secret provider configuration is invalid") from None
        name = getattr(provider, "name", None)
        if (
            not isinstance(name, str)
            or _PROVIDER_RE.match(name) is None
            or name in _BUILTIN_PROVIDERS
            or name in registry
        ):
            raise SecretRefError("secret provider plugin has an invalid name")
        registry[name] = provider
    _PLUGIN_REGISTRIES[spec] = registry
    return registry
