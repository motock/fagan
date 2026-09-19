"""Local success report CLI.

This script is a thin I/O wrapper around :mod:`pipeline.local_success`.
It reads plan manifests and notification records from a plan directory and
prints a rolling‑rate report for the W0 metric.

The report is produced by :func:`pipeline.local_success.rolling_rate` and
:func:`pipeline.local_success.classify_story`.  The script does not perform
any classification logic itself.

Usage
-----

```.venv/bin/python scripts/local_success_report.py [--window 30] [--plan-dir DIR]```

The ``--window`` option controls the number of most‑recent stories to
include.  ``0`` means *all* stories (the full‑population baseline).

The ``--plan-dir`` option defaults to the value of the ``PLAN_DIR``
environment variable or ``~/.claude/plans`` if that variable is not set.

The script prints a header line naming the window, followed by a block for
``overall`` and then one block for each tier in the order
``on-device``, ``cloud-oss``, ``unknown``.  Each block contains a line
``<label>: <clean>/<count> clean (<pct>%)`` where ``pct`` is shown with one
decimal place.  If ``count`` is zero the percentage is shown as ``n/a``.

Under each block the reasons for dirty stories are listed, indented and
sorted by count descending then name ascending.

The script exits with status ``0`` on success, ``2`` if the plan directory
does not exist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Insert the repository root into ``sys.path`` so that the ``pipeline``
# package can be imported when the script is executed from a checkout.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Import only the helpers that are required.  Importing other parts of the
# application (e.g. ``pipeline.server``) would create directories and load
# configuration at import time, which is undesirable for a CLI.
from pipeline.local_success import classify_story
from pipeline.story_metrics import load_notification_records


def _load_and_classify(plan_dir: Path) -> list[tuple[str, str, str]]:
    """Return a list of ``(tier, reason, key)`` tuples for all stories.

    Each tuple represents a story that was classified as *dirty*.
    ``tier`` is one of ``on-device``, ``cloud-oss`` or ``unknown``.
    ``reason`` is the reason string returned by :func:`classify_story`.
    ``key`` is the story key.
    """
    results: list[tuple[str, str, str]] = []
    for manifest_path in sorted(plan_dir.glob("*.manifest.json")):
        plan_name = manifest_path.stem.replace(".manifest", "")
        try:
            manifest_text = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
        except (OSError, ValueError) as exc:
            print(f"warning: failed to load manifest {plan_name}: {exc}", file=sys.stderr)
            continue
        stories = manifest.get("stories", {})
        if not isinstance(stories, dict):
            continue
        records, _ = load_notification_records(plan_dir / f"{plan_name}.notifications.jsonl")
        for key, story in stories.items():
            if not isinstance(story, dict):
                continue
            tier, reason = classify_story(key, story, records)
            if reason:
                results.append((tier, reason, key))
    return results


def _print_block(label: str, data: list[tuple[str, str, str]]) -> None:
    """Print a single tier block.

    ``data`` contains tuples ``(tier, reason, key)`` for the stories in this
    tier.  The function aggregates the counts and prints the required
    formatting.
    """
    total = len(data)
    clean = sum(1 for _, reason, _ in data if not reason)
    pct = f"{(clean / total * 100):.1f}%" if total else "n/a"
    print(f"{label}: {clean}/{total} clean ({pct})")
    reason_counts: dict[str, int] = {}
    for _, reason, _ in data:
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    for reason, count in sorted(reason_counts.items(), key=lambda x: (-x[1], x[0])):
        print(f"    {reason}: {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report local success rates.")
    parser.add_argument(
        "--plan-dir",
        default=os.environ.get("PLAN_DIR", "~/.claude/plans"),
        type=Path,
        help="Directory containing plan manifests and notification records.",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=30,
        help="Number of most‑recent stories to include; 0 means all.",
    )
    args = parser.parse_args(argv)

    plan_dir = args.plan_dir.expanduser()
    if not plan_dir.is_dir():
        print(f"error: plan directory {plan_dir} does not exist", file=sys.stderr)
        return 2

    classified = _load_and_classify(plan_dir)
    tier_map: dict[str, list[tuple[str, str, str]]] = {
        "on-device": [],
        "cloud-oss": [],
        "unknown": [],
    }
    for tier, reason, key in classified:
        tier_map.setdefault(tier, []).append((tier, reason, key))

    window_name = "all" if args.window == 0 else str(args.window)
    print(f"Window: {window_name}")
    _print_block("overall", classified)
    for tier in ("on-device", "cloud-oss", "unknown"):
        _print_block(tier, tier_map.get(tier, []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
