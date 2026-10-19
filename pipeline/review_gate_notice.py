"""Transition state for the plan-level "review backend gated" notice.

Background: ``pipeline/advance.py``'s review block notifies the operator with
"Review backend gated (<reason>): deferring review." whenever the review
backend is gated and at least one story is ``tests_passed``. That notice used
to fire on EVERY scheduler tick, because ``_notify_user`` records a
``dedup_key`` but does not dedupe - dedupe is caller-side in this codebase
(see ``pipeline/wedge_io.py``'s ``_WEDGE_LAST_EMIT`` and
``pipeline/watchers.py``). Measured impact: overlord-failure-triage
accumulated 159 identical records in 3.2 hours (51 in one hour), and M56-1
emitted 4 in three minutes on 2026-10-19.

This module holds the "already told them" bit for that notice: one notice per
gated episode per plan, re-armed when review actually proceeds (the caller
calls :func:`clear_review_gate_notice` from the ``review_ok`` branch).

The state is in-process, exactly like ``wedge_io``'s cooldown: a restart
re-notifies once, which is the desired behaviour (a fresh process has no way
to know the operator was already told, and one extra notice after a restart
is far cheaper than silence).

The key is the PLAN, never the reason. A gate reason can embed a measured
value - free memory, a usage percentage - so keying on it would produce a
different key every tick and re-notify every tick, which is the very bug this
module exists to fix.
"""

from __future__ import annotations

_NOTIFIED: set[str] = set()


def should_notify_review_gate(plan_name: str) -> bool:
    """True the first time this plan's gate is seen in an episode."""
    if plan_name in _NOTIFIED:
        return False
    _NOTIFIED.add(plan_name)
    return True


def clear_review_gate_notice(plan_name: str) -> None:
    """Drop this plan's gate state: review proceeded this tick."""
    _NOTIFIED.discard(plan_name)
