"""Aggregate benchmark cell results into a model-comparison scorecard.

Reads a list of per-cell result dicts (as produced by harness.py and collected
by matrix.py) and renders a markdown table comparing models across tasks.

A cell is a SUCCESS only when the plan completed autonomously AND the code is
actually correct: final_status == "done" AND groundtruth_passed. "merged but
groundtruth failed" is surfaced separately -- it is the pipeline landing wrong
code, the most important failure mode to see.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


def _is_success(cell: dict) -> bool:
    return cell.get("final_status") == "done" and cell.get("groundtruth_passed") is True


def _pct(n: int, d: int) -> str:
    return f"{100 * n // d}%" if d else "-"


def aggregate(cells: list[dict]) -> dict:
    """Group cells by (task, model) and compute per-cell-group stats."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in cells:
        groups[(c["task"], c["model"])].append(c)

    stats: dict[tuple[str, str], dict] = {}
    for key, group in groups.items():
        n = len(group)
        stats[key] = {
            "trials": n,
            "success": sum(_is_success(c) for c in group),
            "merged": sum(bool(c.get("merged")) for c in group),
            "gt_pass": sum(bool(c.get("groundtruth_passed")) for c in group),
            "merged_wrong": sum(
                bool(c.get("merged")) and not c.get("groundtruth_passed") for c in group
            ),
            "timeouts": sum(bool(c.get("timed_out")) for c in group),
            "avg_elapsed": round(sum(c.get("elapsed_s", 0) for c in group) / n, 1),
            "avg_ticks": round(sum(c.get("ticks", 0) for c in group) / n, 1),
        }
    return stats


def render(cells: list[dict]) -> str:
    stats = aggregate(cells)
    tasks = sorted({t for t, _ in stats})
    models = sorted({m for _, m in stats})

    lines: list[str] = []
    lines.append("# Pipeline Benchmark Scorecard\n")
    lines.append(
        "Success = plan reached `done` AND the independent ground-truth passed "
        "(autonomous completion of correct code). Cells show `success/trials`.\n"
    )

    # --- success-rate matrix ---
    header = "| Task | " + " | ".join(models) + " |"
    sep = "|------|" + "|".join(["------"] * len(models)) + "|"
    lines.append(header)
    lines.append(sep)
    for task in tasks:
        row = [f"| {task} "]
        for model in models:
            s = stats.get((task, model))
            if s is None:
                row.append("| - ")
            else:
                row.append(f"| {s['success']}/{s['trials']} ({_pct(s['success'], s['trials'])}) ")
        lines.append("".join(row) + "|")

    # --- per-model rollup ---
    lines.append("\n## Per-model totals\n")
    lines.append("| Model | Success | Merged | GT-pass | Merged-but-wrong | Timeouts | Avg s | Avg ticks |")
    lines.append("|-------|---------|--------|---------|------------------|----------|-------|-----------|")
    for model in models:
        msl = [s for (t, m), s in stats.items() if m == model]
        trials = sum(s["trials"] for s in msl)
        succ = sum(s["success"] for s in msl)
        merged = sum(s["merged"] for s in msl)
        gtp = sum(s["gt_pass"] for s in msl)
        mw = sum(s["merged_wrong"] for s in msl)
        to = sum(s["timeouts"] for s in msl)
        avg_s = round(sum(s["avg_elapsed"] * s["trials"] for s in msl) / trials, 1) if trials else 0
        avg_t = round(sum(s["avg_ticks"] * s["trials"] for s in msl) / trials, 1) if trials else 0
        lines.append(
            f"| {model} | {succ}/{trials} ({_pct(succ, trials)}) | {merged}/{trials} | "
            f"{gtp}/{trials} | {mw} | {to} | {avg_s} | {avg_t} |"
        )

    lines.append(
        "\n> **Merged-but-wrong** counts cells the pipeline merged whose code "
        "the ground-truth rejected — the pipeline accepting incorrect work. Any "
        "non-zero value here is a pipeline-quality signal worth investigating.\n"
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, help="path to matrix results.json")
    ap.add_argument("--out", default=None, help="output .md path (default: stdout)")
    args = ap.parse_args()

    cells = json.loads(Path(args.results).read_text())
    md = render(cells)
    if args.out:
        Path(args.out).write_text(md)
        print(f"wrote {args.out}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
