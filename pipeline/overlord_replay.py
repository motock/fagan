"""Replay recorded overlord rulings against the current or a candidate policy.

Every ``failure triage`` and ``high-risk merge adjudication`` record carries an
``input`` snapshot (``pipeline.ruling_snapshot``) of what the overlord was
asked, so the question can be asked again. This module rebuilds each prompt
from that snapshot, asks the overlord again ``--runs`` times, and scores the
new answers with the pairing and scoring rules of ``pipeline.triage_readiness``.
It shows what a change to the policy, the context sources or the overlord model
would have done to recorded rulings before that change goes live.

The replay is read-only. It calls the overlord and reads the decisions log and
the recorded context snapshots. It never writes a store, a decisions log, a
manifest or a notification, and never executes a ruling.

A failed or unparseable run is scored the way the gate treats it in production:
a triage run that fails parks the story, and a merge run that fails or does not
parse holds the merge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import context_sources as ctxsrc
from . import decision_log, overlord, review
from .merge import _parse_merge_ruling
from .module_ref import _ModuleRef
from .parsers import _parse_ruling
from .store_records import InvalidRecordKeyError, validate_plan_name
from .triage_outcomes import HIGH_RISK_MERGE_CLASS
from .triage_park_classes import ALLOWED_ACTIONS
from .triage_readiness import (
    _MERGE_PARK,
    _MERGE_PROCEED,
    PARK_ACTION,
    _latest_resolutions,
    _paired_resolution,
    _score,
    _score_merge,
)

#: The configured Store, read only for the plan list (``--all``), as triage_readiness does.
_store = _ModuleRef("pipeline.server", "_store")

#: Rulings replayed per plan when ``--limit`` is not given.
DEFAULT_LIMIT = 50

#: Overlord calls per ruling when ``--runs`` is not given, and the most allowed.
DEFAULT_RUNS = 1
MAX_RUNS = 5

_TRIAGE_QUESTION = "failure triage"
_ADJUDICATION_QUESTION = "high-risk merge adjudication"
_RULING_QUESTIONS = (_TRIAGE_QUESTION, _ADJUDICATION_QUESTION)
_UNKNOWN_CLASS = "unknown"

#: The line that begins the reply format in the merge adjudication prompt.
_MERGE_RESPONSE_MARKER = "Respond in this exact format:"

#: Outcome labels for a run that did not return a ruling.
_ERROR = "error"
_UNPARSEABLE = "unparseable"

#: Skip reasons, in the order the report lists them.
SKIP_REASONS = ("no_input", "truncated", "context_unavailable")

_NON_NAME_CHARS = re.compile(r"[^a-z0-9-]")


class _InputError(Exception):
    """A ``--policy`` or ``--context`` input cannot be used. The message names no path."""


class _ContextUnavailable(Exception):
    """A recorded context snapshot cannot be loaded or rendered for one ruling."""


@dataclass(frozen=True)
class _Inputs:
    """What every replayed prompt shares: the policy and, with --context, the context block."""

    policy: str
    context: str | None


def _new_tally() -> dict[str, Any]:
    return {
        "replayed": 0,
        "skipped": {reason: 0 for reason in SKIP_REASONS},
        "paired": 0,
        "agree": 0,
        "false_proceed": 0,
        "false_park": 0,
        "flips": 0,
        "unstable": 0,
        "unparseable": 0,
        "errors": 0,
    }


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source(name: str, *, authority: str, provider: str, text: str, digest: str) -> dict[str, Any]:
    return {
        "name": name,
        "authority": authority,
        "provider": provider,
        "text": text,
        "digest": digest,
        "bytes": len(text.encode("utf-8")),
    }


def _dir_source(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return _source(
        _NON_NAME_CHARS.sub("-", path.stem.lower()),
        authority="policy",
        provider="dir",
        text=text,
        digest=_digest(text),
    )


def _read_context_dir(directory: str) -> str:
    """Render the regular files of ``directory``, sorted by name, as one context block."""
    try:
        paths = sorted((p for p in Path(directory).iterdir() if p.is_file()), key=lambda p: p.name)
        sources = [_dir_source(path) for path in paths]
        return ctxsrc.render_context_block(sources, max_total_chars=ctxsrc.DEFAULT_MAX_TOTAL_CHARS)
    except (OSError, ValueError, ctxsrc.ContextSourceError):
        raise _InputError("the context directory could not be read or rendered") from None


def _read_policy(path: str | None) -> str:
    try:
        if path is None:
            return overlord._load_policy()
        return Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        raise _InputError("the policy could not be read") from None


def _recorded_block(digests: list[Any]) -> str:
    """Render the recorded snapshots for ``digests``; raise when one cannot be loaded."""
    sources = []
    for digest in digests:
        text = review._read_recorded_snapshot({"digest": digest}) if isinstance(digest, str) else None
        if text is None:
            raise _ContextUnavailable
        name = "recorded-" + digest.split(":", 1)[-1][:12]
        sources.append(
            _source(name, authority="reference", provider="recorded", text=text, digest=digest)
        )
    try:
        return ctxsrc.render_context_block(sources, max_total_chars=ctxsrc.DEFAULT_MAX_TOTAL_CHARS)
    except ctxsrc.ContextSourceError:
        raise _ContextUnavailable from None


def _park_class(record: dict[str, Any]) -> str:
    if record.get("question") == _ADJUDICATION_QUESTION:
        return HIGH_RISK_MERGE_CLASS
    snapshot = record.get("input")
    name = snapshot.get("park_class") if isinstance(snapshot, dict) else None
    return name or record.get("park_class") or _UNKNOWN_CLASS


def _merge_prompt(evidence: str, context: str) -> str:
    head, marker, closing = evidence.rpartition(_MERGE_RESPONSE_MARKER)
    if not marker:
        return evidence + context
    return head + context + marker + closing


def _rebuild_prompt(record: dict[str, Any], inputs: _Inputs) -> tuple[str | None, str | None]:
    """Return ``(prompt, None)``, or ``(None, reason)`` when the ruling cannot be replayed."""
    snapshot = record.get("input")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("evidence"), str):
        return None, "no_input"
    if snapshot.get("truncated"):
        return None, "truncated"
    evidence = snapshot["evidence"]
    try:
        if inputs.context is not None:
            context = inputs.context
        else:
            digests = snapshot.get("context_digests") or []
            if not isinstance(digests, list):
                return None, "context_unavailable"
            context = _recorded_block(digests)
    except _ContextUnavailable:
        return None, "context_unavailable"
    if record.get("question") == _ADJUDICATION_QUESTION:
        return _merge_prompt(evidence, context), None
    return inputs.policy + context + evidence, None


def _ask(prompt: str, *, merge: bool, plan: str, story_key: Any) -> str:
    """Return one run's outcome: the action or merge ruling, or an error/unparseable label."""
    try:
        raw = overlord._invoke_overlord(prompt)
    except Exception as exc:  # noqa: BLE001 - one failed call is one error run, not a crash
        logging.getLogger("pipeline").warning(
            "overlord replay call failed for %s in plan %s: %s",
            story_key,
            plan,
            type(exc).__name__,
        )
        return _ERROR
    if merge:
        parsed = _parse_merge_ruling(raw or "")
        return _UNPARSEABLE if parsed is None else parsed["ruling"]
    return _parse_ruling(raw or "")["action"]


