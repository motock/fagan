"""Report, per park class, whether its triage rulings are ready to leave shadow.

ADR 0014 (decision 8) keeps every park class in shadow mode until its recorded
rulings have been checked against what a human did. A shadow ruling is
``failure triage`` record; the human's answer is a ``human resolution`` record
that points back at it through ``ruling_ref``. This module pairs the two,
tallies the agreement per park class, and names the thresholds a class must
clear before a human may promote it.

The report is read-only. It never writes the decisions log and never changes
a ruling's mode; promotion stays a human decision.

Definitions, from a paired ruling and the human's ``human_action``:

- A ruling that proceeds is any action but ``park_for_human``. It is a false
  proceed when the human parked the story instead.
- A ``park_for_human`` ruling is a false park when the human resolved the
  story without parking it.
- ``false_park_rate`` is false parks over paired decisions. Unpaired rulings
  count toward ``shadow_count`` only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from typing import Any

from .module_ref import _ModuleRef
from .store_records import InvalidRecordKeyError, validate_plan_name
from .triage_outcomes import HIGH_RISK_MERGE_CLASS
from .triage_park_classes import ALLOWED_ACTIONS

#: The configured Store. Plans and decision records are read only through it
#: (ADR 0012), so the report sees the same store the server writes to.
_store = _ModuleRef("pipeline.server", "_store")

__all__ = ["MAX_FALSE_PARK_PERCENT", "MIN_PAIRED_DECISIONS", "PARK_ACTION", "main", "readiness"]

#: The ruling action that parks a story for a human.
PARK_ACTION = "park_for_human"

#: The human action that parks a story. Any other human action unparks it.
_HUMAN_PARK = "park"

#: ADR 0014 decision 8: a class needs at least this many paired decisions.
MIN_PAIRED_DECISIONS = 20

#: ADR 0014 decision 8: the false-park rate must not exceed this percentage.
#: Compared as integers (``false_park * 100 <= MAX * paired``) so a rate of
#: exactly 10% is eligible and no float rounding decides the boundary.
MAX_FALSE_PARK_PERCENT = 10

_RULING_QUESTION = "failure triage"
_ADJUDICATION_QUESTION = "high-risk merge adjudication"
_RESOLUTION_QUESTION = "human resolution"
_UNKNOWN_CLASS = "unknown"

#: The ruling that lets a high-risk merge go ahead.
_MERGE_PROCEED = "proceed"

#: The ruling that holds a high-risk merge for a human.
_MERGE_PARK = "park"

#: The human action that holds a merge the machine would have let go.
_HUMAN_MERGE_HELD = "merge_held"


def _tally() -> dict[str, int]:
    return {
        "shadow_count": 0,
        "paired_count": 0,
        "agree": 0,
        "false_proceed": 0,
        "false_park": 0,
    }


def _latest_resolutions(records: list[dict[str, Any]]) -> dict[tuple[Any, Any], dict[str, Any]]:
    """Map ``(story_key, ruling decided_at)`` to the latest resolution for it.

    Records are in append order, so a later resolution for the same ruling
    replaces an earlier one. A resolution with no ``ruling_ref`` pairs with
    nothing.
    """
    latest: dict[tuple[Any, Any], dict[str, Any]] = {}
    for record in records:
        if record.get("question") != _RESOLUTION_QUESTION:
            continue
        ref = record.get("ruling_ref")
        if ref is not None:
            latest[(record.get("story_key"), ref)] = record
    return latest


def _score(tally: dict[str, int], action: Any, human_action: Any) -> None:
    parked_by_machine = action == PARK_ACTION
    parked_by_human = human_action == _HUMAN_PARK
    if parked_by_machine and parked_by_human:
        tally["agree"] += 1
    elif parked_by_machine:
        tally["false_park"] += 1
    elif parked_by_human:
        tally["false_proceed"] += 1
    else:
        tally["agree"] += 1


def _score_merge(tally: dict[str, int], ruling: Any, human_action: Any) -> None:
    """Score one paired high-risk merge adjudication.

    The human's answer to a merge adjudication is ``merge_held``, not ``park``.
    A ``proceed`` ruling that the human held agrees with the machine; a
    ``proceed`` ruling the human answered some other way is a false proceed. A
    ``park`` ruling that the human held is a false park; every other pairing
    agrees with what the machine ruled.
    """
    held = human_action == _HUMAN_MERGE_HELD
    if ruling == _MERGE_PROCEED:
        if held:
            tally["agree"] += 1
        else:
            tally["false_proceed"] += 1
    elif ruling == _MERGE_PARK and held:
        tally["false_park"] += 1
    else:
        tally["agree"] += 1


def _verdict(tally: dict[str, int]) -> dict[str, Any]:
    paired = tally["paired_count"]
    false_park = tally["false_park"]
    false_proceed = tally["false_proceed"]
    blockers: list[str] = []
    if paired < MIN_PAIRED_DECISIONS:
        blockers.append(f"{paired} paired decisions, {MIN_PAIRED_DECISIONS} required")
    if false_proceed:
        blockers.append(f"{false_proceed} false proceed(s), none allowed")
    if paired and false_park * 100 > MAX_FALSE_PARK_PERCENT * paired:
        blockers.append(f"false-park rate above {MAX_FALSE_PARK_PERCENT}%")
    return {
        **tally,
        "false_park_rate": false_park / paired if paired else None,
        "eligible": not blockers,
        "blockers": blockers,
    }


def readiness(decisions: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return the promotion readiness of each park class in ``decisions``.

    Every class in :data:`pipeline.triage_park_classes.ALLOWED_ACTIONS` is
    reported, even with no decisions, so an empty input yields a denial for
    each class, and so is ``high_risk_merge``. A class seen only in the data is
    reported too. Each verdict lists its ``blockers``; an ineligible class
    always names at least one.
    """
    records = list(decisions)
    resolutions = _latest_resolutions(records)
    tallies: dict[str, dict[str, int]] = {name: _tally() for name in ALLOWED_ACTIONS}
    tallies.setdefault(HIGH_RISK_MERGE_CLASS, _tally())
    for ruling in records:
        question = ruling.get("question")
        if question == _ADJUDICATION_QUESTION:
            _tally_adjudication(tallies, resolutions, ruling)
            continue
        if question != _RULING_QUESTION:
            continue
        name = ruling.get("park_class") or _UNKNOWN_CLASS
        tally = tallies.setdefault(name, _tally())
        tally["shadow_count"] += 1
        resolution = _paired_resolution(resolutions, ruling)
        if resolution is None:
            continue
        tally["paired_count"] += 1
        _score(tally, ruling.get("action"), resolution.get("human_action"))
    return {name: _verdict(tally) for name, tally in tallies.items()}


