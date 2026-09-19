"""LD90-W0-07: the local-success reporting CLI (``scripts/local_success_report.py``).

The CLI is a thin I/O + printing layer over the pure classifier in
``pipeline/local_success``: it walks ``*.manifest.json`` files in a plan
directory, loads the matching ``.notifications.jsonl`` sidecar, classifies
every story dict in the manifest, and prints a windowed clean-rate block for
"overall" plus each tier, with the reason breakdown underneath each block.

Everything here is synthetic: manifests and sidecars are written into
``tmp_path`` and the CLI is imported by path.  No real plan directory (e.g.
``~/.claude/plans``) is read and no live baseline number is asserted - the
"reproduces the recorded baseline" check is a manual, post-merge operator
check run with ``--window 0`` against real data.

"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Import the pure classifier and record loader
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.local_success import classify_story, rolling_rate
from pipeline.story_metrics import load_notification_records

TIER_LABELS = ("on-device", "cloud-oss", "unknown")


def _print_block(label: str, stats: dict) -> None:
    """Print a single tier or overall block.

    ``stats`` is expected to have keys ``clean``, ``count``, ``rate`` and
    ``reasons`` mapping.  ``rate`` may be ``None`` when ``count`` is 0.
    """
    clean = stats["clean"]
    count = stats["count"]
    rate = stats["rate"]
    if count == 0:
        pct_str = "n/a"
    else:
        pct_str = f"{rate * 100:.1f}%"
    print(f"{label}: {clean}/{count} clean ({pct_str})")
    # Print reasons sorted by count desc then name
    for reason, n in sorted(stats["reasons"].items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {reason}: {n}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report local-success metrics for a plan directory.")
    parser.add_argument(
        "--plan-dir",
        default=os.environ.get("PLAN_DIR", "~/.claude/plans"),
        help="Directory containing plan manifests and notification sidecars.",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=30,
        help="Number of most recent stories to include; 0 means all.",
    )
    args = parser.parse_args(argv)

    plan_dir = Path(args.plan_dir).expanduser()
    if not plan_dir.is_dir():
        print(f"plan directory {plan_dir} does not exist", file=sys.stderr)
        return 2

    # Collect all manifests
    manifests = sorted(plan_dir.glob("*.manifest.json"))
    all_classified: list[dict] = []
    for manifest_path in manifests:
        plan = manifest_path.stem
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"warning: failed to load manifest {plan}: {exc}", file=sys.stderr)
            continue
        records, _ = load_notification_records(plan_dir / f"{plan}.notifications.jsonl")
        stories = manifest.get("stories", {})
        for key, story in stories.items():
            if not isinstance(story, dict):
                continue
            classified = classify_story(key, story, records)
            all_classified.append(classified)

    # Compute overall and per-tier stats
    overall_stats = rolling_rate(all_classified, window=args.window)
    tier_stats = {tier: rolling_rate(all_classified, window=args.window, tier=tier) for tier in TIER_LABELS}

    # Print header
    print(f"window: {args.window}")
    _print_block("overall", overall_stats)
    for tier in TIER_LABELS:
        _print_block(tier, tier_stats[tier])

    return 0


if __name__ == "__main__":
    sys.exit(main())
