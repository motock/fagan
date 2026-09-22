"""Advance-owned merge-hold re-adjudication.

The names this module reads that are bound at module level in
``pipeline.advance`` (``PIPELINE_AUTONOMY``, ``_merge_decision``,
``_merge_mod``, ``merge_adjudication_plan``) are resolved through
``pipeline.advance`` at call time via ``_ModuleRef``, so
``monkeypatch.setattr(pipeline.advance, NAME, ...)`` keeps landing.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .module_ref import _ModuleRef

PIPELINE_AUTONOMY = _ModuleRef("pipeline.advance", "PIPELINE_AUTONOMY")
_merge_decision = _ModuleRef("pipeline.advance", "_merge_decision")
_merge_mod = _ModuleRef("pipeline.advance", "_merge_mod")
merge_adjudication_plan = _ModuleRef("pipeline.advance", "merge_adjudication_plan")


_MERGE_HOLD_REASON = "high risk held for human review"


def _readjudicate_parked_merge_hold(
    plan_name: str, key: str, story: dict[str, Any]
) -> dict[str, Any] | None:
    """Re-run the merge gate for a parked high-risk hold whose evidence changed.

    MERGEPARK-2: a merge-gate park used to be terminal - the loop below
    skipped every story whose status was not ``pr_open``, so a story parked
    citing missing PR checks (WAP-1, parked 2026-09-15T01:26) was never
    revisited after ``gh pr checks`` turned green minutes later. A park is
    re-examined only when ALL of the following hold:

    * the park is the merge gate's own high-risk hold (the exact reason
      string - human parks, triage parks and any other reason are never
      touched);
    * the story is approved and carries a ``pr_url``;
    * the park recorded the evidence the ruling was made on
      (``merge_park_evidence`` - parks created before this feature, and
      human/triage parks, have none);
    * autonomy is ``full``: gated and dry-run never adjudicated in the first
      place, so they must never re-adjudicate - checked BEFORE any gather.

    The CURRENT single-poll CI state is gathered with the non-polling
    ``_ci_status_once`` (never the blocking ``_ci_status`` poller), the branch
    resolved exactly like the merge path below does. Only a DIFFERING state
    re-invokes the gate, so the cost is one overlord call per evidence
    TRANSITION, not per tick. A ``merge`` ruling clears the snapshot, flips
    the story to ``pr_open`` and lets the caller fall through into the same
    merge path a pr_open story takes; a ``park`` ruling leaves the story
    parked and the caller's park branch refreshes the snapshot. A gather
    failure leaves the old snapshot intact and the story parked - the tick
    must survive it.

    Returns the fresh gate decision, or ``None`` when there is nothing to
    re-adjudicate.
    """
    if story["status"] != "parked":
        return None
    if story.get("parked_reason") != _MERGE_HOLD_REASON:
        return None
    if story.get("review_verdict") != "APPROVE":
        return None
    if not story.get("pr_url"):
        return None
    snapshot = story.get("merge_park_evidence")
    if not isinstance(snapshot, dict):
        return None
    # Dereference once: PIPELINE_AUTONOMY is a _ServerRef proxy (see the
    # dry-run preview above for why the raw proxy is not used in comparisons
    # that outlive this expression).
    if PIPELINE_AUTONOMY._value() != "full":
        return None

    from .ci import _ci_status_once
    from .pr import _resolve_story_branch

    worktree = story.get("worktree", "")
    if worktree and Path(worktree).is_dir():
        branch = _resolve_story_branch(worktree, key)
    else:
        # No worktree to probe: hand the gather no branch at all, exactly
        # like the merge path - a locally computed convention branch is the
        # exact mistake the round-2 review finding names.
        branch = ""
    try:
        current = _ci_status_once(branch, sha="")
    except Exception:  # noqa: BLE001 (fail-open by design; the tick must survive it)
        logging.getLogger("pipeline").warning(
            "%s merge-park re-adjudication: CI gather failed; keeping the "
            "recorded evidence",
            key,
        )
        return None
    if current == snapshot.get("pr_checks"):
        # Flap guard: an unchanged state never re-invokes the overlord.
        return None

    logging.getLogger("pipeline").info(
        "%s merge-park evidence changed; re-adjudicating the hold", key
    )
    story["pr_checks"] = current
    with merge_adjudication_plan(plan_name), _merge_mod.merge_adjudication_story(key):
        decision = _merge_decision(story)
    if decision["action"] == "merge":
        story.pop("merge_park_evidence", None)
        story.pop("merge_parked_at", None)
        # Flip FIRST, so a merge path that then blocks (pending CI, failed
        # rebase) leaves the story in the ordinary pr_open state that path
        # already knows how to retry.
        story["status"] = "pr_open"
    else:
        # Stay parked: refresh the snapshot to the fresh value. This is the
        # flap guard - the next tick compares equal and never re-invokes the
        # gate, so the cost is one overlord call per evidence TRANSITION.
        story["merge_park_evidence"] = {"pr_checks": story.get("pr_checks")}
        story["merge_parked_at"] = datetime.now(timezone.utc).isoformat()
    return decision


