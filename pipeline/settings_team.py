"""The team whose settings apply to a plan.

A team's settings are written by the console keyed by the tenancy team id, so a
plan's settings team comes from its namespace, not from the free-text manifest
``team`` label. With ``PIPELINE_SETTINGS_TEAM_PLUGIN`` unset the manifest team
(or ``DEFAULT_TEAM``) is used, as it always was.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.plugin_loader import (
    load_plugin,
    plugin_spec,
    require_allowlist,
    require_methods,
)

from .authorization import DEFAULT_TEAM
from .ingest_plan_fields import _TEAM_RE

logger = logging.getLogger("pipeline.settings_team")

SETTINGS_TEAM_PLUGIN_ENV = "PIPELINE_SETTINGS_TEAM_PLUGIN"
SEAM = "settings_team"
PROVIDER_METHODS = ("team_for_namespace",)
DEFAULT_NAMESPACE = "default"

# Built providers, keyed by spec. Answers are never cached: they depend on the
# namespace each call names.
_PLUGIN_TEAMS: dict[str, Any] = {}
_PLUGIN_LOCK = threading.Lock()


def _provider(spec: str) -> Any:
    """Return the built provider for ``spec``, loading it once per process.

    Load and conformance errors propagate: a bad selection is an operator error,
    not a plan-level answer, so it must not silently pick a team.
    """
    with _PLUGIN_LOCK:
        cached = _PLUGIN_TEAMS.get(spec)
        if cached is not None:
            return cached
        # Deny by default: the team decides which settings gate a plan's test
        # scope, autonomy and merge, so the module allowlist is required.
        require_allowlist(SEAM)
        factory = load_plugin(SEAM)
        built = factory()
        require_methods(built, PROVIDER_METHODS, SEAM)
        _PLUGIN_TEAMS[spec] = built
        return built


def _is_team(answer: object) -> bool:
    return isinstance(answer, str) and _TEAM_RE.match(answer) is not None


def settings_team(manifest: dict) -> str:
    """Return the team whose settings layers apply to the plan ``manifest``.

    ``None`` from the plugin means no opinion and keeps the manifest team. An
    answer that is not a team slug, or a plugin that raises, yields
    ``DEFAULT_TEAM``, so a failure never applies a team's looser settings. Only
    the exception type is logged: neither the namespace nor the answer is.
    """
    manifest_team = manifest.get("team") or DEFAULT_TEAM
    spec = plugin_spec(SEAM)
    if spec is None:
        return manifest_team
    provider = _provider(spec)
    namespace = manifest.get("namespace") or DEFAULT_NAMESPACE
    try:
        answer = provider.team_for_namespace(namespace)
    except Exception as exc:  # noqa: BLE001 - any failure must fail to the default team
        logger.warning(
            "settings team plugin failed; using the default team",
            extra={"plugin_seam": SEAM, "error_type": type(exc).__name__},
        )
        return DEFAULT_TEAM
    if answer is None:
        return manifest_team
    if not _is_team(answer):
        logger.warning(
            "settings team plugin returned an invalid team; using the default team",
            extra={"plugin_seam": SEAM},
        )
        return DEFAULT_TEAM
    return answer
