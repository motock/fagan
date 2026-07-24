"""
Utility functions for the real‑repo integration harness.

This module implements two small, independently testable helpers used by the
real‑repo benchmark driver.  The implementation closely mirrors the logic in
``harness.setup_workspace`` and ``harness.run_groundtruth`` but is adapted to
work with a full clone of the pipeline repository instead of a synthetic
scaffold.

The functions are intentionally minimal – they perform only what the tests
exercise, without any additional side‑effects.  They rely on the constants
``PIPELINE_REPO`` and ``VENV_PY`` defined in ``tests/benchmark/harness.py``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Import constants from harness – use the same import style as compound_harness.py
from harness import PIPELINE_REPO, VENV_PY, drive
def setup_real_repo_workspace(cell: Path, base_commit: str) -> dict[str, Path]:
    """Create a throwaway workspace that contains a full clone of the pipeline repo.

    Parameters
    ----------
    cell:
        The directory in which to create the workspace.  Any existing contents are
        removed before creation.
    base_commit:
        A commit SHA (or ref) that the cloned repository will be checked out at.

    Returns
    -------
    dict[str, Path]
        Mapping with keys ``repo``, ``origin``, ``plans`` and ``worktrees``.  The
        values are :class:`~pathlib.Path` objects pointing to the corresponding
        directories inside *cell*.
    """
    # Ensure a clean cell directory
    if cell.exists():
        shutil.rmtree(cell)

    repo = cell / "repo"
    origin = cell / "origin.git"
    plans = cell / "plans"
    worktrees = cell / "worktrees"

    for d in (repo, origin, plans, worktrees):
        d.mkdir(parents=True)

    # Clone the pipeline repo locally – ``--local`` keeps it a copy of the same
    # working tree without network traffic.
        subprocess.run(["git", "clone", "--local", str(PIPELINE_REPO), str(repo)], check=True, capture_output=True)
    # Pin to the requested commit.  This detaches HEAD.
    subprocess.run(["git", "-C", str(repo), "checkout", base_commit], check=True, capture_output=True)

    # Create a real ``master`` branch pointing at that commit so pushes work.
    subprocess.run(["git", "-C", str(repo), "branch", "--force", "master", "HEAD"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "master"], check=True, capture_output=True)

    # Initialise the bare origin in the sibling directory.  The ``-q`` flag keeps
    # output quiet; ``-b master`` ensures the remote has a default branch.
    subprocess.run(["git", "init", "--bare", "-q", "-b", "master", "."], cwd=str(origin), check=True, capture_output=True)

    # Update the existing ``origin`` remote to point at the new bare repo.
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", str(origin)], check=True, capture_output=True)
    # Symlink the pipeline's virtualenv into the clone so pytest resolves correctly.
    (repo / ".venv").symlink_to(PIPELINE_REPO / ".venv")
    subprocess.run(["git", "-C", str(repo), "push", "-q", "-u", "origin", "master"], check=True, capture_output=True)
    return {"repo": repo, "origin": origin, "plans": plans, "worktrees": worktrees}
def run_groundtruth_in_place(repo: Path, groundtruth_source: str, groundtruth_name: str = "test_groundtruth_review_story_lock_guard.py") -> dict:
    """Run a ground‑truth test file inside *repo* and clean up.

    The function writes ``groundtruth_source`` to ``repo / groundtruth_name``,
    executes it with the pipeline virtualenv's pytest, then removes the file
    regardless of success or failure.  It returns a dict compatible with
    :func:`harness.run_groundtruth`.
    """
    test_file = repo / groundtruth_name
    test_file.write_text(groundtruth_source)

    try:
        result = subprocess.run(
            [str(VENV_PY), "-m", "pytest", groundtruth_name, "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        # Ensure the throwaway file is removed even if pytest crashes.
        test_file.unlink(missing_ok=True)

    tail = (result.stdout + result.stderr)[-700:]
    return {"ran": True, "passed": result.returncode == 0, "tail": tail}


def main() -> int:
    import argparse
    import json
    import os
    import sys
    import time
    from pathlib import Path

    # Import harness helpers
    from harness import build_plan_from_stories, install_merge_stubs, _set_review_backend_env

    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="review_story_lock_guard")
    parser.add_argument("--model", required=True)
    parser.add_argument("--trial", type=int, default=0)
    workdir_default = str(Path(__file__).resolve().parent / "_runs")
    parser.add_argument("--workdir", default=workdir_default)
    parser.add_argument("--timeout", type=int, default=10800)
    parser.add_argument("--tick", type=float, default=10.0)
    parser.add_argument("--max-defer-extension", type=int, default=14400)

    args = parser.parse_args()

    # Unknown model check
    try:
        from models import MODELS
    except Exception:
        print("Could not load models", file=sys.stderr)
        return 2

    if args.model not in MODELS:
        print(f"Unknown model: {args.model}", file=sys.stderr)
        return 2

    task_dir = Path(__file__).resolve().parent / "tasks" / args.task
    spec_path = task_dir / "spec.json"
    groundtruth_path = task_dir / "groundtruth.py"

    if not spec_path.is_file() or not groundtruth_path.is_file():
        print(f"Task {args.task} does not exist", file=sys.stderr)
        return 2

    with open(spec_path) as f:
        task = json.loads(f.read())


    try:
        base_commit = subprocess.run(
            ["git", "-C", str(PIPELINE_REPO), "rev-parse", "HEAD"],
            text=True
        ).stdout.strip()
    except Exception:
        base_commit = "HEAD"
    cell = Path(args.workdir).resolve() / f"{args.task}__{args.model}__t{args.trial}"
    paths = setup_real_repo_workspace(cell, base_commit)
    os.environ["PIPELINE_AUTONOMY"] = "full"
    os.environ["PIPELINE_RISK_THRESHOLD"] = "low"
    os.environ["PIPELINE_MAX_CONCURRENT_AGENTS"] = "1"
    _set_review_backend_env()
    # Clear any explicit review backend overrides
    for key in ["PIPELINE_LOCAL_MODEL_SONNET", "PIPELINE_LOCAL_MODEL_OPUS", "PIPELINE_LOCAL_MODEL_HAIKU"]:
        os.environ.pop(key, None)
    os.environ.update(MODELS[args.model]["env"])

    import pipeline_mcp_server as p
    install_merge_stubs(p, paths["repo"])

    story_key = task["name"].upper().replace("_", "-")
    stories = [
        {
            "key": story_key,
            "summary": task["summary"],
            "agent_instructions": task["agent_instructions"],
            "persona": task["persona"],
            "model": task["model"],
            "risk": task["risk"],
            "dependencies": [],
            "acceptance": []
        }
    ]
    plan = build_plan_from_stories(paths["repo"], task["summary"], stories)
    plan_name = f"bench_{args.task}_{args.model}_t{args.trial}"
    p.save_plan(plan_name, json.dumps(plan))
    p.ingest_plan(plan_name)

    started = time.time()
    deadline = started + args.timeout
    ticks = drive(p, plan_name, story_key, deadline, args.tick, max_defer_extension=args.max_defer_extension)
    elapsed_s = round(time.time() - started, 1)

    manifest_path = paths["plans"] / f"{plan_name}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    story = manifest["stories"][story_key]
    final_status = story["status"]

    if final_status == "done":
        gt_src = paths["repo"]
    else:
        wt = Path(story.get("worktree", ""))
        gt_src = wt if wt.is_dir() else paths["repo"]

    gt = run_groundtruth_in_place(gt_src, task["groundtruth_source"])

    rework_cycles = story.get("rework_attempts", 0)

    infra_failures = 0
    journal_path = paths["plans"] / f"{plan_name}.{story_key}.journal.json"
    if journal_path.is_file():
        for entry in json.loads(journal_path.read_text()):
            if entry.get("step") == "infra_failure":
                infra_failures += 1

    result = {
        "final_status": final_status,
        "review_verdict": story.get("review_verdict"),
        "merged": final_status == "done",
        "dispatched_model": story.get("dispatched_model"),
        "elapsed_s": elapsed_s,
        "ticks": len(ticks),
        "groundtruth_passed": gt.get("passed"),
        "groundtruth_tail": gt.get("tail"),
        "rework_cycles": rework_cycles,
        "infra_failures": infra_failures,
        "task": args.task,
        "model": args.model,
        "trial": args.trial
    }

    cell.mkdir(parents=True, exist_ok=True)
    (cell / "result.json").write_text(json.dumps(result))
    print(json.dumps(result))
    return 0

