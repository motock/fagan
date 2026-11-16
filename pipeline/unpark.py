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

``--add-files a.py,b.py`` appends production paths to the story's ``files``
(the sanctioned replacement for hand-editing the manifest). Each path is checked
before anything is written; ``files`` is part of the plan approval digest, so
the CLI says the plan must be re-approved. Every successful unpark also records
the operator's resolution beside the overlord's latest ruling for the story
(:mod:`pipeline.triage_outcomes`); ``--human-action`` names it explicitly.

``--status`` defaults to ``interrupted``: a parked story is not re-dispatched,
an interrupted one is. It accepts only the fixed set of statuses the pipeline
itself assigns.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING

from . import triage_outcomes
from .actor import actor_id
from .parsers import _validate_key
from .persistence import _notify_user
from .store import build_store

if TYPE_CHECKING:
    from .store import Store

__all__ = ["UNPARKABLE_FIELDS", "clear_story_fields", "main", "unpark_story"]

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


def _validate_added_files(add_files: list[str]) -> list[str]:
    """Return ``add_files`` deduplicated in order, or raise on an unsafe path.

    The same shape a plan's ``files`` entries must have at ingest: a non-empty
    repo-relative POSIX path with no leading ``/``, no backslash, no NUL byte,
    and no ``..`` component. The error names the entry's position, never its
    value, so a refused path is not echoed into a log line.
    """
    checked: list[str] = []
    for index, path in enumerate(add_files, start=1):
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or "\x00" in path
            or ".." in path.split("/")
        ):
            raise ValueError(
                f"invalid --add-files entry #{index}: must be a non-empty "
                "repo-relative path with no leading '/', no backslash, and no '..'"
            )
        if path not in checked:
            checked.append(path)
    return checked


def _derive_human_action(
    files_added: list[str], prior_status: str | None, status: str | None
) -> str:
    """Name what the operator did, from the change they made.

    Widening ``files`` is an ``amend_scope``. Moving a failed story to
    ``tests_passed`` is a ``resume_review``; sending a story back as
    ``changes_requested`` is a ``rebrief``. Anything else is ``other`` -- the
    operator can name it explicitly with ``--human-action``.
    """
    if files_added:
        return "amend_scope"
    if prior_status == "failed" and status == "tests_passed":
        return "resume_review"
    if status == "changes_requested":
        return "rebrief"
    return "other"


def _apply_unpark(
    store: Store,
    plan_name: str,
    story_key: str,
    fields: list[str],
    status: str | None,
    new_paths: list[str],
) -> dict:
    """Apply the unblock to the manifest with the plan lock already held.

    The lock is the caller's: :func:`unpark_story` opens it for the CLI, the
    ``unblock_story`` tool calls this through :func:`unpark_story` while its own
    transaction is open. Nothing here acquires it, so a store whose lock is held
    per session is never asked for it twice.

    Returns ``{"cleared", "prior_status", "new_status", "files_added"}``;
    ``cleared`` keeps manifest order and ``files_added`` lists only new paths.
    """
    manifest = store.get_manifest(plan_name)
    story = manifest["stories"].get(story_key)
    if story is None:
        raise KeyError(f"no such story {story_key!r} in plan {plan_name!r}")

    prior_status = story.get("status")
    # Dedupe while preserving order: a repeated name would otherwise be
    # deleted twice and raise KeyError on the second `del`.
    cleared = list(dict.fromkeys(field for field in fields if field in story))
    for field in cleared:
        del story[field]
    declared = story.get("files") or []
    files_added = [path for path in new_paths if path not in declared]
    if files_added:
        story["files"] = [*declared, *files_added]
    if status is not None:
        story["status"] = status
    store.save_manifest(plan_name, manifest)
    return {
        "cleared": cleared,
        "prior_status": prior_status,
        "new_status": story.get("status"),
        "files_added": files_added,
    }


