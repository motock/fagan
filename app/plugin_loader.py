"""Operator-selected plugins for architectural seams.

A plugin is chosen ONLY by an operator-controlled environment variable of the
form ``PIPELINE_<SEAM>_PLUGIN=module:attr`` (or ``PIPELINE_<SEAM>_PLUGINS`` for
an ordered, comma-separated list) — never by request, plan, job or manifest
data. Because loading a plugin is code execution chosen by the environment, the
loader is off unless the variable is set, validates the spec strictly, and
fails closed: a bad spec raises :class:`PluginError` rather than silently
falling back to the built-in adapter.

Trust boundary — stated honestly: whoever can set the variable runs code in the
orchestrator. ``pipeline/__init__.py`` loads ``<repo_root>/.pipeline.env`` at
import time and, by an explicit precedence rule, that FILE WINS over the process
environment for every ``PIPELINE_*`` key. So *write access to the repository
checkout* (any agent, packaging step or process able to write that tree) is
equivalent to arbitrary code execution in the orchestrator. "Off unless the
variable is set" is the primary control; the checks below are defence in depth,
not a sandbox:

* the spec must match :data:`SPEC_RE` — a dotted identifier path, one colon,
  one identifier: no paths, whitespace or leading dot, and a trailing newline is
  rejected (``\\Z``, not ``$``);
* the attribute must be public, so ``_private``/``__dunder__`` reach is refused;
* the module must NOT be a standard-library, builtin or frozen module, so
  ``os:system``, ``subprocess:run`` or ``builtins:eval`` can never be a plugin;
* setting ``PIPELINE_PLUGIN_MODULES`` to comma-separated module prefixes makes
  that a hard allowlist: every module outside it is refused (recommended, and
  required on any host where more than one principal can write the tree);
* the resolved attribute must actually be defined by the requested module, so a
  re-export cannot smuggle a callable in from elsewhere.

IMPORTANT for callers: loading must never be triggered from request, plan, job
or repository content. Resolve a seam's plugin once at start-up
(:func:`load_plugin` / :func:`load_plugin_list`) and pass the factory down. Do
not re-read the environment per request and do not give any request a way to
name a module or attribute.

The overlay image can supply plugin modules because ``/app`` is the working
directory, e.g. ``overlay.adapters:build_authorizer``.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import sys
from collections.abc import Callable, Iterable

logger = logging.getLogger("app.plugin_loader")

# ``package.module:attr`` — dotted identifier path, single colon, identifier.
# ``\Z`` (not ``$``) so a trailing newline is rejected rather than matched.
SPEC_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*\Z"
)
# Seams are lowercase identifiers with underscores, e.g. ``event_bus``.
_SEAM_RE = re.compile(r"^[a-z][a-z_]*\Z")

# Optional operator allowlist: comma-separated module names (or package
# prefixes). When set, only matching modules may be used as plugins.
ALLOWLIST_ENV = "PIPELINE_PLUGIN_MODULES"
# Hard cap on a plugin list, so one variable cannot import unboundedly.
MAX_PLUGINS = 64

_MISSING = object()


def _plugin_names(seam: str) -> tuple[str, str]:
    """Return the single and list variable names for ``seam``.

    Both are derived from :func:`plugin_env_name`, so an invalid seam raises
    ``ValueError`` here instead of silently producing an unchecked name.
    """
    singular = plugin_env_name(seam)
    return singular, singular + "S"


def _reject_two_sources(seam: str, env) -> tuple[str, str]:
    """Raise when both the single and the list variable are set for ``seam``."""
    singular, plural = _plugin_names(seam)
    if str(env.get(singular, "")).strip() and str(env.get(plural, "")).strip():
        raise PluginError(f"set only one of {singular} and {plural}")
    return singular, plural


def _allowlist(env) -> tuple[str, ...]:
    """Return the configured module allowlist (empty tuple means unset)."""
    raw = str(env.get(ALLOWLIST_ENV, "")).strip()
    if not raw:
        return ()
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())


def _check_module_permitted(env_name: str, module_name: str, env) -> None:
    """Refuse modules that could never be plugin code, before importing them.

    Standard-library, builtin and frozen modules are refused outright: they are
    where the process-executing callables live (``os``, ``posix``,
    ``subprocess``, ``shutil``, ``builtins``, ...). An explicit
    ``PIPELINE_PLUGIN_MODULES`` allowlist narrows this further.
    """
    top_level = module_name.split(".")[0]
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    if top_level in stdlib or top_level in sys.builtin_module_names:
        raise PluginError(f"{env_name}: standard-library modules cannot be plugins")
    allowlist = _allowlist(env)
    if allowlist and not any(
        module_name == prefix or module_name.startswith(prefix + ".")
        for prefix in allowlist
    ):
        # The offending module name is deliberately not echoed.
        raise PluginError(f"{env_name}: the plugin module is not in {ALLOWLIST_ENV}")


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


def require_allowlist(seam: str, environ: dict[str, str] | None = None) -> None:
    """Refuse ``seam``'s plugin unless the module allowlist is configured.

    The identity and authorizer seams are the security-sensitive pair, so an
    operator who can write the repository tree must not be able to install one
    without ``PIPELINE_PLUGIN_MODULES``. The check runs before any import, so a
    refused plugin is never even loaded: this function never imports anything
    and never inspects the spec's module. A seam with no plugin selected passes,
    which keeps the built-in defaults working with the variable unset.
    """
    env = os.environ if environ is None else environ
    if plugin_spec(seam, env) is not None and not _allowlist(env):
        raise PluginError(f"{plugin_env_name(seam)} requires {ALLOWLIST_ENV}")


def _resolve(env_name: str, spec: str, environ=None) -> Callable:
    """Import ``spec`` and return its factory, or raise :class:`PluginError`.

    Every static check runs before the import, so a refused spec never imports
    anything.
    """
    if not SPEC_RE.match(spec):
        raise PluginError(
            f"{env_name} is not a valid plugin spec; expected 'module:attr'"
        )
    module_name, _, attr = spec.partition(":")
    if attr.startswith("_"):
        raise PluginError(f"{env_name}: the plugin attribute must be public")
    env = os.environ if environ is None else environ
    _check_module_permitted(env_name, module_name, env)
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
    # The attribute must belong to the plugin module itself, so importing a
    # module that merely re-exports ``os.system`` cannot smuggle it in.
    owner = str(getattr(target, "__module__", "") or "")
    if not (owner == module_name or owner.startswith(module_name + ".")):
        raise PluginError(
            f"{env_name}: the plugin attribute is not defined by the plugin module"
        )
    return target


def check_no_conflict(
    seam: str,
    environ: dict[str, str] | None = None,
    *,
    selector: str,
    selector_default: str | None = None,
    env_name: str | None = None,
) -> None:
    """Raise when another selector variable would pick a different adapter.

    Does not inspect the plugin variable itself — callers decide whether a
    plugin is being used. ``env_name`` names the variable in the error message
    and defaults to the single-plugin variable; the list loader passes its own
    so the message points at the variable the operator actually set.
    """
    env = os.environ if environ is None else environ
    name = plugin_env_name(seam) if env_name is None else env_name
    value = str(env.get(selector, "")).strip()
    if value and value != selector_default:
        raise PluginError(f"{name} conflicts with {selector}; unset one of them")


def load_plugin(
    seam: str,
    environ: dict[str, str] | None = None,
    *,
    selector: str | None = None,
    selector_default: str | None = None,
    required_methods: Iterable[str] | None = None,
) -> Callable | None:
    """Return the factory selected for ``seam``, or None when no plugin is set.

    ``selector``/``selector_default`` describe a legacy adapter selector for the
    same seam: when the plugin variable is set, a disagreeing selector is a
    configuration error rather than a silent win for one of the two.

    ``required_methods`` marks the seam as security-critical: protocol
    conformance is enforced here, and the absence of a plugin is logged as a
    WARNING so that running a security seam without its plugin is auditable.
    """
    env = os.environ if environ is None else environ
    name, _plural = _reject_two_sources(seam, env)
    spec = plugin_spec(seam, env)
    if spec is None:
        if required_methods:
            logger.warning(
                "no plugin configured for a security-sensitive seam",
                extra={"plugin_seam": seam, "plugin_env": name},
            )
        return None
    if selector is not None:
        check_no_conflict(
            seam, env, selector=selector, selector_default=selector_default
        )
    factory = _resolve(name, spec, env)
    if required_methods:
        require_methods(factory, required_methods, seam)
    module_name, _, attr = spec.partition(":")
    logger.info(
        "plugin loaded",
        extra={
            "plugin_seam": seam,
            "plugin_env": name,
            "plugin_module": module_name,
            "plugin_attr": attr,
        },
    )
    return factory


def load_plugin_list(
    seam: str,
    environ: dict[str, str] | None = None,
    *,
    selector: str | None = None,
    selector_default: str | None = None,
    required_methods: Iterable[str] | None = None,
) -> list[Callable]:
    """Return the ordered factories from ``PIPELINE_<SEAM>_PLUGINS``.

    Applies the same controls as :func:`load_plugin`: the seam is validated, the
    two variables for one seam are mutually exclusive, a disagreeing
    ``selector`` is an error, and the module allowlist is enforced.
    """
    env = os.environ if environ is None else environ
    _, name = _reject_two_sources(seam, env)
    raw = str(env.get(name, "")).strip()
    if not raw:
        if required_methods:
            logger.warning(
                "no plugin configured for a security-sensitive seam",
                extra={"plugin_seam": seam, "plugin_env": name},
            )
        return []
    specs = [entry.strip() for entry in raw.split(",")]
    if any(not entry for entry in specs):
        raise PluginError(f"{name} has an empty entry")
    if len(specs) > MAX_PLUGINS:
        raise PluginError(f"{name} lists more than {MAX_PLUGINS} plugins")
    if len(set(specs)) != len(specs):
        raise PluginError(f"{name} lists the same plugin more than once")
    if selector is not None:
        check_no_conflict(
            seam,
            env,
            selector=selector,
            selector_default=selector_default,
            env_name=name,
        )
    factories: list[Callable] = []
    for spec in specs:
        factory = _resolve(name, spec, env)
        if required_methods:
            require_methods(factory, required_methods, seam)
        module_name, _, attr = spec.partition(":")
        logger.info(
            "plugin loaded",
            extra={
                "plugin_seam": seam,
                "plugin_env": name,
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
