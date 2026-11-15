"""Overlord executor for ``resume_review`` (ov-04): park class ``agent_failed_after_push``.

A story whose agent pushed commits and then failed has a branch head no review
has seen. This executor returns such a story to ``tests_passed``, the status
``review_story`` reviews, after clearing the review records that would
otherwise skip the new head or report the old verdict.

The class check (``classify_park``) is what establishes that the story has a
recorded head beyond base with no verdict for it. A verdict that cannot be
bound to a head fails closed. A risk-high story is never resumed here.

Every refusal parks the story for a human with the reason stated, and nothing
is written before the unpark step succeeds.
"""

from __future__ import annotations

from .module_ref import _ModuleRef
from .persistence import _notify_user
from .triage_park_classes import check_ruling_allowed, classify_park
from .unpark import clear_story_fields

_park = _ModuleRef("pipeline.triage", "_park")
_triage_mode = _ModuleRef("pipeline.triage", "_triage_mode")

#: Review records cleared so ``review_story`` reviews the recorded head afresh.
RESUME_CLEARED_FIELDS = frozenset(
    {
        "last_review_findings",
        "last_reviewed_logic_fingerprint",
        "last_reviewed_sha",
        "parked_reason",
        "review_feedback",
        "review_verdict",
    }
)


def _verdict_blocks_head(story: dict, head: str) -> bool:
    """True when a stored verdict covers ``head``, or cannot be tied to any head."""
    if not story.get("review_verdict"):
        return False
    bound = story.get("review_verdict_sha")
    return not isinstance(bound, str) or not bound or bound == head


def _notify(plan_name, story_key, story, message: str, event: str) -> None:
    _notify_user(
        plan_name,
        message,
        story_key=story_key,
        event=event,
        **({"correlation_id": story["correlation_id"]} if story.get("correlation_id") else {}),
    )


def execute_resume_review(plan_name, story_key, story, ruling, manifest) -> str:
    """Return a failed, unreviewed branch head to ``tests_passed`` for review.

    Shadow mode returns ``"shadow"`` and touches nothing. In execute mode the
    story is unparked through ``clear_story_fields`` first; only after that
    write succeeds is the in-memory story changed, so the sweep persists the
    same state the store holds.
    """
    if _triage_mode() != "execute":
        return "shadow"
    head = story.get("last_commit")
    if not isinstance(head, str) or not head:
        return _park(
            plan_name, story_key, story,
            "resume_review refused: no commit is recorded beyond the base; parked for a human",
        )
    if str(story.get("risk", "")).lower() == "high":
        return _park(
            plan_name, story_key, story,
            "resume_review refused: risk-high story; parked for a human",
        )
    park = classify_park(story, {})
    if not check_ruling_allowed(park, "resume_review"):
        return _park(
            plan_name, story_key, story,
            f"resume_review refused for park class {park.name}; parked for a human",
        )
    if _verdict_blocks_head(story, head):
        return _park(
            plan_name, story_key, story,
            "resume_review refused: a review verdict covers this head; parked for a human",
        )
    try:
        clear_story_fields(
            plan_name, story_key, sorted(RESUME_CLEARED_FIELDS), status="tests_passed"
        )
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        return _park(
            plan_name, story_key, story,
            f"resume_review failed to unpark ({type(exc).__name__}); parked for a human",
        )
    for field in RESUME_CLEARED_FIELDS:
        story.pop(field, None)
    story["status"] = "tests_passed"
    _notify(plan_name, story_key, story,
            f"{story_key} resumed by overlord: failed branch head has no review; queued for review",
            "story_resumed_by_overlord")
    return "resume_review"