def _decision(outcome: str, *, merge: bool, park_class: str) -> str:
    """The decision the gate would take on one run.

    A run that gave no ruling holds or parks. A triage action the park class does
    not allow is parked, as the live path does before it executes anything.
    """
    if merge:
        return _MERGE_PROCEED if outcome == _MERGE_PROCEED else _MERGE_PARK
    if outcome == _ERROR or outcome not in ALLOWED_ACTIONS.get(park_class, frozenset()):
        return PARK_ACTION
    return outcome


def _count_run_outcome(tally: dict[str, Any], outcome: str) -> None:
    if outcome == _ERROR:
        tally["errors"] += 1
    elif outcome == _UNPARSEABLE:
        tally["unparseable"] += 1


def _replay_ruling(
    record: dict[str, Any],
    prompt: str,
    tally: dict[str, Any],
    resolutions: dict,
    *,
    plan: str,
    runs: int,
) -> None:
    merge = record.get("question") == _ADJUDICATION_QUESTION
    outcomes = [_ask(prompt, merge=merge, plan=plan, story_key=record.get("story_key")) for _ in range(runs)]
    for outcome in outcomes:
        _count_run_outcome(tally, outcome)
    decision = _decision(outcomes[0], merge=merge, park_class=_park_class(record))
    recorded = record.get("ruling") if merge else record.get("action")
    if decision != recorded:
        tally["flips"] += 1
    if len(set(outcomes)) > 1:
        tally["unstable"] += 1
    resolution = _paired_resolution(resolutions, record)
    if resolution is None:
        return
    tally["paired"] += 1
    human_action = resolution.get("human_action")
    if merge:
        _score_merge(tally, decision, human_action)
    else:
        _score(tally, decision, resolution, count_input=False)


