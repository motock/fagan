"""Operator-selected plugins for architectural seams.

A plugin is chosen ONLY by an operator-controlled environment variable of the
form ``PIPELINE_<SEAM>_PLUGIN=module:attr`` (or ``PIPELINE_<SEAM>_PLUGINS`` for
an ordered, comma-separated list) — never by request, plan, repository or
manifest data. Because loading a plugin is code execution chosen by the
environment, the loader is off unless the variable is set, validates the spec
strictly, and fails closed: a bad spec raises :class:`PluginError` rather than
silently falling back to the built-in adapter.

The overlay image can supply plugin modules because ``/app`` is the working
directory, e.g. ``overlay.adapters:build_authorizer``.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
from collections.abc import Callable

logger = logging.getLogger("app.plugin_loader")

# ``package.module:attr`` — dotted identifier path, single colon, identifier.
SPEC_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$"
)
# Seams are lowercase identifiers with underscores, e.g. ``event_bus``.
_SEAM_RE = re.compile(r"^[a-z][a-z_]*$")

_MISSING = object()


class PluginError(RuntimeError):
    """A plugin selection is invalid, unresolvable, or conflicts with config."""


def plugin_env_name(seam: str) -> str:
    """Return the environment variable that selects ``seam``'s plugin."""
    if not _SEAM_RE.match(seam):
        raise ValueError(f"invalid plugin seam {seam!r}")
    return "PIPELINE_" + seam.upper() + "_PLUGIN"


def plugin_spec(seam: str, environ: dict[str, str] | None = None) -> str | None:
    """Return the stripped plugin spec for ``seam``, or None when unset/blank."""
    env = os.environ if environ is None else environ
    raw = env.get(plugin_env_name(seam), "")
    spec = str(raw).strip()
    return spec or None


def _resolve(env_name: str, spec: str) -> Callable:
    """Import ``spec`` and return its factory, or raise :class:`PluginError`."""
    if not SPEC_RE.match(spec):
        raise PluginError(
            f"{env_name} is not a valid plugin spec; expected 'module:attr'"
        )
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    # A plugin module is arbitrary third-party code: importing it can raise
    # anything, and every failure must fail closed as a PluginError.
    except Exception as exc:  # noqa: BLE001
        raise PluginError(
            f"{env_name}: cannot import the plugin module ({type(exc).__name__})"
        ) from None
    target = getattr(module, attr, _MISSING)
    if target is _MISSING:
        raise PluginError(f"{env_name}: the plugin module has no such attribute")
    if not callable(target):
        raise PluginError(f"{env_name}: the plugin attribute is not callable")
    return target


def check_no_conflict(
    seam: str,
    environ: dict[str, str] | None = None,
    *,
    selector: str,
    selector_default: str | None = None,
) -> None:
    """Raise when another selector variable would pick a different adapter.

    Does not inspect the plugin variable itself — callers decide whether a
    plugin is being used.
    """
    env = os.environ if environ is None else environ
    name = plugin_env_name(seam)
    value = str(env.get(selector, "")).strip()
    if value and value != selector_default:
        raise PluginError(f"{name} conflicts with {selector}; unset one of them")


def load_plugin(
    seam: str,
    environ: dict[str, str] | None = None,
    *,
    selector: str | None = None,
    selector_default: str | None = None,
) -> Callable | None:
    """Return the factory selected for ``seam``, or None when no plugin is set."""
    env = os.environ if environ is None else environ
    name = plugin_env_name(seam)
    spec = plugin_spec(seam, env)
    if spec is None:
        return None
    if selector is not None:
        check_no_conflict(seam, env, selector=selector, selector_default=selector_default)
    factory = _resolve(name, spec)
    module_name, _, attr = spec.partition(":")
    logger.info(
        "plugin loaded",
        extra={
            "plugin_seam": seam,
            "plugin_module": module_name,
            "plugin_attr": attr,
        },
    )
    return factory


def load_plugin_list(
    seam: str, environ: dict[str, str] | None = None
) -> list[Callable]:
    """Return the ordered factories from ``PIPELINE_<SEAM>_PLUGINS``."""
    env = os.environ if environ is None else environ
    name = "PIPELINE_" + seam.upper() + "_PLUGINS"
    raw = str(env.get(name, "")).strip()
    if not raw:
        return []
    specs = [entry.strip() for entry in raw.split(",")]
    if any(not entry for entry in specs):
        raise PluginError(f"{name} has an empty entry")
    factories: list[Callable] = []
    for spec in specs:
        factory = _resolve(name, spec)
        module_name, _, attr = spec.partition(":")
        logger.info(
            "plugin loaded",
            extra={
                "plugin_seam": seam,
                "plugin_module": module_name,
                "plugin_attr": attr,
            },
        )
        factories.append(factory)
    return factories


def require_methods(obj: object, names, label: str) -> None:
    """Raise unless ``obj`` provides every named method as a callable."""
    missing = [name for name in names if not callable(getattr(obj, name, None))]
    if missing:
        raise PluginError(
            f"{label} plugin is missing required methods: {', '.join(missing)}"
        )
