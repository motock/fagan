"""Known-answer hard cases the overlord must pass before a policy change goes live.

``coa-08`` (``pipeline.overlord_replay``) replays recorded history. A scenario
here is a case whose right answer is known in advance, including cases history
has not produced yet. Each scenario file holds one question as the overlord
receives it, the answers that are allowed, and the answers that must never be
given.

The prompt is rebuilt and the overlord asked through the same functions
``pipeline.overlord_replay`` uses, so a scenario is graded against the prompt the
gate would send. The check is read-only: it calls the overlord and reads the
scenario files and the policy or context named on the command line. It never
writes a store, a decisions log, a manifest or a notification.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .context_sources import scan_for_secrets
from .overlord_replay import (
    _ADJUDICATION_QUESTION,
    _MERGE_RESPONSE_MARKER,
    _TRIAGE_QUESTION,
    DEFAULT_RUNS,
    MAX_RUNS,
    _ask,
    _InputError,
    _Inputs,
    _int_between,
    _read_context_dir,
    _read_policy,
    _rebuild_prompt,
)
from .triage_outcomes import HIGH_RISK_MERGE_CLASS
from .triage_park_classes import ALLOWED_ACTIONS, EXISTING_ACTIONS, NEW_ACTIONS

#: The scenarios shipped with this repository. ``--scenarios`` replaces this set.
DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "tests" / "overlord_scenarios"

_FIELDS = frozenset({"id", "kind", "description", "park_class", "question", "expected", "forbidden"})
_KINDS = ("triage", "merge")
_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")
_TRIAGE_ANSWERS = frozenset(EXISTING_ACTIONS | NEW_ACTIONS)
_MERGE_ANSWERS = frozenset({"proceed", "park"})
_TRIAGE_PREFIX = "\nTRIAGE QUESTION:"
_TRIAGE_MARKERS = ("OBSERVED PARK CLASS:", "ACTION:")
_MERGE_PREFIX = "MERGE ADJUDICATION:"


def _read(name: str, path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # The decoder's message can echo file content, so it is not repeated.
        raise ValueError(f"{name}: is not readable as JSON") from None


def _check_park_class(name: str, kind: str, value: Any) -> None:
    if kind == "merge":
        valid = value == HIGH_RISK_MERGE_CLASS
    else:
        valid = isinstance(value, str) and value in ALLOWED_ACTIONS
    if not valid:
        raise ValueError(
            f"{name}: park_class must be {HIGH_RISK_MERGE_CLASS!r} for merge, "
            "or a known triage park class"
        )


def _check_question(name: str, kind: str, value: Any) -> None:
    if kind == "triage":
        ok = isinstance(value, str) and value.startswith(_TRIAGE_PREFIX)
        ok = ok and all(marker in value for marker in _TRIAGE_MARKERS)
    else:
        ok = isinstance(value, str) and value.startswith(_MERGE_PREFIX)
        ok = ok and _MERGE_RESPONSE_MARKER in value
    if not ok:
        raise ValueError(f"{name}: question must be the {kind} prompt as the overlord receives it")


def _check_answers(name: str, field: str, value: Any, allowed: frozenset[str], *, required: bool) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(answer, str) for answer in value):
        raise ValueError(f"{name}: {field} must be a list of answer strings")
    if required and not value:
        raise ValueError(f"{name}: {field} must name at least one answer")
    for answer in value:
        if answer not in allowed:
            raise ValueError(f"{name}: {field} names an unknown answer: {answer}")
    return value


def _validate(name: str, stem: str, data: Any) -> None:
    if type(data) is not dict:  # json.loads yields exactly dict for an object
        raise ValueError(f"{name}: must be one JSON object")
    missing = _FIELDS - data.keys()
    if missing:
        raise ValueError(f"{name}: missing field(s): {', '.join(sorted(missing))}")
    unknown = data.keys() - _FIELDS
    if unknown:
        raise ValueError(f"{name}: unknown field(s): {', '.join(sorted(unknown))}")
    if not _ID_PATTERN.fullmatch(stem) or data["id"] != stem:
        raise ValueError(f"{name}: id must equal the file name without .json")
    kind = data["kind"]
    if kind not in _KINDS:
        raise ValueError(f"{name}: kind must be 'triage' or 'merge'")
    description = data["description"]
    if not isinstance(description, str) or not description.strip() or "\n" in description:
        raise ValueError(f"{name}: description must be one non-empty line")
    _check_park_class(name, kind, data["park_class"])
    _check_question(name, kind, data["question"])
    allowed = _TRIAGE_ANSWERS if kind == "triage" else _MERGE_ANSWERS
    expected = _check_answers(name, "expected", data["expected"], allowed, required=True)
    forbidden = _check_answers(name, "forbidden", data["forbidden"], allowed, required=False)
    if set(expected) & set(forbidden):
        raise ValueError(f"{name}: expected and forbidden overlap")
    labels = scan_for_secrets(json.dumps(data, ensure_ascii=False))
    if labels:
        raise ValueError(f"{name}: contains a secret pattern ({labels[0]}); write the value as [redacted]")


def load_scenarios(dirs: list[Path]) -> list[dict[str, Any]]:
    """Return every scenario in ``dirs``, validated. Raises ValueError naming the bad file.

    Ids must be unique across all directories, because a run reports results by id.
    """
    scenarios: list[dict[str, Any]] = []
    owner: dict[str, str] = {}
    for directory in dirs:
        if not Path(directory).is_dir():
            raise ValueError("scenarios directory is missing or is not a directory")
        for path in sorted(Path(directory).glob("*.json")):
            data = _read(path.name, path)
            _validate(path.name, path.stem, data)
            if data["id"] in owner:
                raise ValueError(f"{path.name}: id is already defined by {owner[data['id']]}")
            owner[data["id"]] = path.name
            scenarios.append(data)
    return scenarios


def run_scenarios(
    scenarios: list[dict[str, Any]], *, policy: str, context: str, runs: int
) -> list[dict[str, Any]]:
    """Ask the overlord ``runs`` times per scenario. A scenario passes when every answer is expected."""
    inputs = _Inputs(policy=policy, context=context)
    results = []
    for scenario in scenarios:
        merge = scenario["kind"] == "merge"
        record = {
            "question": _ADJUDICATION_QUESTION if merge else _TRIAGE_QUESTION,
            "input": {"evidence": scenario["question"], "truncated": False},
        }
        prompt, _ = _rebuild_prompt(record, inputs)
        answers = [
            _ask(prompt, merge=merge, plan="scenarios", story_key=scenario["id"]) for _ in range(runs)
        ]
        passed = all(
            answer in scenario["expected"] and answer not in scenario["forbidden"] for answer in answers
        )
        results.append({"id": scenario["id"], "kind": scenario["kind"], "answers": answers, "passed": passed})
    return results


def _print_table(results: list[dict[str, Any]]) -> None:
    print(f"{'id':<40}{'kind':<8}{'answers':<36}result")
    for row in results:
        answers = ", ".join(row["answers"]) or "-"
        verdict = "PASS" if row["passed"] else "FAIL"
        print(f"{row['id']:<40}{row['kind']:<8}{answers:<36}{verdict}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Read-only. Returns 0 when every scenario passes, 1 otherwise."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.overlord_scenarios",
        description=(
            "Ask the overlord the known-answer hard cases and report which pass. "
            "Read-only; calls the overlord."
        ),
    )
    parser.add_argument(
        "--scenarios", action="append", type=Path, metavar="DIR",
        help="scenario directory, repeatable; replaces the shipped set (default: %(default)s)",
    )
    parser.add_argument(
        "--policy", metavar="FILE", help="policy text for triage prompts (default: the shipped policy)"
    )
    parser.add_argument(
        "--context", metavar="DIR", help="context sources for every prompt, one per regular file"
    )
    parser.add_argument(
        "--runs", type=_int_between(1, MAX_RUNS), default=DEFAULT_RUNS,
        help=f"overlord calls per scenario, 1 to {MAX_RUNS} (default: %(default)s)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    try:
        scenarios = load_scenarios(args.scenarios or [DEFAULT_SCENARIOS_DIR])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not scenarios:
        print("error: no scenario files were found", file=sys.stderr)
        return 1
    try:
        policy = _read_policy(args.policy)
        context = "" if args.context is None else _read_context_dir(args.context)
    except _InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    results = run_scenarios(scenarios, policy=policy, context=context, runs=args.runs)
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        _print_table(results)
    return 0 if all(row["passed"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
