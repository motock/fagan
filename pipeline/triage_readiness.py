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
  story without parking it and without supplying anything. A human who changed
  the brief, widened the scope, approved a test edit or added files supplied
  input, so that pairing agrees; it is also tallied as ``park_needed_input``.
- ``false_park_rate`` is false parks over paired decisions. Unpaired rulings
  count toward ``shadow_count`` only.

A person's park option choice (``park_option_chosen``) is not a ruling, so it
never enters the pairing or the verdict. It is reported beside the verdict as
``park_option_choices`` (every choice in the class) and
``park_option_agreements`` (choices of the overlord's recommended option).

Scope decides which decisions are pooled before a class is judged. A ruling is
always paired inside its own plan, so pooling never pairs a ruling with another
plan's resolution. The scopes are:

- ``plan``: each plan is judged on its own decisions.
- ``team``: the plans of one namespace are pooled; the report is keyed by namespace.
- ``deployment``: every plan is pooled; the report is keyed by ``deployment``.

Pooling sums the per-plan counts, so the thresholds and the verdict are the
same ones a single plan is judged against. A promotion is per decision class,
and a class is tiered by the authority matrix, not by a plan.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from typing import Any

from .actor import current_namespace
from .authority_matrix import FileLayerSource
from .module_ref import _ModuleRef
from .overlord_class_mode import matrix_tier
from .store_namespaces import namespaces_with_plans
from .store_records import InvalidRecordKeyError, validate_plan_name
from .triage_outcomes import HIGH_RISK_MERGE_CLASS
from .triage_park_classes import ALLOWED_ACTIONS

#: The configured Store. Plans and decision records are read only through it
#: (ADR 0012), so the report sees the same store the server writes to.
_store = _ModuleRef("pipeline.server", "_store")

__all__ = [
    "MAX_FALSE_PARK_PERCENT",
    "MIN_PAIRED_DECISIONS",
    "PARK_ACTION",
    "SCOPES",
    "main",
    "readiness",
    "scoped_readiness",
]

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

#: Authority-matrix rule ids that an organization may tighten (tighten-only).
_MIN_PAIRED_RULE = "readiness.min_paired"
_RATE_RULE = "readiness.max_false_park_percent"

#: The scopes a readiness report can be judged over, and the key a deployment report uses.
SCOPE_PLAN = "plan"
SCOPE_TEAM = "team"
SCOPE_DEPLOYMENT = "deployment"
SCOPES = (SCOPE_DEPLOYMENT, SCOPE_TEAM, SCOPE_PLAN)

#: The tier label for a park class the authority matrix does not name.
_NOT_GOVERNED = "not governed"

_RULING_QUESTION = "failure triage"
_ADJUDICATION_QUESTION = "high-risk merge adjudication"
_RESOLUTION_QUESTION = "human resolution"
_CHOICE_KIND = "park_option_chosen"
_UNKNOWN_CLASS = "unknown"

#: The ruling that lets a high-risk merge go ahead.
_MERGE_PROCEED = "proceed"

#: The ruling that holds a high-risk merge for a human.
_MERGE_PARK = "park"

#: The human action that holds a merge the machine would have let go.
_HUMAN_MERGE_HELD = "merge_held"

#: Human actions that supply input the machine did not have: a changed scope,
#: a rewritten brief or an approved test edit.
_HUMAN_INPUT_ACTIONS = frozenset({"amend_scope", "rebrief", "approve_test_edit"})


def _tally() -> dict[str, int]:
    return {
        "shadow_count": 0,
        "paired_count": 0,
        "agree": 0,
        "false_proceed": 0,
        "false_park": 0,
        "park_needed_input": 0,
        "incomplete_evidence": 0,
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


def _supplied_input(resolution: dict[str, Any]) -> bool:
    """True when the human's answer carried input the machine did not have.

    ``brief_changed`` counts only as the boolean ``True``: a stored string or
    number is not evidence that the brief changed.
    """
    return (
        resolution.get("human_action") in _HUMAN_INPUT_ACTIONS
        or bool(resolution.get("files_added"))
        or resolution.get("brief_changed") is True
    )


def _score(
    tally: dict[str, int],
    action: Any,
    human_action: Any,
    resolution: dict[str, Any] | None = None,
) -> None:
    """Score one paired ruling.

    ``resolution`` is the human resolution record. Without it a park the human
    answered with input is still a false park, the rule ``overlord_replay``
    scores with.
    """
    parked_by_machine = action == PARK_ACTION
    parked_by_human = human_action == _HUMAN_PARK
    if parked_by_machine and parked_by_human:
        tally["agree"] += 1
    elif parked_by_machine and resolution is not None and _supplied_input(resolution):
        tally["agree"] += 1
        tally["park_needed_input"] += 1
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


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


def thresholds() -> dict[str, Any]:
    """Return the promotion thresholds in force: ``min_paired`` and ``max_false_park_percent``.

    The shipped constants are the loosest values allowed. An organization or
    team layer of the authority matrix may tighten them; a value that is
    missing or not a number falls back to the constant. The result is clamped
    in code as well, so an edited matrix bound cannot loosen the thresholds.
    Never raises: an unusable matrix yields the constants.
    """
    resolved = FileLayerSource().resolve()
    values = resolved.values if resolved is not None else {}
    min_paired = _number(values.get(_MIN_PAIRED_RULE), MIN_PAIRED_DECISIONS)
    max_percent = _number(values.get(_RATE_RULE), MAX_FALSE_PARK_PERCENT)
    return {
        "min_paired": max(min_paired, MIN_PAIRED_DECISIONS),
        "max_false_park_percent": min(max_percent, MAX_FALSE_PARK_PERCENT),
    }


def _tier_label(decision_class: str) -> str:
    tier = matrix_tier(decision_class)
    return _NOT_GOVERNED if tier is None else tier


def _count_choices(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Count park option choices per park class.

    A choice with no usable ``park_class`` is skipped: it cannot be placed in a
    class, and nothing else in the record is needed to count it.
    """
    counts: dict[str, dict[str, int]] = {}
    for record in records:
        if record.get("kind") != _CHOICE_KIND:
            continue
        name = record.get("park_class")
        if not isinstance(name, str) or not name:
            continue
        count = counts.setdefault(name, _choice_tally())
        count["park_option_choices"] += 1
        if record.get("chose_recommended") is True:
            count["park_option_agreements"] += 1
    return counts


def _choice_tally() -> dict[str, int]:
    return {"park_option_choices": 0, "park_option_agreements": 0}


def _verdict(tally: dict[str, int], limits: dict[str, Any], tier: str) -> dict[str, Any]:
    paired = tally["paired_count"]
    false_park = tally["false_park"]
    false_proceed = tally["false_proceed"]
    min_paired = limits["min_paired"]
    max_percent = limits["max_false_park_percent"]
    blockers: list[str] = []
    if paired < min_paired:
        blockers.append(f"{paired} paired decisions, {min_paired} required")
    if false_proceed:
        blockers.append(f"{false_proceed} false proceed(s), none allowed")
    if paired and false_park * 100 > max_percent * paired:
        blockers.append(f"false-park rate above {max_percent}%")
    return {
        **tally,
        "false_park_rate": false_park / paired if paired else None,
        "eligible": not blockers,
        "blockers": blockers,
        "thresholds": limits,
        "tier": tier,
    }


_Counts = tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]


def _classify(decisions: Iterable[dict[str, Any]]) -> _Counts:
    """Count one set of decisions: the per-class tallies and the park option choices.

    Pairing happens here, inside the set given, so a set is always one plan or
    one pooled scope of plans that each paired within themselves.
    """
    records = list(decisions)
    resolutions = _latest_resolutions(records)
    choices = _count_choices(records)
    tallies: dict[str, dict[str, int]] = {name: _tally() for name in ALLOWED_ACTIONS}
    tallies.setdefault(HIGH_RISK_MERGE_CLASS, _tally())
    for name in choices:
        tallies.setdefault(name, _tally())
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
        _score(tally, ruling.get("action"), resolution.get("human_action"), resolution)
    return tallies, choices


def _pool(parts: Iterable[_Counts]) -> _Counts:
    """Sum counts from several sets of decisions, class by class.

    The result always names every class in ``ALLOWED_ACTIONS``, even with no
    parts, so an empty scope is still a denial for each class.
    """
    tallies, choices = _classify([])
    for part_tallies, part_choices in parts:
        for name, tally in part_tallies.items():
            pooled = tallies.setdefault(name, _tally())
            for key, value in tally.items():
                pooled[key] += value
        for name, count in part_choices.items():
            pooled_choices = choices.setdefault(name, _choice_tally())
            for key, value in count.items():
                pooled_choices[key] += value
    return tallies, choices


def _verdicts(counts: _Counts, limits: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tallies, choices = counts
    report = {}
    for name, tally in tallies.items():
        verdict = _verdict(tally, limits, _tier_label(name))
        verdict.update(choices.get(name, _choice_tally()))
        report[name] = verdict
    return report


def readiness(
    decisions: Iterable[dict[str, Any]], limits: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Return the promotion readiness of each park class in ``decisions``.

    Every class in :data:`pipeline.triage_park_classes.ALLOWED_ACTIONS` is
    reported, even with no decisions, so an empty input yields a denial for
    each class, and so is ``high_risk_merge``. A class seen only in the data is
    reported too. Each verdict lists its ``blockers``, the ``thresholds`` it was
    judged against and the class's matrix ``tier``; an ineligible class always
    names at least one blocker. ``limits`` defaults to :func:`thresholds`.
    """
    if limits is None:
        limits = thresholds()
    return _verdicts(_classify(decisions), limits)


def scoped_readiness(
    plans: Iterable[tuple[str, str, Iterable[dict[str, Any]]]],
    scope: str,
    limits: dict[str, Any] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return the readiness report of ``plans`` judged over ``scope``.

    ``plans`` holds one ``(namespace, plan, decisions)`` triple per plan. The
    report has the shape :func:`readiness` returns, keyed by the scope's name:
    the plan name for ``plan``, the namespace for ``team`` and ``deployment`` for
    ``deployment``. Pooling sums each plan's own counts, so no ruling pairs with
    a resolution from another plan. A plan name that appears in more than one
    namespace has no unique key at ``plan`` scope, so that scope refuses it.
    An unknown scope is refused.
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope!r}")
    if limits is None:
        limits = thresholds()
    counted = [(namespace, plan, _classify(decisions)) for namespace, plan, decisions in plans]
    if scope == SCOPE_PLAN:
        report: dict[str, dict[str, dict[str, Any]]] = {}
        for _, plan, counts in counted:
            if plan in report:
                raise ValueError(f"plan {plan!r} is held in more than one namespace")
            report[plan] = _verdicts(counts, limits)
        return report
    if scope == SCOPE_DEPLOYMENT:
        return {SCOPE_DEPLOYMENT: _verdicts(_pool(counts for _, _, counts in counted), limits)}
    by_namespace: dict[str, list[_Counts]] = {}
    for namespace, _, counts in counted:
        by_namespace.setdefault(namespace, []).append(counts)
    return {namespace: _verdicts(_pool(parts), limits) for namespace, parts in by_namespace.items()}


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
    One with ``evidence_complete`` false was ruled before CI concluded: it is
    reported as ``incomplete_evidence`` and left out of every other count. A
    record without the field is history and is counted as before.
    """
    merge_ruling = ruling.get("ruling")
    if not merge_ruling:
        return
    tally = tallies.setdefault(HIGH_RISK_MERGE_CLASS, _tally())
    if ruling.get("evidence_complete") is False:
        tally["incomplete_evidence"] += 1
        return
    tally["shadow_count"] += 1
    resolution = _paired_resolution(resolutions, ruling)
    if resolution is None:
        return
    tally["paired_count"] += 1
    _score_merge(tally, merge_ruling, resolution.get("human_action"))


def _checked(records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise TypeError("decisions are not a list of records")
    return records


def _read_decisions(plan: str) -> list[dict[str, Any]]:
    return _checked(_store.get_decisions(plan))


def _read_namespaced_plans() -> list[tuple[str, str, list[dict[str, Any]]]]:
    """Read every plan in every namespace the store holds, with its decisions.

    The namespaced record view is the only read that tells which namespace a
    plan is bound to, so team and deployment scope both go through it.
    """
    records = _store.records()
    plans = []
    for namespace in namespaces_with_plans(records):
        for plan in records.list_plans(namespace):
            plans.append((namespace, plan, _checked(records.get_decisions(namespace, plan))))
    return plans


def _print_table(report: dict[str, dict[str, dict[str, Any]]]) -> None:
    limits = thresholds()
    print(
        f"thresholds: min paired {limits['min_paired']}, "
        f"max false-park {limits['max_false_park_percent']}%"
    )
    header = (
        f"{'plan':<24}{'class':<28}{'shadow':>7}{'paired':>8}{'agree':>7}{'f-proc':>7}{'f-park':>7}{'rate':>7}"
        f"{'choices':>9}{'chose-rec':>10}  eligible  tier"
    )
    print(header)
    for scope_key, classes in report.items():
        for name, cls in classes.items():
            rate = "-" if cls["false_park_rate"] is None else f"{cls['false_park_rate']:.0%}"
            eligible = "yes" if cls["eligible"] else "no"
            print(
                f"{scope_key:<24}{name:<28}{cls['shadow_count']:>7}{cls['paired_count']:>8}"
                f"{cls['agree']:>7}{cls['false_proceed']:>7}{cls['false_park']:>7}{rate:>7}"
                f"{cls['park_option_choices']:>9}{cls['park_option_agreements']:>10}  "
                f"{eligible:<8}  {cls['tier']}"
            )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Read-only. Returns 0 on a report, 1 if a log is unreadable."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.triage_readiness",
        description="Report per park class whether its shadow rulings meet ADR 0014 decision 8.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", nargs="+", metavar="PLAN", help="plan name(s) to report; implies --scope plan")
    source.add_argument("--all", action="store_true", help="every plan in every namespace the store lists")
    parser.add_argument(
        "--scope",
        choices=SCOPES,
        help=(
            "pool decisions over this scope before judging each class: deployment (every plan, "
            "the default for --all), team (plans grouped by namespace) or plan (each plan alone)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)
    if args.plan is not None and args.scope not in (None, SCOPE_PLAN):
        parser.error("--plan reports each plan on its own; drop --scope or use --all for a pooled scope")
    scope = args.scope or (SCOPE_DEPLOYMENT if args.all else SCOPE_PLAN)

    try:
        names = None if args.all else [validate_plan_name(p) for p in args.plan]
    except InvalidRecordKeyError:
        print("error: invalid plan name", file=sys.stderr)
        return 1
    try:
        plans = _read_namespaced_plans() if args.all else [(current_namespace(), n, _read_decisions(n)) for n in names]
    except (OSError, TypeError, ValueError):
        print("error: a decisions log could not be read", file=sys.stderr)
        return 1
    try:
        report = scoped_readiness(plans, scope)
    except ValueError:
        print("error: a plan name is held in more than one namespace; use --scope team or deployment", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