def _replay_plan(plan: str, inputs: _Inputs, *, runs: int, limit: int) -> dict[str, dict[str, Any]]:
    """Return ``{park class: tally}`` for ``plan``. Raises on an unreadable decisions log."""
    records = decision_log.read_decisions(plan)
    resolutions = _latest_resolutions(records)
    rulings = [record for record in records if record.get("question") in _RULING_QUESTIONS]
    rulings.sort(key=lambda record: str(record.get("decided_at") or ""), reverse=True)
    report: dict[str, dict[str, Any]] = {}
    replayed = 0
    for record in rulings:
        merge = record.get("question") == _ADJUDICATION_QUESTION
        if merge and not record.get("ruling"):
            continue
        tally = report.setdefault(_park_class(record), _new_tally())
        prompt, reason = _rebuild_prompt(record, inputs)
        if reason is not None:
            tally["skipped"][reason] += 1
            continue
        if replayed >= limit:
            continue
        replayed += 1
        tally["replayed"] += 1
        _replay_ruling(record, prompt, tally, resolutions, plan=plan, runs=runs)
    return report


def _int_between(low: int, high: int | None) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError("must be an integer") from None
        if value < low or (high is not None and value > high):
            bound = f"between {low} and {high}" if high is not None else f"at least {low}"
            raise argparse.ArgumentTypeError(f"must be {bound}")
        return value

    return parse


def _print_table(report: dict[str, dict[str, dict[str, Any]]]) -> None:
    header = (
        f"{'plan':<24}{'class':<28}{'replayed':>9}{'skipped':>9}{'paired':>8}"
        f"{'agree':>7}{'f-proc':>7}{'f-park':>7}{'flips':>7}{'unstable':>9}"
    )
    print(header)
    for plan, classes in report.items():
        for name, cls in classes.items():
            print(
                f"{plan:<24}{name:<28}{cls['replayed']:>9}{sum(cls['skipped'].values()):>9}"
                f"{cls['paired']:>8}{cls['agree']:>7}{cls['false_proceed']:>7}"
                f"{cls['false_park']:>7}{cls['flips']:>7}{cls['unstable']:>9}"
            )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Read-only. Returns 0 on a report, 1 if an input or a log cannot be read."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.overlord_replay",
        description=(
            "Ask the overlord again about recorded rulings and score the new answers "
            "against the human resolutions. Read-only; calls the overlord."
        ),
        epilog=(
            "--policy replaces the policy text of failure triage prompts only; a "
            "high-risk merge prompt carries no policy text, so --policy does not apply to it."
        ),
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--plan", nargs="+", metavar="PLAN", help="plan name(s) to replay")
    scope.add_argument("--all", action="store_true", help="every plan the store lists")
    parser.add_argument(
        "--policy", metavar="FILE", help="policy text for triage prompts (default: the shipped policy)"
    )
    parser.add_argument(
        "--context", metavar="DIR",
        help="context sources for every prompt, one per regular file (default: the recorded snapshots)",
    )
    parser.add_argument(
        "--runs", type=_int_between(1, MAX_RUNS), default=DEFAULT_RUNS,
        help=f"overlord calls per ruling, 1 to {MAX_RUNS} (default: %(default)s)",
    )
    parser.add_argument(
        "--limit", type=_int_between(1, None), default=DEFAULT_LIMIT,
        help="rulings replayed per plan, newest first (default: %(default)s)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    try:
        plans = sorted(_store.list_manifests()) if args.all else [validate_plan_name(p) for p in args.plan]
    except InvalidRecordKeyError:
        print("error: invalid plan name", file=sys.stderr)
        return 1
    try:
        inputs = _Inputs(
            policy=_read_policy(args.policy),
            context=None if args.context is None else _read_context_dir(args.context),
        )
    except _InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        report = {
            plan: _replay_plan(plan, inputs, runs=args.runs, limit=args.limit) for plan in plans
        }
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
