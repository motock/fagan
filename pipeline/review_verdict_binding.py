"""Bind a review verdict to the sha it actually graded (issue 9012b28e, D-C).

``review_verdict`` alone cannot say which commit the reviewer looked at: the
story keeps moving (rework, a racing commit) and a later trust check compares
the verdict against whatever HEAD is *now*. Recording the sha captured BEFORE
the reviewer was invoked makes the verdict self-describing, so a consumer can
require ``review_verdict == "APPROVE"`` *and* ``review_verdict_sha == head``
instead of trusting an unbound approval.

Strictly additive: ``review_verdict`` keeps its value and its lifecycle, so no
existing merge gate changes behaviour.
"""

from datetime import datetime


def bind_verdict(
    story: dict, verdict: str, reviewed_sha: str | None, now: datetime
) -> None:
    """Record ``verdict`` and the sha it graded on ``story``, in place.

    ``reviewed_sha`` is the HEAD captured before the reviewer ran. ``None`` or
    empty (no worktree, unreadable git) is stored as ``""``: an empty binding
    means "unbound, untrusted" and must never satisfy a trust check.
    """
    story["review_verdict"] = verdict
    story["review_verdict_sha"] = reviewed_sha or ""
    story["review_verdict_ts"] = now.isoformat()
