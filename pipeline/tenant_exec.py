"""Command-wrap seam: an operator plugin may rewrite argv before repository code runs.

Repository-controlled commands (agent harnesses, reviews, tests, builds, setup,
lint, validation) run as plain subprocesses beside the control plane's
credentials. ``wrap`` is the one place such an argv can be rewritten, for
example into a container or a sandbox the operator provides.

With ``PIPELINE_COMMAND_WRAP_PLUGIN`` unset or blank there is no plugin and
``wrap`` returns the very list it was given. With it set, the plugin's answer
is the only answer: a failure to load, call or validate the plugin refuses the
command with ``CommandWrapError``, and the unwrapped argv is never used.

Cold import: this module imports only the standard library at module level.
The plugin loader and the actor helper load inside ``wrap``, on the configured
path only.
"""

from __future__ import annotations

import logging
import os
from typing import Any

WRAP_PLUGIN_ENV = "PIPELINE_COMMAND_WRAP_PLUGIN"
WRAP_KINDS = frozenset(
    {"agent", "review", "test", "build", "setup", "lint", "validate"}
)

_SEAM = "command_wrap"
_REFUSED = "command wrap refused"

logger = logging.getLogger("pipeline")

_cached: Any = None


class CommandWrapError(RuntimeError):
    """Raised when a configured command wrap refuses a command."""


def is_configured() -> bool:
    """Return True when ``PIPELINE_COMMAND_WRAP_PLUGIN`` is set and not blank."""
    return bool(os.environ.get(WRAP_PLUGIN_ENV, "").strip())


def wrap(argv: list[str], *, cwd: str, kind: str) -> list[str]:
    """Return the argv to run for ``argv``, rewritten by the plugin when configured.

    ``kind`` must be one of ``WRAP_KINDS``; anything else is a programming error.
    Unconfigured, the same list object comes back unchanged.
    """
    if kind not in WRAP_KINDS:
        raise ValueError(f"unknown command wrap kind {kind!r}")
    if not is_configured():
        return argv
    try:
        obj = _plugin()
        result = obj.wrap(list(argv), cwd=cwd, kind=kind, namespace=_namespace())
        if not _is_valid_argv(result):
            raise TypeError("malformed command wrap result")
    except Exception as exc:  # noqa: BLE001 - any failure to consult must refuse
        logger.warning(
            _REFUSED,
            extra={"wrap_kind": kind, "error_type": type(exc).__name__},
        )
        raise CommandWrapError(_REFUSED) from None
    return result


def reset_for_tests() -> None:
    """Drop the cached plugin object so the next ``wrap`` loads it afresh."""
    global _cached
    _cached = None


def _plugin() -> Any:
    """Return the built plugin object, loading it once per process."""
    global _cached
    if _cached is None:
        from app.plugin_loader import load_plugin, require_allowlist

        require_allowlist(_SEAM)
        obj = load_plugin(_SEAM)()
        if not callable(getattr(obj, "wrap", None)):
            raise TypeError("command wrap plugin has no wrap method")
        _cached = obj
    return _cached


def _namespace() -> str:
    from pipeline.actor import current_namespace

    return current_namespace()


def _is_valid_argv(result: object) -> bool:
    return (
        isinstance(result, list)
        and len(result) > 0
        and all(isinstance(item, str) for item in result)
    )
