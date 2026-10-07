"""The bound on consecutive rate-limited review deferrals.

See `pipeline.review_orchestrator`, which calls both functions here from the two
places a rate-limited review is deferred.

Live 2026-10-06: the review path was rate-limited for 51 minutes and emitted 52
identical "will retry next tick" notices with no escalation. The only escape
hatch, PIPELINE_REVIEW_FALLBACK, defaults to "off", so its fallback_after
threshold was unreachable and the retry was genuinely unbounded. This module
owns the bound that turns such a streak into a park.

Kept out of review_orchestrator.py, which already sits at the 1000-line
production cap that scripts/check_line_limit.py enforces.
"""

from __future__ import annotations

import os
from typing import Any

from .review_refs import _ServerRef

_notify_user = _ServerRef("_notify_user")
_atomic_write_json = _ServerRef("_atomic_write_json")

REVIEW_DEFER_PARK_AFTER_DEFAULT = 30


def review_defer_park_after() -> int:
    """Consecutive rate-limited review deferrals to tolerate before parking.

    Zero or negative disables the bound, restoring unbounded deferral. An empty
    or non-numeric value falls back to REVIEW_DEFER_PARK_AFTER_DEFAULT rather
    than raising: this runs on the scheduler's tick, and a typo in a config var
    must not take a tick down.
    """
    raw = os.environ.get("PIPELINE_REVIEW_DEFER_PARK_AFTER", "").strip()
    if not raw:
        return REVIEW_DEFER_PARK_AFTER_DEFAULT
    try:
        return int(raw)
    except ValueError:
        return REVIEW_DEFER_PARK_AFTER_DEFAULT


def park_rate_limited_review(
    plan_name: str,
    story_key: str,
    story: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Any,
    **cid_kwargs: Any,
) -> dict[str, Any]:
    """Park a story whose review has been rate-limited too many times running.

    A single deferral is a transient blip that self-heals on the next tick; an
    unbounded streak is not. Parking hands the story back to a human instead of
    retrying forever. It deliberately does not touch rework_attempts: this is an
    outage on the review backend, not a fault of the story, and charging the
    story's rework budget for it would eventually park a correct implementation.
    """
    story["status"] = "parked"
    story["parked_reason"] = (
        f"review backend rate-limited on {story['review_deferred_count']} "
        f"consecutive attempts; set PIPELINE_REVIEW_DEFER_PARK_AFTER=0 to retry "
        f"unbounded"
    )
    _notify_user(
        plan_name,
        f"{story_key} parked: {story['parked_reason']}.",
        **cid_kwargs,
    )
    _atomic_write_json(manifest_path, manifest)
    return {"ok": False, "status": "parked", "deferred": "rate_limited"}
