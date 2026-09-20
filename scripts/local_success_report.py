"""Local success report CLI.

This script implements the ``local_success_report`` command used to
calculate the first‑pass‑clean rate for local (non‑Claude) stories.

It relies on :mod:`pipeline.local_success` for classification and
rolling‑rate calculation, and on :mod:`pipeline.story_metrics` for
loading notification records.

The module depends on :mod:`pipeline.local_success` and
:mod:`pipeline.story_metrics`; see ``pipeline/local_success.py`` for
implementation details.

Usage:

::

    .venv/bin/python scripts/local_success_report.py [--window 30] [--plan-dir DIR]

The ``--window`` flag selects how many of the newest stories in the
population to report; every tier block is computed over that same cohort
(``--window 0`` means all stories), so the tier rates partition the
overall rate instead of each covering a different period.  The
``--plan-dir`` flag points to the directory
containing plan sidecars.  If the directory does not exist, the
command prints a message to ``stderr`` and exits with status ``2``.

--window 0

"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure the repository root is on sys.path so that the pipeline
# package can be imported.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.local_success import classify_story, rolling_rate
from pipeline.story_metrics import load_notification_records

TIERS = ("on-device", "cloud-oss", "unknown")


def _load_manifest(path: Path) -> dict | None:
    """Load a manifest JSON file.

    Returns ``None`` on error, printing a warning to ``stderr``.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"warning: failed to load manifest {path.name}: {exc}", file=sys.stderr)
        return None


def _classify_plan(plan_dir: Path, plan: str, records: list[dict]) -> list[dict]:
    """Return a list of classified stories for a single plan."""
    manifest_path = plan_dir / f"{plan}.manifest.json"
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        return []
    classified: list[dict] = []
    for key, story in manifest.get("stories", {}).items():
        if isinstance(story, dict):
            classified.append(classify_story(key, story, records))
    return classified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local success report")
    parser.add_argument(
        "--plan-dir",
        default=os.environ.get("PLAN_DIR", "~/.claude/plans"),
        help="Directory containing plan sidecars",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=30,
        help="Size of the rolling window; 0 means all stories",
    )
    args = parser.parse_args(argv)

    plan_dir = Path(args.plan_dir).expanduser()
    if not plan_dir.is_dir():
        print(f"error: plan directory {plan_dir} does not exist", file=sys.stderr)
        return 2

    # Collect classified stories from all plans.
    all_classified: list[dict] = []
    for path in sorted(plan_dir.glob("*.manifest.json")):
        plan_name = path.name.removesuffix(".manifest.json")
        records, _malformed = load_notification_records(plan_dir / f"{plan_name}.notifications.jsonl")
        all_classified.extend(_classify_plan(plan_dir, plan_name, records))

    # Header
    print(f"window: {args.window}")

    # One cohort for every block: the newest N population stories, selected
    # once here so the tier blocks partition the overall block instead of each
    # taking its own last-N window (which made them cover different periods).
    population = [
        entry
        for entry in all_classified
        if entry.get("in_population") and entry.get("dispatched_at")
    ]
    population.sort(key=lambda entry: entry["dispatched_at"])
    if args.window <= 0:
        cohort = population
    else:
        cohort = population[-args.window:]
    if cohort:
        print(
            f"cohort: {cohort[0]['dispatched_at']} .. "
            f"{cohort[-1]['dispatched_at']} ({len(cohort)} stories)"
        )

    # Overall block
    overall = rolling_rate(cohort, window=0)
    count = overall.get("count", 0)
    clean = overall.get("clean", 0)
    rate = overall.get("rate")
    if count == 0:
        print("overall: 0/0 clean (n/a)")
    else:
        pct = f"{rate * 100:.1f}%" if rate is not None else "n/a"
        print(f"overall: {clean}/{count} clean ({pct})")
    for reason, cnt in sorted(overall.get("reasons", {}).items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {reason}: {cnt}")

    # Tier blocks
    for tier in TIERS:
        tier_rate = rolling_rate(cohort, window=0, tier=tier)
        count = tier_rate.get("count", 0)
        clean = tier_rate.get("clean", 0)
        rate = tier_rate.get("rate")
        if count == 0:
            print(f"{tier}: 0/0 clean (n/a)")
        else:
            pct = f"{rate * 100:.1f}%" if rate is not None else "n/a"
            print(f"{tier}: {clean}/{count} clean ({pct})")
        for reason, cnt in sorted(tier_rate.get("reasons", {}).items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {reason}: {cnt}")

    return 0


# _print_block removed; logic inlined in main
# _print_block removed; logic inlined in main


if __name__ == "__main__":
    sys.exit(main())
