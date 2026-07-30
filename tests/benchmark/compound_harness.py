"""Compound-task harness: drives a multi-story PLAN (not a single story)
through the real pipeline, for validating whether product-analyst's Story
Sizing guidance produces stories that complete more reliably than an
equivalent bundled/monolithic story. See PRODUCT_ANALYST_VALIDATION_PLAN.md.

Deliberately a separate script from harness.py rather than a generalization
of it: harness.py's single-story contract (build_plan/drive/main) is covered
by its own tests and used by every existing task, so this reuses its shared
pieces (setup_workspace, install_merge_stubs, run_groundtruth, MockBackend,
drive_plan, build_plan_from_stories) without touching that contract.

Two conditions, same requirement, same task's acceptance/groundtruth:
    --condition M   one hand-bundled monolithic story (tasks/<name>/spec.json)
    --condition D   a cached product-analyst decomposition
                     (tasks/<name>/decomposed_plan.stories.json)

Usage:
    python compound_harness.py --task ratelimiter_inspect --condition M --model mock
    python compound_harness.py --task ratelimiter_inspect --condition D --model gptoss_temp03
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import harness
from harness import BENCH_DIR, TASKS_DIR


def load_compound_task(name: str) -> dict:
    """Like harness.load_task, but for a task with both a monolithic spec.json
    (Condition M) and an optional cached decomposition (Condition D)."""
    d = TASKS_DIR / name
    spec = json.loads((d / "spec.json").read_text())
    spec["acceptance_source"] = (d / "acceptance.py").read_text()
    spec["groundtruth_source"] = (d / "groundtruth.py").read_text()
    decomposed_path = d / "decomposed_plan.stories.json"
    spec["decomposed_stories"] = (
        json.loads(decomposed_path.read_text()) if decomposed_path.exists() else None
    )
    return spec


def build_condition_stories(task: dict, condition: str) -> list[dict]:
    """Return the story list for the given condition, with the task's shared
    acceptance oracle attached only to the terminal story of the chain (the
    one nothing else depends on) -- by the time it's dispatched, master
    already contains every prerequisite story's merged code, so its worktree
    is the right place to grade the combined feature."""
    acceptance = [{"path": "test_acceptance.py", "source": task["acceptance_source"]}]
    key_prefix = task["name"].upper().replace("_", "-")

    if condition == "M":
        return [{
            "key": f"{key_prefix}-M",
            "summary": task["summary"],
            "agent_instructions": task["agent_instructions"],
            "persona": task.get("persona", "software-engineer"),
            "model": task.get("model", "sonnet"),
            "risk": task.get("risk", "low"),
            "dependencies": [],
            "acceptance": acceptance,
        }]

    if condition == "D":
        stories = task["decomposed_stories"]
        if not stories:
            raise SystemExit(
                f"no cached decomposition at tasks/{task['name']}/decomposed_plan.stories.json"
                " -- generate one (via the product-analyst agent) first"
            )
        depended_on = {dep for s in stories for dep in s.get("dependencies", [])}
        sinks = [s for s in stories if s["key"] not in depended_on]
        if len(sinks) != 1:
            raise SystemExit(
                f"expected exactly one terminal story in the decomposed plan, "
                f"found {len(sinks)}: {[s['key'] for s in sinks]}"
            )
        sinks[0]["acceptance"] = acceptance
        return stories

    raise SystemExit(f"unknown condition {condition!r}; must be D or M")


def _final_status(story_statuses: dict[str, str]) -> str:
    values = set(story_statuses.values())
    if values == {"done"}:
        return "done"
    if values <= harness.TERMINAL:
        return "parked" if "parked" in values else "failed"
    return "incomplete"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--condition", required=True, choices=["D", "M"])
    ap.add_argument("--model", required=True)
    ap.add_argument("--trial", type=int, default=0)
    ap.add_argument("--workdir", default=str(BENCH_DIR / "_runs"))
    ap.add_argument("--timeout", type=int, default=3600,
                    help="wall-clock budget for the WHOLE plan, seconds")
    ap.add_argument("--tick", type=float, default=10.0)
    ap.add_argument("--max-defer-extension", type=int, default=14400)
    ap.add_argument(
        "--decompose", choices=["off", "cloud", "local"], default="off",
        help="GUIDED_DECOMPOSITION_PLAN.md: sets PIPELINE_DECOMPOSE for this "
             "run - off (default, matches condition M as-is), cloud (the "
             "G-cloud arm: a Claude planner checklist), local (the H2 "
             "ablation: the same local model plans for itself).",
    )
    ap.add_argument(
        "--decompose-scratchpad", choices=["on", "off"], default="on",
        help="Sets PIPELINE_DECOMPOSE_SCRATCHPAD. 'off' is the H3 ablation "
             "(the G-cloud-noscratch arm: checklist only, no persistent "
             "scratchpad instruction). Ignored when --decompose is off.",
    )
    args = ap.parse_args()

    from models import MODELS
    if args.model not in MODELS:
        print(f"unknown model {args.model!r}; known: {list(MODELS)}", file=sys.stderr)
        return 2
    model_cfg = MODELS[args.model]
    task = load_compound_task(args.task)
    stories = build_condition_stories(task, args.condition)
    story_keys = [s["key"] for s in stories]

    cell = (Path(args.workdir).resolve()
            / f"{args.task}__{args.condition}__{args.model}__t{args.trial}")
    paths = harness.setup_workspace(cell, task)
    repo = paths["repo"]

    os.environ["PLAN_DIR"] = str(paths["plans"])
    os.environ["WORKTREE_ROOT"] = str(paths["worktrees"])
    os.environ["REPO_ROOT"] = str(repo)
    os.environ["PIPELINE_AUTONOMY"] = "full"
    os.environ["PIPELINE_RISK_THRESHOLD"] = "low"
    os.environ["PIPELINE_MAX_CONCURRENT_AGENTS"] = "1"
    os.environ["PIPELINE_DECOMPOSE"] = args.decompose
    os.environ["PIPELINE_DECOMPOSE_SCRATCHPAD"] = args.decompose_scratchpad
    harness._set_review_backend_env()
    for k in ("PIPELINE_LOCAL_MODEL_SONNET", "PIPELINE_LOCAL_MODEL_OPUS",
              "PIPELINE_LOCAL_MODEL_HAIKU"):
        os.environ.pop(k, None)
    os.environ.update(model_cfg["env"])

    from app import pipeline_mcp_server as p
    harness.install_merge_stubs(p, repo)

    mock = None
    if model_cfg.get("mock"):
        from app import backend as _backend
        mock = harness.MockBackend(task)

        def _get_backend(role, name=None):
            return mock

        _backend.get_backend = _get_backend
        p.backend.get_backend = _get_backend
        p._run_reviewer = lambda worktree, branch: "VERDICT: APPROVE"

    plan_name = f"bench_{args.task}_{args.condition}_{args.model}_t{args.trial}"
    plan = harness.build_plan_from_stories(
        repo, f"benchmark: {task['name']} (condition {args.condition})", stories
    )

    p.save_plan(plan_name, json.dumps(plan))
    p.ingest_plan(plan_name)

    started = time.time()
    deadline = started + args.timeout
    ticks = harness.drive_plan(p, plan_name, story_keys, deadline, args.tick,
                               max_defer_extension=args.max_defer_extension)
    elapsed = round(time.time() - started, 1)

    manifest = json.loads((paths["plans"] / f"{plan_name}.manifest.json").read_text())
    story_statuses = {k: manifest["stories"][k]["status"] for k in story_keys}
    final_status = _final_status(story_statuses)
    all_done = final_status == "done"

    # Grade against master once every story is done; otherwise fall back to
    # the deepest story's surviving worktree (the fullest partial picture,
    # since a dependent story branches after its prerequisites already
    # merged), or master itself if no worktree survived.
    if all_done:
        gt_src, gt_where = repo, "master"
    else:
        gt_src, gt_where = repo, "master"
        for key in reversed(story_keys):
            wt = Path(manifest["stories"][key].get("worktree", ""))
            if wt.is_dir():
                gt_src, gt_where = wt, "worktree"
                break
    gt = harness.run_groundtruth(gt_src, task["impl_file"], task["groundtruth_source"],
                                 cell / "_gt")

    result = {
        "task": args.task,
        "condition": args.condition,
        "model": args.model,
        "trial": args.trial,
        "decompose": args.decompose,
        "decompose_scratchpad": args.decompose_scratchpad,
        "story_count": len(stories),
        "story_statuses": story_statuses,
        "final_status": final_status,
        "merged": all_done,
        "groundtruth_where": gt_where,
        "groundtruth_passed": gt.get("passed", False),
        "groundtruth_ran": gt.get("ran", False),
        "elapsed_s": elapsed,
        "ticks": len(ticks),
        "timed_out": final_status == "incomplete",
        "tick_log": ticks,
        "groundtruth_tail": gt.get("tail", gt.get("reason", "")),
        # Only meaningful under --model mock (GUIDED_DECOMPOSITION_PLAN.md's
        # offline self-test): the number of complete() calls the planner
        # made against MockBackend, proving the planner path was actually
        # exercised rather than silently skipped. None for a real model,
        # since a real backend has no equivalent call-count to surface here.
        "decompose_planner_calls": len(mock.complete_calls) if mock else None,
    }
    (cell / "result.json").write_text(json.dumps(result, indent=2))

    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("tick_log", "groundtruth_tail")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
