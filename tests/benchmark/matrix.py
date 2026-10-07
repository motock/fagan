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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if str(Path(__file__).resolve().parents[2]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scorecard
from models import MODELS

from app.role_registry import load_registry

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
        check=False, capture_output=True, text=True,
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


def preflight_models(models: list[str], registry: dict | None = None) -> list[str]:
    """Reasons the grid cannot run as configured; empty means it can.

    Every arm pins the roles a cell can invoke in its plan role_config (see
    models._ollama_role_config), and resolve_role rejects a pin whose model
    is not declared under providers.<provider>.models in the registry
    load_registry() actually reads. An isolated bench clone loses
    model_registry.local.json -- and load_registry() returns {} for a missing
    file rather than raising -- so every pin resolves against nothing, every
    cell parks with no dispatched model, and the grid burns all of its cells
    discovering that. Check it once here instead.
    """
    if registry is None:
        registry = load_registry()
    providers = registry.get("providers", {})
    problems: list[str] = []
    for name in models:
        for role, pin in (MODELS[name].get("role_config") or {}).items():
            provider = str(pin.get("provider", "")).strip().lower()
            declared = providers.get(provider, {}).get("models", {})
            if pin.get("model") not in declared:
                problems.append(
                    f"model {name!r}: role {role!r} pins "
                    f"{provider}/{pin.get('model')!r}, which is not declared "
                    f"under providers.{provider}.models"
                )
    return problems


def _needs_claude(model_cfg: dict) -> bool:
    """True when a cell for this arm can invoke a Claude agent.

    A mock arm never touches the network. When the arm pins its roles in
    role_config that is authoritative -- the harness plan-pins every role from
    it and it outranks the env -- so the env is ignored. Legacy env-only arms
    fall back to the env: dispatch claude, or review claude (harness's
    _set_review_backend_env defaults review to claude when the key is absent).
    """
    if model_cfg.get("mock"):
        return False
    role_config = model_cfg.get("role_config")
    if role_config is not None:
        return any(str(pin.get("provider", "")).strip().lower() == "claude"
                   for pin in role_config.values())
    env = model_cfg.get("env") or {}
    if env.get("PIPELINE_BACKEND_DISPATCH") == "claude":
        return True
    return env.get("PIPELINE_BACKEND_REVIEW", "claude") == "claude"


# Substrings that mean "Claude is unusable right now", not "this cell failed":
# the CLI reports them on stdout with is_error false and exit code 0.
_CREDIT_MARKERS = ("out_of_credits", "rate limit", "rate_limit", "usage limit")


def _probe_verdict(returncode: int, stdout: str) -> tuple[bool, str]:
    """Pure verdict for a claude CLI probe: (available, reason)."""
    if returncode != 0:
        return False, f"claude probe exited {returncode}"
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return False, "claude probe did not return JSON"
    if not isinstance(payload, dict):
        return False, "claude probe did not return a JSON object"
    if payload.get("is_error"):
        return False, "claude probe reported is_error"
    lowered = stdout.lower()
    for marker in _CREDIT_MARKERS:
        if marker in lowered:
            return False, f"claude probe reported {marker}"
    return True, ""


def claude_available() -> tuple[bool, str]:
    """Probe the claude CLI once: (available, reason).

    A missing binary or a hung CLI is an infra failure, not a model verdict,
    so both map to unavailable rather than raising.
    """
    try:
        proc = subprocess.run(
            ["claude", "-p", "Reply with the single word OK",
             "--output-format", "json"],
            timeout=120, check=False, capture_output=True, text=True,
        )
    except FileNotFoundError:
        return False, "claude CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "claude probe timed out"
    return _probe_verdict(proc.returncode, proc.stdout)


def _is_environment_failure(result: dict) -> bool:
    """True when a cell failed before any agent ran.

    Such a cell carries no model verdict to attribute the failure to, so
    every later cell would fail the same way. A cell that dispatched and then
    parked is a normal bench outcome -- the model simply failed the task --
    and must not stop the grid. An infra_skipped cell never ran an agent by
    design (Claude was unavailable), so it must not trip the abort-on-first-
    cell rule either: the grid keeps recording every skipped cell so a later
    --resume re-runs them.
    """
    if result.get("final_status") == "infra_skipped":
        return False
    if result.get("final_status") == "harness_error":
        return True
    return not result.get("dispatched_model")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", nargs="+", default=None,
                    help="task names (default: all under tasks/)")
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--workdir", default=str(BENCH / "_runs"))
    ap.add_argument("--timeout", type=int, default=3600,
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

    problems = preflight_models(args.models)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"refusing to run {len(cells_spec)} cells with unresolvable "
              f"role pins -- fix the registry and retry.", file=sys.stderr)
        return 2

    results: list[dict] = []
    started = time.time()

    # Latched once Claude is found unavailable. Credits can run out mid-run,
    # so the probe runs per Claude-using cell, but once it fails every later
    # Claude cell is skipped without another probe. _go may run in threads.
    claude_latch: dict[str, str] = {}
    latch_lock = threading.Lock()

    def _go(spec):
        t, m, i = spec
        cell_dir = workdir / f"{t}__{m}__t{i}"
        result_path = cell_dir / "result.json"
        if args.resume and result_path.exists():
            r = json.loads(result_path.read_text())
            print(f"  [{t}/{m}/t{i}] SKIP (existing) {r['final_status']}", file=sys.stderr)
            return r
        if _needs_claude(MODELS[m]):
            with latch_lock:
                reason = claude_latch.get("reason")
            if reason is None:
                ok, probe_reason = claude_available()
                if not ok:
                    with latch_lock:
                        if claude_latch.get("reason") is None:
                            claude_latch["reason"] = probe_reason
                            print(f"  claude unavailable: {probe_reason}",
                                  file=sys.stderr)
                        reason = claude_latch["reason"]
            if reason is not None:
                # No run, no result.json: a later --resume re-runs this cell.
                print(f"  [{t}/{m}/t{i}] infra_skipped (claude unavailable)",
                      file=sys.stderr)
                return {
                    "task": t, "model": m, "trial": i,
                    "final_status": "infra_skipped", "merged": False,
                    "groundtruth_passed": False, "groundtruth_ran": False,
                    "timed_out": False, "elapsed_s": 0, "ticks": 0,
                    "error": reason,
                }
        t0 = time.time()
        r = run_cell(t, m, i, workdir, args.timeout, args.tick)
        print(f"  [{t}/{m}/t{i}] {r['final_status']:13} "
              f"gt={r.get('groundtruth_passed')!s:5} "
              f"({round(time.time() - t0, 1)}s)", file=sys.stderr)
        return r

    if args.jobs <= 1:
        first_ran = False
        for spec in cells_spec:
            cached = args.resume and (workdir / f"{spec[0]}__{spec[1]}__t{spec[2]}"
                                      / "result.json").exists()
            r = _go(spec)
            results.append(r)
            if not first_ran and not cached:
                first_ran = True
                if _is_environment_failure(r):
                    print(f"  [{spec[0]}/{spec[1]}/t{spec[2]}] first cell failed "
                          f"before any agent ran; aborting rather than running "
                          f"{len(cells_spec) - 1} more cells that would fail the "
                          f"same way.", file=sys.stderr)
                    return 2
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
