"""Run operator-supplied intake plugins once per scheduler tick.

``PIPELINE_INTAKE_PLUGINS`` is a comma list of ``module:attr`` specs. Each
loaded callable is called as ``plugin(now)`` and may create plans from the
operator's own sources. Its return value is ignored.

An intake plugin creates plans, so the seam is refused unless the module
allowlist (``PIPELINE_PLUGIN_MODULES``) is set, and it does nothing while the
plan-approval gate is off: an ingested plan would otherwise run on the next
tick with no person's approval. The list is read on every call. A plugin that
raises is logged by exception type only, and the others still run.
"""

from __future__ import annotations

import logging
import os

from app.plugin_loader import ALLOWLIST_ENV, PluginError, load_plugin_list
from pipeline.plan_approval import approval_enabled

logger = logging.getLogger("pipeline.intake_plugins")

SEAM = "intake"
PLUGINS_ENV = "PIPELINE_INTAKE_PLUGINS"


def _approval_gate_on() -> bool:
    """Fail closed: an unrecognised approval setting counts as off."""
    try:
        return approval_enabled()
    except ValueError:
        return False


def run_intake_plugins(now: float) -> dict[str, int]:
    """Call each configured intake plugin once. Returns ``{"ran": n, "failed": m}``.

    Returns ``{}`` without importing anything unless ``PIPELINE_INTAKE_PLUGINS``
    names at least one plugin.
    """
    if not str(os.environ.get(PLUGINS_ENV, "")).strip():
        return {}
    if not str(os.environ.get(ALLOWLIST_ENV, "")).strip():
        raise PluginError(f"{PLUGINS_ENV} requires {ALLOWLIST_ENV}")
    if not _approval_gate_on():
        logger.warning("intake plugins skipped: plan approval gate is off")
        return {}
    counts = {"ran": 0, "failed": 0}
    for plugin in load_plugin_list(SEAM):
        try:
            plugin(now)
        except Exception as exc:  # noqa: BLE001 - one plugin never stops the others
            counts["failed"] += 1
            # Only the type is logged: an exception message can echo plugin data.
            logger.warning(
                "intake plugin failed",
                extra={"error_type": type(exc).__name__},
            )
            continue
        counts["ran"] += 1
    return counts
