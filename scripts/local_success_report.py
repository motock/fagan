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

    .venv/bin/python scripts/local_success_report.py [--window 30] [--plan-dir DIR] [--repo PATH] [--all-repos]

The ``--window`` flag selects how many of the newest stories in the
population to report; every tier block is computed over that same cohort
(``--window 0`` means all stories), so the tier rates partition the
overall rate instead of each covering a different period.  The
``--plan-dir`` flag points to the directory
containing plan sidecars.  It applies to the file store only: the records are
read through the store that ``PIPELINE_STORE`` selects, so on a sqlite
deployment the flag is ignored and the report reads the database.  If the
file store's directory does not exist, the command prints a message to
``stderr`` and exits with status ``2``.

--window 0

"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

# Ensure the repository root is on sys.path so that the pipeline
# package can be imported.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.local_success import classify_story, rolling_rate
from pipeline.store import build_store, store_kind
from pipeline.store_records import DEFAULT_NAMESPACE

TIERS = ("on-device", "cloud-oss", "unknown")

# The file store resolves its plans directory from module bindings made at
# import time, so --plan-dir re-points these for the duration of one call.
_PLAN_DIR_MODULES = ("pipeline.paths", "pipeline.persistence", "pipeline.server")


class _PlanDirBinding:
    """Point the file store at ``plan_dir`` for one ``with`` block; no-op on other stores."""

    def __init__(self, plan_dir: Path) -> None:
        self._plan_dir = plan_dir
        self._saved: list[tuple[object, Path]] = []

    def __enter__(self):
        if store_kind() != "file":
            return self
        modules = [importlib.import_module(name) for name in _PLAN_DIR_MODULES]
        self._saved = [(module, module.PLAN_DIR) for module in modules]
        for module in modules:
            module.PLAN_DIR = self._plan_dir
        return self

    def __exit__(self, *exc_info) -> None:
        for module, value in self._saved:
            module.PLAN_DIR = value
        self._saved = []


def _load_journal(records, plan: str, key: str) -> list[dict]:
    """Return the journal entries for one story, failing open.

    A story with no journal, or a key the store refuses, yields ``[]``;
    non-dict entries are dropped so the classifier only ever sees dicts.
    """
    try:
        entries = records.get_journal(DEFAULT_NAMESPACE, plan, key)
    except ValueError:
        return []
    return [e for e in entries if isinstance(e, dict)]


def _classify_plan(records, plan: str, notifications: list[dict], manifest: dict) -> list[dict]:
    """Return a list of classified stories for a single plan."""
    classified: list[dict] = []
    for key, story in manifest.get("stories", {}).items():
        if not isinstance(story, dict):
            continue
        classified.append(
            classify_story(key, story, notifications, journal=_load_journal(records, plan, key))
        )
    return classified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local success report")
    parser.add_argument(
        "--plan-dir",
        default=os.environ.get("PLAN_DIR", "~/.claude/plans"),
        help="Directory containing plan sidecars",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=ROOT,
        help="Restrict to manifests whose repo_root matches PATH (default: this repo).",
    )
    parser.add_argument(
        "--all-repos",
        action="store_true",
        help="Ignore the repo filter and report every manifest.",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=30,
        help="Size of the rolling window; 0 means all stories",
    )
    args = parser.parse_args(argv)

    # Repo filter
    if args.all_repos:
        repo_filter = None
    else:
        repo_filter = Path(args.repo).expanduser().resolve()

    
    def _repo_matches(plan: dict) -> bool:
        if repo_filter is None:
            return True
        raw = plan.get("repo_root")
        if not raw:
            return False
        try:
            return Path(raw).expanduser().resolve() == repo_filter
        except (OSError, ValueError):
            return False


    plan_dir = Path(args.plan_dir).expanduser()
    if store_kind() == "file" and not plan_dir.is_dir():
        print(f"error: plan directory {plan_dir} does not exist", file=sys.stderr)
        return 2

    # Collect classified stories from all plans, read through the selected store.
    all_classified: list[dict] = []
    with _PlanDirBinding(plan_dir):
        records = build_store().records()
        for plan_name in records.list_plans(DEFAULT_NAMESPACE):
            manifest = records.get_manifest(DEFAULT_NAMESPACE, plan_name)
            if manifest is None:
                print(f"warning: failed to load manifest {plan_name}.manifest.json", file=sys.stderr)
                continue
            if not _repo_matches(manifest):
                continue
            notifications, _malformed = records.get_notification_history(DEFAULT_NAMESPACE, plan_name)
            all_classified.extend(_classify_plan(records, plan_name, notifications, manifest))

    # Header
    print(f"window: {args.window}")
    print(f"repo: {'all' if repo_filter is None else repo_filter}")

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