def unpark_story(
    plan_name: str,
    story_key: str,
    fields: list[str],
    status: str | None = None,
    *,
    add_files: list[str] | tuple[str, ...] = (),
    human_action: str | None = None,
    store: Store | None = None,
) -> dict:
    """Clear ``fields`` from one story under the plan lock and record the unblock.

    Deletes each named field that is actually present (deletion, not zeroing),
    optionally sets ``status``, appends each path in ``add_files`` that the
    story does not already declare to its ``files`` list, saves the manifest,
    and appends a ``story_unparked_manually`` notice to the plan's notification
    sidecar.

    ``human_action`` names the resolution the operator CLI records for this
    unblock; when not given it is derived by :func:`_derive_human_action`. The
    resolution record itself is written by :func:`main`, never here, so an
    automated caller that goes through :func:`clear_story_fields` does not record
    a human decision.

    ``store`` is for a caller that already holds the plan lock in an open
    transaction (the ``unblock_story`` tool): the unblock is then applied through
    that store without taking the lock a second time. When it is ``None`` -- the
    CLI and every other caller -- this function builds the configured store and
    takes the lock itself, exactly as before.

    Returns ``{"plan", "story", "cleared", "status", "new_status", "files_added",
    "human_action"}`` where ``cleared`` is the sorted list of names actually
    removed, ``new_status`` is the story's status after the save, and
    ``files_added`` lists only the paths that were newly declared.

    Raises:
        ValueError: ``plan_name`` or ``story_key`` is not a safe path key (the
            same boundary check every sibling entry point applies, since both
            flow into ``PLAN_DIR / f"{plan_name}.manifest.json"``), a name in
            ``fields`` is not in :data:`UNPARKABLE_FIELDS`, ``status`` is not
            one the pipeline assigns, an ``add_files`` entry is unsafe, or
            ``human_action`` is not in :data:`triage_outcomes.HUMAN_ACTIONS`.
            Raised before the lock is taken and before anything is read, so
            nothing is written.
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
    new_paths = _validate_added_files(list(add_files))
    if human_action is not None and human_action not in triage_outcomes.HUMAN_ACTIONS:
        raise ValueError(
            f"invalid human action {human_action!r}: must be one of "
            f"{sorted(triage_outcomes.HUMAN_ACTIONS)}"
        )

    if store is not None:
        # The caller already holds the plan lock in an open transaction (the
        # ``unblock_story`` tool passes its own store). Acquiring it here would
        # open a second session, and a lock held by the first session is never
        # granted to the second.
        outcome = _apply_unpark(
            store, plan_name, story_key, fields, status, new_paths
        )
    else:
        active_store = build_store()
        with active_store.transaction(plan_name) as acquired:
            if not acquired:
                raise RuntimeError(
                    f"could not acquire the plan lock for {plan_name!r}: "
                    "another dispatch/ingest/interrupt is in progress"
                )
            outcome = _apply_unpark(
                active_store, plan_name, story_key, fields, status, new_paths
            )

    # Only after the save returns: a failed write must never claim the unblock
    # happened.
    _notify_user(
        plan_name,
        f"manual unblock: cleared {', '.join(outcome['cleared']) or 'nothing'}"
        + (f"; status -> {status}" if status else ""),
        story_key=story_key,
        severity="warning",
        event="story_unparked_manually",
    )
    return {
        "plan": plan_name,
        "story": story_key,
        "cleared": sorted(outcome["cleared"]),
        "status": status,
        "new_status": outcome["new_status"],
        "files_added": outcome["files_added"],
        "human_action": human_action
        or _derive_human_action(
            outcome["files_added"], outcome["prior_status"], status
        ),
    }


def clear_story_fields(
    plan_name: str,
    story_key: str,
    fields: list[str],
    status: str | None = None,
) -> dict:
    """Clear ``fields`` and optionally set ``status``; return the unblock summary.

    The automated callers' entry point (the triage rebrief and rebase paths).
    It is :func:`unpark_story` with the summary narrowed to the four keys those
    callers have always read, and no resolution is recorded.

    Returns ``{"plan", "story", "cleared", "status"}``.
    """
    result = unpark_story(plan_name, story_key, fields, status=status)
    return {key: result[key] for key in ("plan", "story", "cleared", "status")}


def _record_resolution(result: dict, principal: str) -> None:
    """Record the operator's resolution beside the latest ruling, fail-open.

    The unblock has already been saved and announced by the time this runs, so
    a failed record write is a warning, never a failed unpark.
    """
    try:
        triage_outcomes.record_human_resolution(
            result["plan"],
            result["story"],
            human_action=result["human_action"],
            files_added=result["files_added"],
            new_status=result["new_status"],
            principal=principal,
        )
    except Exception:  # noqa: BLE001 - the unblock already happened; a record must not undo it
        logging.getLogger("pipeline").warning(
            "failed to record human resolution for story %s", result["story"]
        )


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
        default=None,
        help="comma-separated field names to delete (see UNPARKABLE_FIELDS)",
    )
    parser.add_argument(
        "--status",
        default="interrupted",
        help="status to set on the story (default: interrupted)",
    )
    parser.add_argument(
        "--add-files",
        default="",
        help=(
            "comma-separated production paths to append to the story's files "
            "(changing files requires the plan to be re-approved)"
        ),
    )
    parser.add_argument(
        "--human-action",
        default=None,
        help=(
            "what the operator did, recorded beside the overlord's ruling: one of "
            f"{', '.join(sorted(triage_outcomes.HUMAN_ACTIONS))} "
            "(default: derived from the change made)"
        ),
    )
    args = parser.parse_args(argv)
    if args.clear is None and not args.add_files:
        parser.error("one of --clear or --add-files is required")

    fields = [token.strip() for token in (args.clear or "").split(",") if token.strip()]
    add_files = [token.strip() for token in args.add_files.split(",") if token.strip()]
    try:
        result = unpark_story(
            args.plan,
            args.story,
            fields,
            status=args.status,
            add_files=add_files,
            human_action=args.human_action,
        )
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    _record_resolution(result, principal=actor_id())

    print(
        f"unparked {result['story']} in {result['plan']}: "
        f"cleared {', '.join(result['cleared']) or 'nothing'}"
        + (f"; status -> {result['status']}" if result["status"] else "")
    )
    if result["files_added"]:
        print(
            f"files changed for {result['story']}: the plan must be re-approved "
            "before the story is dispatched"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
