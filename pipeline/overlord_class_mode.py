"""Per-class promotion of overlord decisions through the authority matrix.

``PIPELINE_TRIAGE_MODE`` used to promote every decision class at once. This
module lets the authority matrix (``overlord-authority.json``, ADR 0014) decide
one class at a time: the triage ruling path and the high-risk merge gate ask
:func:`class_mode` what a class may do, and the answer is the global switch
folded with that class's tier.

An unreadable matrix can never grant a merge: ``high_risk_merge`` is always
governed, so it resolves to ``human`` whenever the matrix cannot say otherwise.
A park class the matrix does not name follows ``triage_mode`` alone.
"""

from __future__ import annotations

from .authority_matrix import HUMAN_TIER, TIERS, FileLayerSource

#: Classes that stay governed even when the matrix is unreadable or silent.
ALWAYS_GOVERNED = frozenset({"high_risk_merge"})

_MODE_BY_TIER = {"auto": "execute", "auto+notify": "notify", HUMAN_TIER: "shadow"}


def matrix_tier(decision_class: str | None) -> str | None:
    """Return the matrix tier for ``decision_class``, or ``None`` if ungoverned.

    ``None`` means the matrix does not govern the class and ``triage_mode``
    decides alone. An unusable matrix, or a class with no tier value, resolves
    to ``human`` for an always-governed class and to ``None`` otherwise.
    """
    resolved = FileLayerSource().resolve()
    value = None
    if resolved is not None and isinstance(decision_class, str):
        value = resolved.values.get(f"{decision_class}.tier")
    if value is None:
        return HUMAN_TIER if decision_class in ALWAYS_GOVERNED else None
    if value not in TIERS:
        return HUMAN_TIER
    return value


def class_mode(decision_class: str | None, triage_mode: str) -> str:
    """Return ``shadow``, ``notify`` or ``execute`` for one decision class.

    ``triage_mode`` other than ``execute`` is the global kill switch and always
    holds. Otherwise the class's tier decides: ``auto`` executes, ``auto+notify``
    executes and notifies, and ``human`` (or any tier this module does not know)
    stays in shadow.
    """
    if triage_mode != "execute":
        return "shadow"
    tier = matrix_tier(decision_class)
    if tier is None:
        return "execute"
    return _MODE_BY_TIER.get(tier, "shadow")
