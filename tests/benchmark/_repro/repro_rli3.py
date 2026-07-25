"""One-off repro: dispatch ONLY RLI-3 against a repo already seeded with
RLI-1+RLI-2's real (correct) code, to capture fresh agent.log/review.log for
the 2026-07-04 merged-but-wrong incident (RLI-3 reached done/APPROVE without
rate_limiter.py ever being touched). Copies logs out before any merge cleanup
could remove them, regardless of outcome.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH))
PIPELINE_REPO = BENCH.parents[1]
if str(PIPELINE_REPO) not in sys.path:
    sys.path.insert(0, str(PIPELINE_REPO))

import harness

SEED_IMPL = Path("/tmp/seed_rate_limiter.py").read_text()
SEED_TEST = Path("/tmp/seed_test_rate_limiter.py").read_text()

TASK_DIR = BENCH / "tasks" / "ratelimiter_inspect"
ACCEPTANCE_SOURCE = (TASK_DIR / "acceptance.py").read_text()
GROUNDTRUTH_SOURCE = (TASK_DIR / "groundtruth.py").read_text()
RLI3_STORY = json.loads((TASK_DIR / "decomposed_plan.stories.json").read_text())[2]
assert RLI3_STORY["key"] == "RLI-3"


def main() -> int:
    cell = BENCH / "_repro" / "cell"
    if cell.exists():
        shutil.rmtree(cell)
    repo = cell / "repo"
    origin = cell / "origin.git"
    plans = cell / "plans"
    worktrees = cell / "worktrees"
    for d in (repo, origin, plans, worktrees):
        d.mkdir(parents=True)

    harness._sh(["git", "init", "-q", "-b", "master", "."], repo)
    harness._sh(["git", "config", "user.email", "bench@local"], repo)
    harness._sh(["git", "config", "user.name", "bench"], repo)
    (repo / "pyproject.toml").write_text('[project]\nname = "bench-task"\nversion = "0.0.0"\n')
    (repo / "README.md").write_text("# benchmark task workspace\n")
    (repo / ".venv").symlink_to(PIPELINE_REPO / ".venv")
    (repo / ".gitignore").write_text(".venv/\n__pycache__/\n")
    (repo / "rate_limiter.py").write_text(SEED_IMPL)
    (repo / "test_rate_limiter.py").write_text(SEED_TEST)
    harness._sh(["git", "add", "-A"], repo)
    harness._sh(["git", "commit", "-qm", "init (seeded with real RLI-1+RLI-2 code)"], repo)

    harness._sh(["git", "init", "--bare", "-q", "-b", "master", "."], origin)
    harness._sh(["git", "remote", "add", "origin", str(origin)], repo)
    harness._sh(["git", "push", "-q", "-u", "origin", "master"], repo)

    os.environ["PLAN_DIR"] = str(plans)
    os.environ["WORKTREE_ROOT"] = str(worktrees)
    os.environ["REPO_ROOT"] = str(repo)
    os.environ["PIPELINE_AUTONOMY"] = "full"
    os.environ["PIPELINE_RISK_THRESHOLD"] = "low"
    os.environ["PIPELINE_MAX_CONCURRENT_AGENTS"] = "1"
    os.environ["PIPELINE_BACKEND_REVIEW"] = "local"
    os.environ["PIPELINE_REWORK_MAX_ATTEMPTS_ORACLE"] = "3"
    os.environ["PIPELINE_BACKEND_DISPATCH"] = "local"
    os.environ["PIPELINE_LOCAL_ENDPOINT"] = "http://localhost:11434"
    os.environ["PIPELINE_LOCAL_NUM_CTX"] = "32768"
    os.environ["PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS"] = "900"
    os.environ["PIPELINE_LOCAL_TEMPERATURE"] = "0.3"
    os.environ["PIPELINE_LOCAL_MAX_STEPS"] = "60"
    os.environ["PIPELINE_LOCAL_MODEL_DEFAULT"] = "gpt-oss:20b"
    for k in ("PIPELINE_LOCAL_MODEL_SONNET", "PIPELINE_LOCAL_MODEL_OPUS", "PIPELINE_LOCAL_MODEL_HAIKU"):
        os.environ.pop(k, None)

    import pipeline_mcp_server as p
    harness.install_merge_stubs(p, repo)

    # Override the merge stub to NOT remove the worktree on success, so
    # agent.log/review.log survive for inspection regardless of outcome
    # (the whole point of this repro).
    def _merge_pr_stub_keep_worktree(worktree, story_key):
        branch = f"agent/{story_key.lower()}"
        harness._sh(["git", "merge", "--squash", branch], repo)
        harness._sh(["git", "commit", "-qm", f"{story_key}: squash-merge {branch}"], repo)
        harness._sh(["git", "push", "-q", "origin", "master"], repo)
        return "merged (stub, worktree kept)"

    p._merge_pr = _merge_pr_stub_keep_worktree

    story = dict(RLI3_STORY)
    story["dependencies"] = []  # RLI-1/RLI-2 are already baked into the seed
    story["acceptance"] = [{"path": "test_acceptance.py", "source": ACCEPTANCE_SOURCE}]
    plan = harness.build_plan_from_stories(repo, "repro: RLI-3 only", [story])

    plan_name = "repro_rli3"
    p.save_plan(plan_name, json.dumps(plan))
    p.ingest_plan(plan_name)

    deadline = time.time() + 3600
    harness.drive(p, plan_name, "RLI-3", deadline, tick_interval=10.0)

    manifest = json.loads((plans / f"{plan_name}.manifest.json").read_text())
    story_final = manifest["stories"]["RLI-3"]
    print(json.dumps({"status": story_final["status"],
                      "review_verdict": story_final.get("review_verdict"),
                      "worktree": story_final.get("worktree")}, indent=2))

    # Copy logs out NOW, before any merge cleanup removes the worktree.
    wt = Path(story_final.get("worktree", ""))
    logs_out = BENCH / "_repro" / "logs"
    logs_out.mkdir(parents=True, exist_ok=True)
    if wt.is_dir():
        for name in ("agent.log", "review.log"):
            src = wt / name
            if src.exists():
                shutil.copy(src, logs_out / name)
                print(f"copied {name} ({src.stat().st_size} bytes)")
    else:
        print("worktree already gone (merged) -- checking repo for final state")

    gt = harness.run_groundtruth(repo, "rate_limiter.py", GROUNDTRUTH_SOURCE, cell / "_gt")
    print("groundtruth:", gt.get("passed"), gt.get("reason", ""))
    (repo / "rate_limiter.py").read_text()  # sanity
    print("--- final rate_limiter.py in repo ---")
    print((repo / "rate_limiter.py").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
