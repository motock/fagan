"""Sanctioned manual unblock of a parked story (``python -m pipeline.unpark``).

A story that parks after repeated rework, or after a review finding, can only
get back into the pipeline once a human has resolved the underlying problem.
The intervention itself is allowed and must stay allowed -- but a hand edit of
the manifest bypasses the plan lock (racing the scheduler's ``advance_all_plans``
tick) and leaves no trace, so the manifest reads "never retried" and
``pipeline.local_success.classify_story`` reports nothing. The retro instrument
then lies about the window it is describing.

This module gives the intervention a path that records itself: it clears the
allowlisted retry counters and review records under the same flock the
scheduler tick and ``patch_story`` take, then appends a
``story_unparked_manually`` record to the plan's notification sidecar (the file
``pipeline.story_metrics.load_notification_records`` reads), so the unblock is
visible to every retro instrument.

Usage::

    python -m pipeline.unpark <plan> <story-key> --clear rework_attempts,last_review_findings
    python -m pipeline.unpark <plan> <story-key> --clear rework_attempts --status interrupted

``--clear`` takes a comma-separated list of field names, each of which must be
in :data:`UNPARKABLE_FIELDS`. Any other name -- ``status``, ``pr_url``,
``files``, ``agent_instructions``, ``acceptance``, ``pid``, ``worktree``, ... --
is refused with a ``ValueError`` naming it, before anything is read or written,
so a rejected invocation can never leave a partial write behind.

Fields are *deleted*, not zeroed: an absent key is the honest "not recorded"
state, whereas ``rework_attempts = 0`` would read as "retried zero times" and
would make a follow-up clear falsely report that it removed something.

``--status`` defaults to ``interrupted``: a parked story is not re-dispatched,
an interrupted one is. It accepts only the fixed set of statuses the pipeline
itself assigns.
"""

from __future__ import annotations

import argparse
import sys

from .parsers import _validate_key
from .persistence import _notify_user
from .store import FileStore

__all__ = ["UNPARKABLE_FIELDS", "clear_story_fields", "main"]

#: The retry counters and review records a human may clear to unblock a story.
#: Everything else in a story dict is pipeline-owned state and is refused.
UNPARKABLE_FIELDS: frozenset[str] = frozenset(
    {
        "acceptance_failed_review",
        "ci_rework",
        "escalated",
        "last_review_findings",
        "last_reviewed_logic_fingerprint",
        "last_reviewed_sha",
        "merge_attempts",
        "merge_park_evidence",
        "merge_parked_at",
        "parked_reason",
        "pre_escalation_backend",
        "pre_escalation_model",
        "review_deferred_count",
        "review_feedback",
        "review_inconclusive_count",
        "review_verdict",
        "rework_attempts",
        "rework_test_author_done_for_sha",
        "triage_actions",
        "triage_attempts",
    }
)

#: The statuses ``set_story_status`` accepts -- the fixed set the pipeline
#: itself assigns, so this is a sanctioned transition, not invented state.
_VALID_STATUSES: frozenset[str] = frozenset(
    {
        "todo",
        "in_progress",
        "interrupted",
        "failed",
        "tests_passed",
        "pr_open",
        "changes_requested",
        "parked",
        "done",
    }
)


def clear_story_fields(
    plan_name: str,
    story_key: str,
    fields: list[str],
    status: str | None = None,
) -> dict:
    """Clear ``fields`` from one story under the plan lock and record the unblock.

    Deletes each named field that is actually present (deletion, not zeroing),
    optionally sets ``status``, saves the manifest, and appends a
    ``story_unparked_manually`` notice to the plan's notification sidecar.

    Returns ``{"plan", "story", "cleared", "status"}`` where ``cleared`` is the
    sorted list of names actually removed.

    Raises:
        ValueError: ``plan_name`` or ``story_key`` is not a safe path key (the
            same boundary check every sibling entry point applies, since both
            flow into ``PLAN_DIR / f"{plan_name}.manifest.json"``), a name in
            ``fields`` is not in :data:`UNPARKABLE_FIELDS`, or ``status`` is not
            one the pipeline assigns. Raised before the lock is taken and before
            anything is read, so nothing is written.
        KeyError: the plan has no such story.
        RuntimeError: another dispatch/ingest/interrupt holds the plan lock.
        OSError: the manifest could not be written (no notice is emitted).
    """
    # Fail closed: validate the keys and every field name before the lock,
    # before any read.
    _validate_key(plan_name)
    _validate_key(story_key)
    for field in fields:
        if field not in UNPARKABLE_FIELDS:
            raise ValueError(
                f"cannot clear field {field!r}: only "
                f"{sorted(UNPARKABLE_FIELDS)} may be cleared by a manual unblock"
            )
    if status is not None and status not in _VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}: must be one of {sorted(_VALID_STATUSES)}"
        )

    store = FileStore()
    with store.transaction(plan_name) as acquired:
        if not acquired:
            raise RuntimeError(
                f"could not acquire the plan lock for {plan_name!r}: "
                "another dispatch/ingest/interrupt is in progress"
            )
        manifest = store.get_manifest(plan_name)
        story = manifest["stories"].get(story_key)
        if story is None:
            raise KeyError(f"no such story {story_key!r} in plan {plan_name!r}")

        # Dedupe while preserving order: a repeated name would otherwise be
        # deleted twice and raise KeyError on the second `del`.
        cleared = list(dict.fromkeys(field for field in fields if field in story))
        for field in cleared:
            del story[field]
        if status is not None:
            story["status"] = status
        store.save_manifest(plan_name, manifest)

    # Only after the save returns: a failed write must never claim the unblock
    # happened.
    _notify_user(
        plan_name,
        f"manual unblock: cleared {', '.join(cleared) or 'nothing'}"
        + (f"; status -> {status}" if status else ""),
        story_key=story_key,
        severity="warning",
        event="story_unparked_manually",
    )
    return {
        "plan": plan_name,
        "story": story_key,
        "cleared": sorted(cleared),
        "status": status,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on success, 1 on a refused or failed unblock."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.unpark",
        description=(
            "Clear a parked story's retry counters and review records under the "
            "plan lock, and record the manual unblock in the plan's notification "
            "sidecar."
        ),
    )
    parser.add_argument("plan", help="plan name")
    parser.add_argument("story", help="story key")
    parser.add_argument(
        "--clear",
        required=True,
        help="comma-separated field names to delete (see UNPARKABLE_FIELDS)",
    )
    parser.add_argument(
        "--status",
        default="interrupted",
        help="status to set on the story (default: interrupted)",
    )
    args = parser.parse_args(argv)

    fields = [token.strip() for token in args.clear.split(",") if token.strip()]
    try:
        result = clear_story_fields(args.plan, args.story, fields, status=args.status)
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"unparked {result['story']} in {result['plan']}: "
        f"cleared {', '.join(result['cleared']) or 'nothing'}"
        + (f"; status -> {result['status']}" if result["status"] else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
