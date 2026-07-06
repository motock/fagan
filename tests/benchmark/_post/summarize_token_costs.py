"""Summarize per-call token costs across two benchmark runs.

Walks two run dirs (each containing <cell>/worktrees/review_token_costs.jsonl
written by the patched Backend drivers) and emits a markdown comparison
table to stdout, breaking down input/output tokens, USD cost, and wall-clock
duration by per-cell verdict and at the run level. Pairs with the 2026-07-06
gptoss_glm_review (local review) baseline and the upcoming
gptoss_claude_review run to quantify the driver-level token-spend fixes
(drop memory: user on reviewers, --max-tokens 4096 cap, tightened persona
prose).

Usage:
    python tests/benchmark/_post/summarize_token_costs.py \\
        <run_a_dir> <run_a_label> <run_b_dir> <run_b_label>

Each run dir is the same shape as tests/benchmark/_runs/<name>_<ts>/: a
directory of <task>__<model>__t<trial>/ subdirs, each with a worktrees/
subdir holding review_token_costs.jsonl (one record per chat call +
verdict-bearing terminal row). Cells without a sidecar are excluded
from the totals and reported under "missing sidecar".

The verdict-bearing terminal row has zero input_tokens and
output_tokens by design (it tags the verdict on the prior step's row);
it carries the verdict field and is what the per-cell verdict
breakdown is keyed on.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def _load_sidecar(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL sidecar; skip empty/missing files (no review happened
    on this cell, or the run was started before the patch landed)."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    out: list[dict[str, Any]] = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            out.append(json.loads(ln))
    return out


def _summarize_run(run_dir: Path) -> dict[str, Any]:
    """Aggregate one run's per-cell sidecar data into a totals dict."""
    if not run_dir.is_dir():
        raise SystemExit(f"run dir not found: {run_dir}")

    per_cell: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for cell in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        # The sidecar lives at <cell>/worktrees/review_token_costs.jsonl
        # because _run_reviewer writes it to the worktree's parent dir
        # only when the parent is named "worktrees" (the benchmark cell
        # layout). Other layouts (production) skip the sidecar; we
        # only summarize runs that put cells under <cell>/worktrees/.
        sidecar = cell / "worktrees" / "review_token_costs.jsonl"
        records = _load_sidecar(sidecar)
        if not records:
            missing.append(cell.name)
            continue
        in_t = sum(r.get("input_tokens", 0) or 0 for r in records)
        out_t = sum(r.get("output_tokens", 0) or 0 for r in records)
        cost = sum((r.get("total_cost_usd") or 0) for r in records)
        dur_ms = sum((r.get("duration_ms") or 0) for r in records)
        dur_ns = sum((r.get("duration_ns") or 0) for r in records)
        # The verdict-bearing terminal row has verdict set; pick the
        # first such row (only one per cell in practice).
        verdicts = [r.get("verdict") for r in records if r.get("verdict")]
        per_cell[cell.name] = {
            "calls": len([r for r in records if r.get("verdict") is None]),
            "in_t": in_t,
            "out_t": out_t,
            "cost": cost,
            "dur_ms": dur_ms + dur_ns // 1_000_000,
            "verdict": verdicts[0] if verdicts else None,
        }
    return {"per_cell": per_cell, "missing": missing}


def _row(label: str, agg: dict[str, Any], other_agg: dict[str, Any] | None) -> str:
    in_t = agg["in_t"]
    out_t = agg["out_t"]
    cost = agg["cost"]
    dur_s = agg["dur_ms"] / 1000
    delta = ""
    if other_agg is not None and other_agg["in_t"]:
        delta_pct = (in_t - other_agg["in_t"]) / other_agg["in_t"] * 100
        delta = f"{delta_pct:+.1f}%"
    return (f"| {label} | {agg['calls']} | {in_t:,} | {out_t:,} | "
            f"${cost:.4f} | {dur_s:.1f}s | {delta} |")


def _emit_table(a_label: str, a: dict[str, Any], b_label: str, b: dict[str, Any]) -> str:
    a_total = {
        "calls": sum(c["calls"] for c in a["per_cell"].values()),
        "in_t": sum(c["in_t"] for c in a["per_cell"].values()),
        "out_t": sum(c["out_t"] for c in a["per_cell"].values()),
        "cost": sum(c["cost"] for c in a["per_cell"].values()),
        "dur_ms": sum(c["dur_ms"] for c in a["per_cell"].values()),
    }
    b_total = {
        "calls": sum(c["calls"] for c in b["per_cell"].values()),
        "in_t": sum(c["in_t"] for c in b["per_cell"].values()),
        "out_t": sum(c["out_t"] for c in b["per_cell"].values()),
        "cost": sum(c["cost"] for c in b["per_cell"].values()),
        "dur_ms": sum(c["dur_ms"] for c in b["per_cell"].values()),
    }
    lines: list[str] = []
    lines.append(f"# Token cost comparison: {a_label} vs {b_label}")
    lines.append("")
    lines.append(
        "| Run | Calls | Input tok | Output tok | USD | Wall | Δ input vs other |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    lines.append(_row(f"**{a_label}** (total)", a_total, b_total))
    lines.append(_row(f"**{b_label}** (total)", b_total, a_total))
    lines.append("")

    # Per-cell rows, paired by task (cells in each run share a common
    # <task>__<model>__t<trial> naming scheme; we group by task prefix
    # to show side-by-side per-task numbers).
    by_task_a: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    by_task_b: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for name, agg in a["per_cell"].items():
        task = name.split("__")[0]
        by_task_a[task].append((name, agg))
    for name, agg in b["per_cell"].items():
        task = name.split("__")[0]
        by_task_b[task].append((name, agg))
    tasks = sorted(set(by_task_a) | set(by_task_b))
    if tasks:
        lines.append("## Per-task (sum across trials)")
        lines.append("")
        lines.append("| Task | " + " | ".join(
            [f"{a_label} in_t", f"{a_label} out_t", f"{a_label} USD",
             f"{b_label} in_t", f"{b_label} out_t", f"{b_label} USD"]
        ) + " |")
        lines.append("|---|---|---|---|---|---|---|")
        for task in tasks:
            a_agg = {"in_t": sum(c["in_t"] for _, c in by_task_a[task]),
                     "out_t": sum(c["out_t"] for _, c in by_task_a[task]),
                     "cost": sum(c["cost"] for _, c in by_task_a[task])}
            b_agg = {"in_t": sum(c["in_t"] for _, c in by_task_b[task]),
                     "out_t": sum(c["out_t"] for _, c in by_task_b[task]),
                     "cost": sum(c["cost"] for _, c in by_task_b[task])}
            lines.append(
                f"| {task} | {a_agg['in_t']:,} | {a_agg['out_t']:,} | "
                f"${a_agg['cost']:.4f} | {b_agg['in_t']:,} | "
                f"{b_agg['out_t']:,} | ${b_agg['cost']:.4f} |"
            )
        lines.append("")

    if a["missing"] or b["missing"]:
        lines.append("## Cells without a sidecar (excluded from totals)")
        lines.append("")
        if a["missing"]:
            lines.append(f"- {a_label}: {', '.join(a['missing'])}")
        if b["missing"]:
            lines.append(f"- {b_label}: {', '.join(b['missing'])}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    a_dir = Path(argv[1])
    a_label = argv[2]
    b_dir = Path(argv[3])
    b_label = argv[4]
    a = _summarize_run(a_dir)
    b = _summarize_run(b_dir)
    print(_emit_table(a_label, a, b_label, b))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
