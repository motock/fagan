"""Backfill the human answers to past overlord rulings, inferred from history.

A ``failure triage`` ruling or a ``high-risk merge adjudication`` says what the
machine decided. When nobody recorded what a person did next, the decisions
stream has the ruling and nothing beside it, so readiness and precedent cannot
see that experience. This module reads each unanswered ruling's own story
history and infers the human action, but only when the history is certain:
a story with more than one kind of human action after its ruling, or with a
human action whose meaning is not certain, is undetermined and not written.

The history is read through the same stores the rest of the pipeline uses:

- the plan's audit chain, for merges and dispatches a person made
  (``principal_type`` is ``human``);
- the plan's notifications, for a brief a person patched, a manual unpark, and
  a merge a person made outside the pipeline;
- the decisions stream, for operator judgments made by a human tier;
- the manifest, for the story's current status.

An inferred answer is written through :func:`triage_outcomes.record_human_resolution`
with ``source: "backfill"`` and ``inferred: true``. The backfill never changes
a ruling, a story or a plan. The dry run is the default; ``--apply`` writes.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from . import decision_log, triage_outcomes
from .actor import actor_id, current_namespace
from .decision_record_tool import HUMAN_ACTION_BY_CLASS
from .store_records import InvalidRecordKeyError, validate_plan_name
from .triage_outcomes import HIGH_RISK_MERGE_CLASS

__all__ = [
    "INFERRED_SOURCE",
    "HistoryEvent",
    "backfill",
    "collect_events",
    "infer_human_action",
    "main",
]

#: The ``source`` every inferred resolution carries.
INFERRED_SOURCE = "backfill"

#: Story-history kinds a person produced, mapped to the human action each one
#: certainly means. A manual unpark is absent on purpose: an unpark that added
#: files widened the scope, and the history does not say whether it did.
_EVENT_ACTIONS = {
    "merged": "merge_held",
    "brief_patched": "rebrief",
    "dispatched": "retry",
}

#: Audit actions a person's merge or dispatch writes, mapped to the event kind.
_AUDIT_KINDS = {"story.merge": "merged", "story.dispatch": "dispatched"}

#: Notification events a person's action writes, mapped to the event kind.
_NOTICE_KINDS = {
    "story_merged_outside_pipeline": "merged",
    "brief_patched": "brief_patched",
    "story_unparked_manually": "unparked",
}

_UNKNOWN_CLASS = "unknown"
_PARKED = "parked"


@dataclass(frozen=True)
class HistoryEvent:
    """One thing a person did to a story.

    ``ts`` is ``None`` when the record's time cannot be read; the inference
    then refuses to decide, because the event's place in the order is unknown.
    ``decision_class`` is set for a ``judgment`` and names the judgment's class.
    """

    ts: datetime | None
    kind: str
    decision_class: str = ""


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _action_for(event: HistoryEvent) -> str | None:
    if event.kind == "judgment":
        return HUMAN_ACTION_BY_CLASS.get(event.decision_class)
    return _EVENT_ACTIONS.get(event.kind)


def infer_human_action(
    ruling: dict[str, Any], events: Iterable[HistoryEvent], status: str | None
) -> str | None:
    """Return the human action the story's history shows after ``ruling``, or ``None``.

    ``events`` are the human-attributed events on the story; the machine's own
    actions are not passed in. Only events after the ruling count. With none,
    a story still ``parked`` was left parked by a person, so the answer is
    ``park``; a story moved off park with no person on record is undetermined,
    because the machine may have retried it. Any event that cannot be placed in
    time, has no certain meaning, or disagrees with another event makes the
    answer undetermined too.

    ``None`` means undetermined: the caller counts it and writes nothing.
    """
    ruled_at = _parse_time(ruling.get("decided_at"))
    if ruled_at is None:
        return None
    later: list[HistoryEvent] = []
    for event in events:
        if event.ts is None:
            return None
        if event.ts > ruled_at:
            later.append(event)
    if not later:
        return "park" if status == _PARKED else None
    actions: set[str] = set()
    for event in later:
        action = _action_for(event)
        if action is None:
            return None
        actions.add(action)
    if len(actions) != 1:
        return None
    return actions.pop()


def _history_by_story(
    store: Any, plan_name: str, decisions: Iterable[dict[str, Any]]
) -> dict[str, list[HistoryEvent]]:
    """Group one plan's human-attributed events by story key.

    Reads the plan's audit chain, its notifications and the decisions stream
    once. Machine records (an agent's merge, an overlord's notice) are left
    out, so only a person's action can decide a ruling.
    """
    namespace = current_namespace()
    by_story: dict[str, list[HistoryEvent]] = {}

    def add(story_key: Any, event: HistoryEvent) -> None:
        if isinstance(story_key, str):
            by_story.setdefault(story_key, []).append(event)

    for record in store.audit_log().read_chain(namespace, plan_name):
        if not isinstance(record, dict) or record.get("principal_type") != "human":
            continue
        target = record.get("target")
        action: Any = record.get("action")
        kind = _AUDIT_KINDS.get(action)
        if isinstance(target, dict) and kind is not None:
            add(target.get("story"), HistoryEvent(_parse_time(record.get("ts")), kind))
    notices, _malformed = store.records().get_notification_history(namespace, plan_name)
    for notice in notices:
        event: Any = notice.get("event") if isinstance(notice, dict) else None
        kind = _NOTICE_KINDS.get(event)
        if kind is not None:
            add(notice.get("story_key"), HistoryEvent(_parse_time(notice.get("ts")), kind))
    for judgment in decisions:
        if not isinstance(judgment, dict) or "class" not in judgment:
            continue
        subject = judgment.get("subject")
        if judgment.get("tier") != "human" or not isinstance(subject, dict):
            continue
        event = HistoryEvent(_parse_time(judgment.get("ts")), "judgment", str(judgment["class"]))
        add(subject.get("story"), event)
    return by_story


def collect_events(
    store: Any,
    plan_name: str,
    story_key: str,
    decisions: Iterable[dict[str, Any]],
) -> list[HistoryEvent]:
    """Return the human-attributed events on one story, in no particular order."""
    return _history_by_story(store, plan_name, decisions).get(story_key, [])


def _class_of(ruling: dict[str, Any]) -> str:
    if ruling.get("question") == "high-risk merge adjudication":
        return HIGH_RISK_MERGE_CLASS
    return str(ruling.get("park_class") or _UNKNOWN_CLASS)


def _ruled_on_or_after(ruling: dict[str, Any], since: date) -> bool:
    ruled_at = _parse_time(ruling.get("decided_at"))
    return ruled_at is not None and ruled_at.astimezone(UTC).date() >= since


def _configured_store() -> Any:
    from . import server

    return server._store


def _statuses(store: Any, plan_name: str) -> dict[str, str]:
    """Map each story of ``plan_name`` to its current status, where it has one."""
    manifest = store.get_manifest(plan_name)
    stories = manifest.get("stories") if isinstance(manifest, dict) else None
    if not isinstance(stories, dict):
        return {}
    return {
        key: story["status"]
        for key, story in stories.items()
        if isinstance(story, dict) and isinstance(story.get("status"), str)
    }


def backfill(
    plan_names: Iterable[str],
    *,
    since: date,
    apply: bool,
    store: Any = None,
) -> dict[str, dict[str, Any]]:
    """Infer the human answer to each unanswered ruling since ``since``.

    Returns, per park class, the number of ``rulings`` considered, the number
    of inferred answers by ``inferred`` action, and the ``undetermined`` count.
    A class appears only when it has a ruling. With ``apply`` the inferred
    answers are written; without it nothing is written. A ruling that already
    has any resolution is not considered, so a second run writes nothing.
    """
    source = store if store is not None else _configured_store()
    report: dict[str, dict[str, Any]] = {}
    for plan in plan_names:
        decisions = decision_log.read_decisions(plan)
        history = _history_by_story(source, plan, decisions)
        statuses = _statuses(source, plan)
        for ruling in decisions:
            if not triage_outcomes.is_ruling(ruling) or not _ruled_on_or_after(ruling, since):
                continue
            story_key = ruling.get("story_key")
            if not isinstance(story_key, str) or triage_outcomes.ruling_resolved(plan, story_key, ruling):
                continue
            row = report.setdefault(
                _class_of(ruling), {"rulings": 0, "inferred": {}, "undetermined": 0}
            )
            row["rulings"] += 1
            status = statuses.get(story_key)
            action = infer_human_action(ruling, history.get(story_key, []), status)
            if action is None:
                row["undetermined"] += 1
                continue
            row["inferred"][action] = row["inferred"].get(action, 0) + 1
            if apply:
                triage_outcomes.record_human_resolution(
                    plan,
                    story_key,
                    human_action=action,
                    new_status=status or "",
                    principal=actor_id(),
                    note="inferred from story history",
                    ruling=ruling,
                    source=INFERRED_SOURCE,
                    inferred=True,
                )
    return report


def _print_table(report: dict[str, dict[str, Any]], since: date) -> None:
    if not report:
        print(f"no unanswered rulings since {since.isoformat()}")
        return
    print(f"{'class':<28}{'rulings':>9}  {'inferred by action':<40}{'undetermined':>13}")
    for name, row in sorted(report.items()):
        by_action = ", ".join(f"{action} {count}" for action, count in sorted(row["inferred"].items()))
        print(f"{name:<28}{row['rulings']:>9}  {by_action or '-':<40}{row['undetermined']:>13}")


def _parse_since(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from exc


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Dry run unless ``--apply``. Returns 0, or 1 on a refused run."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.triage_backfill",
        description=(
            "Infer the human answer to past overlord rulings from each story's history. "
            "Dry run by default; --apply writes inferred human resolutions only."
        ),
    )
    parser.add_argument("--since", required=True, type=_parse_since, metavar="YYYY-MM-DD")
    parser.add_argument("--plan", nargs="+", metavar="PLAN", help="plan name(s); default every plan in this namespace")
    parser.add_argument("--apply", action="store_true", help="write the inferred resolutions (default: dry run)")
    args = parser.parse_args(argv)

    try:
        if args.plan:
            plans = [validate_plan_name(name) for name in args.plan]
        else:
            plans = _configured_store().records().list_plans(current_namespace())
    except InvalidRecordKeyError:
        print("error: invalid plan name", file=sys.stderr)
        return 1
    try:
        report = backfill(plans, since=args.since, apply=args.apply)
    except OSError:
        print("error: a decisions log could not be read or written", file=sys.stderr)
        return 1

    _print_table(report, args.since)
    inferred = sum(sum(row["inferred"].values()) for row in report.values())
    if args.apply:
        print(f"applied: {inferred} inferred resolution(s) written")
    else:
        print(f"dry run: {inferred} would be written; nothing was written (pass --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
