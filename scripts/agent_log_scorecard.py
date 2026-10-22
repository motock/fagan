"""Per-run scorecard derived from an ``agent.log``.

Mode 36's retro process reconstructs run quality from notification records and
manifest state, while ``agent.log`` already records every step the agent took
but nothing summarizes it. This module is a pure reader over that existing log:
it measures how many steps a run took, which tools it used, when it first
mutated a file, how much of the run was productive (mutation) work rather than
reading, which guard nudges fired and how often, and how the run ended. That is
Mode 36's deferred per-run measurement item; no agent-loop code changes.

Usage::

    python scripts/agent_log_scorecard.py <worktree>/agent.log [--json]

The line grammar it reads is defined once in ``pipeline/agent_log_format.py``,
so this reader cannot drift from the writers.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Ensure the repository root is on sys.path so that the pipeline
# package can be imported.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.agent_log_format import (
    BOOT_LINE_RE,
    DONE_LINE_RE,
    NUDGE_LINE_RE,
    STEP_LINE_RE,
)

# Tools whose effect is a file mutation. ``bash`` is deliberately excluded: the
# log grammar records only the command text, so a reader cannot classify
# whether a given command mutated anything.
MUTATING_TOOLS = frozenset({
    "create_file", "str_replace", "replace_lines", "restore_file"
})

# A trailing ``[cid=<hex>]`` marker on a step or DONE line.
_CID_RE = re.compile(r"\[cid=([0-9a-fA-F]+)\]$")
_DIGITS_RE = re.compile(r"\d+")
_PARKING_RE = re.compile(r"\bparking\b")


def _parse_boot(fields: str) -> dict:
    """Return the ``[boot]`` line's ``key=value`` fields as a typed dict.

    ``steps`` is coerced to ``int`` and ``timeout`` (which renders with a
    trailing ``s``) to ``float``; every other value stays a string. Tokens
    without ``=`` are skipped, and a value that fails to coerce is kept as the
    raw string rather than raising.
    """
    boot: dict = {}
    for token in fields.split():
        key, sep, value = token.partition("=")
        if not sep:
            continue
        if key == "steps":
            try:
                boot[key] = int(value)
            except ValueError:
                boot[key] = value
        elif key == "timeout":
            try:
                boot[key] = float(value.removesuffix("s"))
            except ValueError:
                boot[key] = value
        else:
            boot[key] = value
    return boot


def _normalize_label(text: str) -> str:
    """Return a guard-trip label with its variable parts removed.

    The label is cut at the first ``:`` or ``;`` and every run of digits is
    replaced with ``N``, so "no scratchpad update in 15 steps" and the same
    line at 30 steps group together.
    """
    cut = len(text)
    for sep in (":", ";"):
        index = text.find(sep)
        if index != -1:
            cut = min(cut, index)
    return " ".join(_DIGITS_RE.sub("N", text[:cut]).split())


def _correlation_id(text: str) -> str | None:
    """Return the trailing ``[cid=<hex>]`` value of a line, if any."""
    match = _CID_RE.search(text)
    return match.group(1) if match else None


def parse_agent_log(text: str) -> dict:
    """Return the scorecard for one ``agent.log`` body.

    Malformed or unrelated lines are ignored rather than raising: this is a
    reader over logs written by several producers, so it must never fail on
    input it does not recognize.
    """
    boot: dict | None = None
    steps = 0
    tool_counts: dict[str, int] = {}
    mutation_step_numbers: set[int] = set()
    guard_trips: dict[str, int] = {}
    done_summary: str | None = None
    parked_reason: str | None = None
    correlation_id: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        boot_match = BOOT_LINE_RE.match(line)
        if boot_match is not None:
            if boot is None:
                boot = _parse_boot(boot_match.group(1))
            continue

        done_match = DONE_LINE_RE.match(line)
        if done_match is not None:
            step = int(done_match.group(1))
            steps = max(steps, step)
            done_summary = done_match.group(2)
            correlation_id = _correlation_id(line) or correlation_id
            continue

        step_match = STEP_LINE_RE.match(line)
        if step_match is not None:
            step = int(step_match.group(1))
            tool = step_match.group(2)
            steps = max(steps, step)
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
            if tool in MUTATING_TOOLS:
                mutation_step_numbers.add(step)
            correlation_id = _correlation_id(line) or correlation_id
            continue

        nudge_match = NUDGE_LINE_RE.match(line)
        if nudge_match is not None:
            label = _normalize_label(nudge_match.group(1))
            guard_trips[label] = guard_trips.get(label, 0) + 1
            continue

        if parked_reason is None and _PARKING_RE.search(line):
            parked_reason = line.strip()[:200]

    if done_summary is not None:
        outcome = "done"
    elif parked_reason is not None:
        outcome = "parked"
    else:
        outcome = "unknown"

    mutation_steps = len(mutation_step_numbers)
    return {
        "ok": True,
        "boot": boot,
        "steps": steps,
        "tool_counts": dict(sorted(tool_counts.items())),
        "mutating_tool_counts": {
            tool: count
            for tool, count in sorted(tool_counts.items())
            if tool in MUTATING_TOOLS
        },
        "first_mutation_step": (
            min(mutation_step_numbers) if mutation_step_numbers else None
        ),
        "mutation_steps": mutation_steps,
        "productive_ratio": (
            round(mutation_steps / steps, 3) if steps else 0.0
        ),
        "guard_trips": dict(sorted(guard_trips.items())),
        "outcome": outcome,
        "done_summary": done_summary,
        "parked_reason": parked_reason,
        "correlation_id": correlation_id,
    }


def _render_human(score: dict, path: Path) -> str:
    """Return a short human-readable summary of ``score``."""
    boot = score["boot"] or {}
    lines = [
        f"agent.log: {path}",
        (
            f"model: {boot.get('model', 'unknown')}  "
            f"max_steps: {boot.get('steps', 'unknown')}  "
            f"outcome: {score['outcome']}"
        ),
        (
            f"steps: {score['steps']}  "
            f"mutations: {score['mutation_steps']}  "
            f"first_mutation_step: {score['first_mutation_step']}  "
            f"productive_ratio: {score['productive_ratio']}"
        ),
    ]
    if score["done_summary"] is not None:
        lines.append(f"done: {score['done_summary']}")
    if score["parked_reason"] is not None:
        lines.append(f"parked: {score['parked_reason']}")
    for label, count in score["guard_trips"].items():
        lines.append(f"guard trip: {label} x{count}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Print the scorecard for one agent.log; return the process exit code."""
    parser = argparse.ArgumentParser(
        description="Summarize a per-run agent.log scorecard."
    )
    parser.add_argument("log", type=Path, help="path to <worktree>/agent.log")
    parser.add_argument(
        "--json", action="store_true", help="emit the full record as JSON"
    )
    args = parser.parse_args(argv)

    try:
        text = args.log.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        print(f"ERROR: no agent.log at {args.log}", file=sys.stderr)
        return 2

    score = parse_agent_log(text)
    if args.json:
        print(json.dumps(score, indent=2, sort_keys=True))
    else:
        print(_render_human(score, args.log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
