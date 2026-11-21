"""Audience seam: who the members of a notice audience are for a plan.

``PIPELINE_AUDIENCE_PLUGIN=module:attr`` names a ``factory()`` returning an
object with ``members(namespace, plan, audience, owner) -> frozenset[str]``
(subjects). With no plugin, ``plan_owner`` is the plan's owner and
``plan_approvers`` is empty: core has no tenancy.

Fail closed: a plugin that cannot be loaded, raises, or returns anything but a
set of non-empty strings resolves that audience to NOBODY. Only the exception
type is logged, never a subject.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.plugin_loader import load_plugin, plugin_spec, require_allowlist

logger = logging.getLogger(__name__)

AUDIENCE_PLUGIN_ENV = "PIPELINE_AUDIENCE_PLUGIN"
AUDIENCES = frozenset({"plan_owner", "plan_approvers"})
_SEAM = "audience"

AudienceMembers = Callable[[str], frozenset[str]]


def _fallback(audience: str, owner: str) -> frozenset[str]:
    if audience == "plan_owner" and owner:
        return frozenset({owner})
    return frozenset()


def audience_members(namespace: str, plan: str, audience: str, owner: str) -> frozenset[str]:
    """Return the subjects in ``audience`` for ``plan``; empty on any failure."""
    try:
        if plugin_spec(_SEAM) is None:
            return _fallback(audience, owner)
        require_allowlist(_SEAM)
        result = load_plugin(_SEAM)().members(namespace, plan, audience, owner)
    except Exception as exc:  # noqa: BLE001 - fail closed on any plugin failure
        logger.warning(
            "audience plugin failed; resolving to nobody: audience=%s error_type=%s",
            audience,
            type(exc).__name__,
        )
        return frozenset()
    if not isinstance(result, (frozenset, set)) or not all(
        isinstance(s, str) and s for s in result
    ):
        logger.warning(
            "audience plugin returned an invalid value; resolving to nobody: audience=%s",
            audience,
        )
        return frozenset()
    return frozenset(result)


def members_for(namespace: str, plan: str, owner: str) -> AudienceMembers:
    """Bind ``audience_members`` to one (namespace, plan, owner)."""
    return lambda audience: audience_members(namespace, plan, audience, owner)
