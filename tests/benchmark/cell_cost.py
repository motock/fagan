"""Per-cell Claude spend accounting for the published benchmark scorecard.

Two independent cost sources exist on disk for a benchmark cell:

* **review spend** - ``app/backend_claude.py``'s ``complete()`` appends one JSON
  object per Claude call (carrying ``"total_cost_usd"``) to
  ``<cell>/worktrees/review_token_costs.jsonl``. Those records have no
  ``"type"`` field, so every record counts.
* **dispatch spend** - ``ClaudeCliDriver.dispatch()`` mirrors every raw
  stream-json line to ``<worktree>/agent.log.raw``; only the terminal
  ``{"type": "result", ...}`` line carries the run's ``"total_cost_usd"``.
  The harness's ``_merge_pr_stub`` runs ``git worktree remove --force`` on
  merge, so ``harness._preserve_dispatch_log`` copies that file out to
  ``<worktrees>/<story_key>.agent.log.raw`` first.

Local (Ollama) dispatches write no cost lines at all, so a cell with no
artifacts must score 0.0 rather than raise.
"""
from __future__ import annotations

import json
from pathlib import Path


def _sum_cost_lines(path: Path, *, result_events_only: bool) -> float:
    """Sum ``total_cost_usd`` over the JSON-object lines of *path*.

    Unparseable lines, non-object JSON values and non-numeric costs are
    skipped silently: a partially written or truncated log must never break
    the scorecard. ``bool`` is excluded explicitly because it is a subclass of
    ``int`` (``True`` would otherwise add 1.0). An unreadable file contributes
    0.0.
    """
    total = 0.0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if result_events_only and obj.get("type") != "result":
                    continue
                value = obj.get("total_cost_usd")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    total += value
    except OSError:
        return 0.0
    return total


def claude_spend_usd(cell_dir) -> dict:
    """Return the cell's Claude spend in USD, rounded to 6 decimal places.

    ``cell_dir`` may be a ``str`` or a ``Path`` (``pipeline/review.py`` passes a
    str). A missing or empty cell dir yields all zeros.
    """
    root = Path(cell_dir)
    if not root.is_dir():
        return {"review_usd": 0.0, "dispatch_usd": 0.0, "total_usd": 0.0}
    review = sum(
        _sum_cost_lines(p, result_events_only=False)
        for p in root.rglob("review_token_costs.jsonl")
    )
    dispatch = sum(
        _sum_cost_lines(p, result_events_only=True) for p in root.rglob("*.log.raw")
    )
    return {
        "review_usd": round(float(review), 6),
        "dispatch_usd": round(float(dispatch), 6),
        "total_usd": round(float(review + dispatch), 6),
    }
