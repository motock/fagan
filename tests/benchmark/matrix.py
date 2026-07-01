"""Drive the full benchmark grid: tasks x models x trials.

Launches harness.py as a fresh subprocess for every cell (each cell needs its
own process because pipeline_mcp_server bakes PLAN_DIR/WORKTREE_ROOT/REPO_ROOT at
import time). Collects each cell's result.json, writes an aggregate results.json,
and renders a scorecard.md comparing the models.

Cells run SEQUENTIALLY by default: local models share one Ollama/GPU, so running
them concurrently just multiplies memory pressure. Use --jobs > 1 only for an
all-cloud run.

Usage:
    python matrix.py                                   # all tasks, real models, 3 trials
    python matrix.py --models devstral sonnet --trials 1
    python matrix.py --tasks token_bucket lru_cache --models mock
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import scorecard
from models import MODELS

BENCH = Path(__file__).resolve().parent
PIPELINE_REPO = BENCH.parents[1]
VENV_PY = PIPELINE_REPO / ".venv" / "bin" / "python"
PY = str(VENV_PY) if VENV_PY.exists() else sys.executable
TASKS_DIR = BENCH / "tasks"

DEFAULT_MODELS = ["devstral", "minimax", "sonnet"]


def all_tasks() -> list[str]:
    return sorted(d.name for d in TASKS_DIR.iterdir()
                  if d.is_dir() and (d / "spec.json").exists())


def run_cell(task: str, model: str, trial: int, workdir: Path,
             timeout: int, tick: float) -> dict:
    """Run one harness subprocess and return its result dict (or an error stub)."""
    cell_dir = workdir / f"{task}__{model}__t{trial}"
    proc = subprocess.run(
        [PY, str(BENCH / "harness.py"), "--task", task, "--model", model,
         "--trial", str(trial), "--workdir", str(workdir),
         "--timeout", str(timeout), "--tick", str(tick)],
        capture_output=True, text=True,
    )
    result_path = cell_dir / "result.json"
    if result_path.exists():
        return json.loads(result_path.read_text())
    # Harness crashed before writing a result: record a synthetic error cell so
    # one bad cell never aborts the whole grid.
    return {
        "task": task, "model": model, "trial": trial,
        "final_status": "harness_error", "merged": False,
        "groundtruth_passed": False, "groundtruth_ran": False,
        "timed_out": False, "elapsed_s": 0, "ticks": 0,
        "error": (proc.stderr or proc.stdout)[-800:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", nargs="+", default=None,
                    help="task names (default: all under tasks/)")
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--workdir", default=str(BENCH / "_runs"))
    ap.add_argument("--timeout", type=int, default=1800,
                    help="per-cell wall-clock budget, seconds")
    ap.add_argument("--tick", type=float, default=10.0)
    ap.add_argument("--jobs", type=int, default=1,
                    help="parallel cells; keep 1 for local models (shared GPU)")
    ap.add_argument("--out", default=None,
                    help="scorecard .md path (default: <workdir>/scorecard.md)")
    ap.add_argument("--resume", action="store_true",
                    help="skip cells that already have a result.json (resume interrupted run)")
    args = ap.parse_args()

    tasks = args.tasks or all_tasks()
    for m in args.models:
        if m not in MODELS:
            print(f"unknown model {m!r}; known: {list(MODELS)}", file=sys.stderr)
            return 2

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    cells_spec = [(t, m, i) for t in tasks for m in args.models
                  for i in range(args.trials)]

    print(f"running {len(cells_spec)} cells: {len(tasks)} tasks x "
          f"{len(args.models)} models x {args.trials} trials "
          f"(jobs={args.jobs}, timeout={args.timeout}s)", file=sys.stderr)

    results: list[dict] = []
    started = time.time()

    def _go(spec):
        t, m, i = spec
        cell_dir = workdir / f"{t}__{m}__t{i}"
        result_path = cell_dir / "result.json"
        if args.resume and result_path.exists():
            r = json.loads(result_path.read_text())
            print(f"  [{t}/{m}/t{i}] SKIP (existing) {r['final_status']}", file=sys.stderr)
            return r
        t0 = time.time()
        r = run_cell(t, m, i, workdir, args.timeout, args.tick)
        print(f"  [{t}/{m}/t{i}] {r['final_status']:13} "
              f"gt={str(r.get('groundtruth_passed')):5} "
              f"({round(time.time() - t0, 1)}s)", file=sys.stderr)
        return r

    if args.jobs <= 1:
        for spec in cells_spec:
            results.append(_go(spec))
    else:
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = [ex.submit(_go, spec) for spec in cells_spec]
            for f in as_completed(futs):
                results.append(f.result())

    results_path = workdir / "results.json"
    results_path.write_text(json.dumps(results, indent=2))

    out = args.out or str(workdir / "scorecard.md")
    Path(out).write_text(scorecard.render(results))

    print(f"\ndone in {round(time.time() - started, 1)}s", file=sys.stderr)
    print(f"results: {results_path}", file=sys.stderr)
    print(f"scorecard: {out}", file=sys.stderr)
    print("\n" + scorecard.render(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