def _paired_resolution(
    resolutions: dict[tuple[Any, Any], dict[str, Any]], ruling: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the resolution that answers ``ruling``, or ``None``.

    A ruling with no ``decided_at`` cannot be pointed at, and a resolution with
    no ``ruling_ref`` pairs with nothing, so either way nothing pairs.
    """
    decided_at = ruling.get("decided_at")
    if decided_at is None:
        return None
    return resolutions.get((ruling.get("story_key"), decided_at))


def _tally_adjudication(
    tallies: dict[str, dict[str, int]],
    resolutions: dict[tuple[Any, Any], dict[str, Any]],
    ruling: dict[str, Any],
) -> None:
    """Count one high-risk merge adjudication in its own class.

    An adjudication with an empty ``ruling`` is the fail-closed record of an
    unparseable reply: it ruled nothing, so it is not a shadow decision here.
    """
    merge_ruling = ruling.get("ruling")
    if not merge_ruling:
        return
    tally = tallies.setdefault(HIGH_RISK_MERGE_CLASS, _tally())
    tally["shadow_count"] += 1
    resolution = _paired_resolution(resolutions, ruling)
    if resolution is None:
        return
    tally["paired_count"] += 1
    _score_merge(tally, merge_ruling, resolution.get("human_action"))


def _read_decisions(plan: str) -> list[dict[str, Any]]:
    records = _store.get_decisions(plan)
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise TypeError("decisions are not a list of records")
    return records


def _print_table(report: dict[str, dict[str, dict[str, Any]]]) -> None:
    header = f"{'plan':<24}{'class':<28}{'shadow':>7}{'paired':>8}{'agree':>7}{'f-proc':>7}{'f-park':>7}{'rate':>7}  eligible"
    print(header)
    for plan, classes in report.items():
        for name, cls in classes.items():
            rate = "-" if cls["false_park_rate"] is None else f"{cls['false_park_rate']:.0%}"
            print(
                f"{plan:<24}{name:<28}{cls['shadow_count']:>7}{cls['paired_count']:>8}"
                f"{cls['agree']:>7}{cls['false_proceed']:>7}{cls['false_park']:>7}{rate:>7}  "
                f"{'yes' if cls['eligible'] else 'no'}"
            )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Read-only. Returns 0 on a report, 1 if a log is unreadable."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.triage_readiness",
        description="Report per park class whether its shadow rulings meet ADR 0014 decision 8.",
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--plan", nargs="+", metavar="PLAN", help="plan name(s) to report")
    scope.add_argument("--all", action="store_true", help="every plan the store lists")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    try:
        plans = sorted(_store.list_manifests()) if args.all else [validate_plan_name(p) for p in args.plan]
    except InvalidRecordKeyError:
        print("error: invalid plan name", file=sys.stderr)
        return 1
    try:
        report = {plan: readiness(_read_decisions(plan)) for plan in plans}
    except (OSError, TypeError, ValueError):
        print("error: a decisions log could not be read", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
