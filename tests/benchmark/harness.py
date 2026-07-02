"""Single-cell pipeline benchmark runner: one task x one model x one trial.

Drives a task through the REAL pipeline end-to-end (ingest -> dispatch ->
check_story_status -> review -> advance/merge) inside a fully isolated, throwaway
workspace, then grades the merged result against an independent ground-truth test
the implementing model never saw.

Isolation
---------
Each cell gets its own directory tree:

    <workdir>/<task>__<model>__t<trial>/
        repo/         the throwaway git repo (the plan's repo_root)
        origin.git/   a LOCAL bare remote, so every git push/rebase/worktree
                      operation the pipeline performs is REAL but hermetic
        plans/        PLAN_DIR for this cell
        worktrees/    WORKTREE_ROOT for this cell

PLAN_DIR / WORKTREE_ROOT / REPO_ROOT are read by pipeline_mcp_server at import
time, so they are set in the environment BEFORE the module is imported (main()
imports it lazily for exactly this reason). Nothing here ever touches the user's
real ~/.claude/plans or ~/.claude/worktrees.

GitHub boundary
---------------
review_story -> _open_pr -> _merge_pr and the CI gate require the `gh` CLI and a
real GitHub remote. The merge plumbing is identical across models, so coupling
every trial to GitHub would add nothing but flakiness. Instead we keep ALL `git`
operations real (against the local bare origin) and monkeypatch only the three
`gh`-calling seams so the full state machine still runs to `done` hermetically:

    _ci_status   -> always "pass"
    _open_pr     -> real `git push`, returns a file:// URL (no `gh pr create`)
    _merge_pr    -> real local squash-merge into master + push (no `gh pr merge`)

Usage
-----
    python harness.py --task token_bucket --model devstral
    python harness.py --task lru_cache --model mock      # offline self-test

Writes <cell>/result.json and prints a one-line summary.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
PIPELINE_REPO = BENCH_DIR.parents[1]
TASKS_DIR = BENCH_DIR / "tasks"
VENV_PY = PIPELINE_REPO / ".venv" / "bin" / "python"

# pipeline_mcp_server.py and backend.py live at the pipeline repo root.
if str(PIPELINE_REPO) not in sys.path:
    sys.path.insert(0, str(PIPELINE_REPO))

def _set_review_backend_env() -> None:
    """Default the review gate to the cloud Claude reviewer, without
    clobbering an explicit override from the invoking shell (e.g. `local`,
    to trade review quality for zero Claude usage on a given run)."""
    os.environ.setdefault("PIPELINE_BACKEND_REVIEW", "claude")


# Terminal manifest statuses for a single story: the drive loop stops here.
TERMINAL = {"done", "failed", "parked"}

# Correct reference implementations used ONLY by the offline `mock` backend to
# self-test the harness plumbing. Real model runs never see these; they are not
# copied into any worktree except by MockBackend. They double as executable
# documentation of each task's intended behavior.
_MOCK_IMPLS: dict[str, str] = {
    "token_bucket": '''
class TokenBucket:
    def __init__(self, capacity, refill_rate, now=0.0):
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be > 0")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.tokens = float(capacity)
        self.last = float(now)

    def allow(self, tokens=1.0, now=None):
        if tokens < 0:
            raise ValueError("tokens must be >= 0")
        if now is None:
            now = self.last
        elapsed = now - self.last
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last = now
        if tokens <= self.tokens:
            self.tokens -= tokens
            return True
        return False
''',
    "lru_cache": '''
from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._d = OrderedDict()

    def get(self, key):
        if key not in self._d:
            return None
        self._d.move_to_end(key)
        return self._d[key]

    def put(self, key, value):
        if key in self._d:
            self._d[key] = value
            self._d.move_to_end(key)
            return
        self._d[key] = value
        if len(self._d) > self.capacity:
            self._d.popitem(last=False)

    @property
    def size(self):
        return len(self._d)
''',
    "cron_field": '''
def _parse_token(tok, lo, hi):
    if tok == "":
        raise ValueError("empty token")
    step = 1
    if "/" in tok:
        parts = tok.split("/")
        if len(parts) != 2:
            raise ValueError("bad step")
        tok, steptok = parts
        step = int(steptok)
        if step <= 0:
            raise ValueError("step must be > 0")
    if tok == "*":
        start, end = lo, hi
    elif "-" in tok[1:]:
        a, b = tok.split("-")
        start, end = int(a), int(b)
    else:
        v = int(tok)
        start = end = v
    if start > end:
        raise ValueError("reversed range")
    if start < lo or end > hi:
        raise ValueError("out of range")
    return set(range(start, end + 1, step))


def match_field(field, lo, hi):
    if lo > hi:
        raise ValueError("lo > hi")
    out = set()
    for tok in field.split(","):
        out |= _parse_token(tok, lo, hi)
    return out
''',
    "retry_backoff": '''
def backoff_delays(base, factor, cap, attempts):
    if base <= 0 or factor < 1 or cap < base or attempts < 0:
        raise ValueError("bad args")
    return [min(cap, base * factor ** i) for i in range(attempts)]


def should_retry(status_code, attempt, max_attempts):
    if max_attempts < 0 or attempt < 0:
        raise ValueError("bad args")
    retryable = status_code == 429 or 500 <= status_code <= 599
    return retryable and attempt < max_attempts
''',
    "interval_merge": '''
def merge(intervals):
    for s, e in intervals:
        if s > e:
            raise ValueError("start > end")
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out
''',
}


def _sh(argv, cwd, check=True):
    return subprocess.run(argv, cwd=str(cwd), check=check,
                          capture_output=True, text=True)


def load_task(name: str) -> dict:
    spec = json.loads((TASKS_DIR / name / "spec.json").read_text())
    spec["acceptance_source"] = (TASKS_DIR / name / "acceptance.py").read_text()
    spec["groundtruth_source"] = (TASKS_DIR / name / "groundtruth.py").read_text()
    return spec


def setup_workspace(cell: Path) -> dict[str, Path]:
    """Create the isolated repo + local bare origin + plan/worktree dirs."""
    if cell.exists():
        shutil.rmtree(cell)
    repo = cell / "repo"
    origin = cell / "origin.git"
    plans = cell / "plans"
    worktrees = cell / "worktrees"
    for d in (repo, origin, plans, worktrees):
        d.mkdir(parents=True)

    _sh(["git", "init", "-q", "-b", "master", "."], repo)
    _sh(["git", "config", "user.email", "bench@local"], repo)
    _sh(["git", "config", "user.name", "bench"], repo)
    # pyproject.toml makes detect_test_command() pick pytest; the .venv symlink
    # makes _venv_python_for() resolve to the pipeline venv (with pytest + deps).
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "bench-task"\nversion = "0.0.0"\n'
    )
    (repo / "README.md").write_text("# benchmark task workspace\n")
    (repo / ".venv").symlink_to(PIPELINE_REPO / ".venv")
    (repo / ".gitignore").write_text(".venv/\n__pycache__/\n")
    _sh(["git", "add", "-A"], repo)
    _sh(["git", "commit", "-qm", "init"], repo)

    _sh(["git", "init", "--bare", "-q", "-b", "master", "."], origin)
    _sh(["git", "remote", "add", "origin", str(origin)], repo)
    _sh(["git", "push", "-q", "-u", "origin", "master"], repo)
    return {"repo": repo, "origin": origin, "plans": plans, "worktrees": worktrees}


def build_plan(repo: Path, task: dict) -> dict:
    """One epic, one story carrying the acceptance fixture as a read-only oracle."""
    return {
        "repo_root": str(repo),
        "epics": [{
            "summary": f"benchmark: {task['name']}",
            "stories": [{
                "key": task["name"].upper().replace("_", "-"),
                "summary": task["summary"],
                "agent_instructions": task["agent_instructions"],
                "persona": task.get("persona", "software-engineer"),
                "model": task.get("model", "sonnet"),
                "risk": task.get("risk", "low"),
                "acceptance": [
                    {"path": "test_acceptance.py", "source": task["acceptance_source"]}
                ],
            }],
        }],
    }


def install_merge_stubs(p, repo: Path) -> None:
    """Replace the three gh-calling seams with hermetic git-only equivalents."""

    def _ci_status_stub(branch, *, timeout_s=None):
        return {"state": "pass", "error": ""}

    def _open_pr_stub(worktree, story_key, story):
        branch = f"agent/{story_key.lower()}"
        _sh(["git", "push", "-q", "-u", "origin", branch], worktree)
        return f"file://{repo}/pr/{branch}"

    def _merge_pr_stub(worktree, story_key):
        branch = f"agent/{story_key.lower()}"
        _sh(["git", "merge", "--squash", branch], repo)
        _sh(["git", "commit", "-qm", f"{story_key}: squash-merge {branch}"], repo)
        _sh(["git", "push", "-q", "origin", "master"], repo)
        _sh(["git", "worktree", "remove", "--force", worktree], repo, check=False)
        _sh(["git", "branch", "-D", branch], repo, check=False)
        return "merged (stub)"

    p._ci_status = _ci_status_stub
    p._open_pr = _open_pr_stub
    p._merge_pr = _merge_pr_stub


class MockBackend:
    """Offline backend for self-testing the harness with no model/network.

    dispatch() synchronously writes a correct reference implementation, commits
    it on the story branch, and returns a handle whose pid has already exited so
    the next check_story_status tick proceeds straight to the test gate.
    """

    def __init__(self, task: dict):
        self.task = task

    def dispatch(self, *, prompt, system, model, allowed_tools, cwd, log_path,
                 append=False, acceptance=None):
        from backend import AgentHandle  # local import; env already set
        cwd = Path(cwd)
        # BENCH_MOCK_IMPL_FILE lets the harness self-test inject a deliberately
        # wrong implementation to prove the independent ground-truth catches it.
        override = os.environ.get("BENCH_MOCK_IMPL_FILE")
        impl = Path(override).read_text() if override else _MOCK_IMPLS[self.task["name"]]
        (cwd / self.task["impl_file"]).write_text(impl.lstrip("\n"))
        Path(log_path).write_text("[mock] wrote reference implementation\n")
        _sh(["git", "add", "-A"], cwd)
        # --allow-empty: a resumed dispatch re-writes identical content, so a
        # plain commit would fail with "nothing to commit".
        _sh(["git", "commit", "-q", "--allow-empty", "-m", "mock: implement"], cwd)
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return AgentHandle(pid=proc.pid, model="mock")

    def resource_status(self):
        return {"ok": True, "reason": ""}


def drive(p, plan_name: str, story_key: str, deadline: float,
          tick_interval: float, max_defer_extension: float = 14400.0) -> list[dict]:
    """Tick advance_pipeline until the story is terminal or the deadline passes.

    A reviewer rate-limit (FM-H) is an infra event, not real elapsed work: while
    advance_pipeline reports the story as deferred on a given tick, the deadline
    is pushed out by one more tick_interval so the wall-clock budget isn't spent
    waiting out the reviewer's reset. Bounded by max_defer_extension so a
    permanently rate-limited reviewer still times out the cell eventually
    instead of hanging the benchmark forever.
    """
    ticks: list[dict] = []
    extension_used = 0.0
    while time.time() < deadline:
        summary = p.advance_pipeline(plan_name)
        manifest = json.loads(
            (Path(p.PLAN_DIR) / f"{plan_name}.manifest.json").read_text()
        )
        status = manifest["stories"][story_key]["status"]
        ticks.append({"t": round(time.time(), 1), "status": status,
                      "skipped": summary.get("skipped")})
        if status in TERMINAL:
            break
        if story_key in summary.get("review_deferred", []) and extension_used < max_defer_extension:
            deadline += tick_interval
            extension_used += tick_interval
        time.sleep(tick_interval)
    return ticks


def run_groundtruth(impl_src: Path, impl_file: str, groundtruth: str,
                    scratch: Path) -> dict:
    """Run the independent ground-truth suite against a copy of the impl file."""
    impl_path = impl_src / impl_file
    if not impl_path.exists():
        return {"ran": False, "passed": False, "reason": f"no {impl_file} at {impl_src}"}
    scratch.mkdir(parents=True, exist_ok=True)
    shutil.copy(impl_path, scratch / impl_file)
    (scratch / "test_groundtruth.py").write_text(groundtruth)
    r = subprocess.run(
        [str(VENV_PY), "-m", "pytest", "test_groundtruth.py", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=str(scratch), capture_output=True, text=True,
    )
    return {"ran": True, "passed": r.returncode == 0,
            "tail": (r.stdout + r.stderr)[-700:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--trial", type=int, default=0)
    ap.add_argument("--workdir", default=str(BENCH_DIR / "_runs"))
    ap.add_argument("--timeout", type=int, default=1800,
                    help="wall-clock budget for the cell, seconds")
    ap.add_argument("--tick", type=float, default=10.0,
                    help="seconds between advance_pipeline ticks")
    ap.add_argument("--max-defer-extension", type=int, default=14400,
                    help="max seconds the deadline may be extended for reviewer "
                         "rate-limit deferrals (FM-H), seconds")
    args = ap.parse_args()

    from models import MODELS
    if args.model not in MODELS:
        print(f"unknown model {args.model!r}; known: {list(MODELS)}", file=sys.stderr)
        return 2
    model_cfg = MODELS[args.model]
    task = load_task(args.task)

    # Resolve to absolute: the bare-origin remote is referenced by path from
    # inside the repo's cwd, so a relative workdir would break git push.
    cell = Path(args.workdir).resolve() / f"{args.task}__{args.model}__t{args.trial}"
    paths = setup_workspace(cell)
    repo = paths["repo"]

    # --- environment: set BEFORE importing pipeline_mcp_server ---
    os.environ["PLAN_DIR"] = str(paths["plans"])
    os.environ["WORKTREE_ROOT"] = str(paths["worktrees"])
    os.environ["REPO_ROOT"] = str(repo)
    os.environ["PIPELINE_AUTONOMY"] = "full"
    os.environ["PIPELINE_RISK_THRESHOLD"] = "low"
    os.environ["PIPELINE_MAX_CONCURRENT_AGENTS"] = "1"
    _set_review_backend_env()
    # Make sure no stale per-tier local override hijacks the "sonnet" story tier.
    for k in ("PIPELINE_LOCAL_MODEL_SONNET", "PIPELINE_LOCAL_MODEL_OPUS",
              "PIPELINE_LOCAL_MODEL_HAIKU"):
        os.environ.pop(k, None)
    os.environ.update(model_cfg["env"])

    import pipeline_mcp_server as p
    install_merge_stubs(p, repo)

    if model_cfg.get("mock"):
        # Fully offline: inject the mock dispatch backend and a stub reviewer.
        import backend as _backend
        mock = MockBackend(task)
        _orig_get_backend = _backend.get_backend

        def _get_backend(role, name=None):
            # Serve every role from the mock so the resource gate
            # (_role_resource_ok) and dispatch are fully offline; the reviewer
            # itself is stubbed below to always APPROVE.
            return mock

        _backend.get_backend = _get_backend
        p.backend.get_backend = _get_backend
        # _parse_verdict looks for the "VERDICT: APPROVE" marker, not a bare word.
        p._run_reviewer = lambda worktree, branch: "VERDICT: APPROVE"

    plan_name = f"bench_{args.task}_{args.model}_t{args.trial}"
    story_key = task["name"].upper().replace("_", "-")
    plan = build_plan(repo, task)

    p.save_plan(plan_name, json.dumps(plan))
    p.ingest_plan(plan_name)

    started = time.time()
    deadline = started + args.timeout
    ticks = drive(p, plan_name, story_key, deadline, args.tick,
                 max_defer_extension=args.max_defer_extension)
    elapsed = round(time.time() - started, 1)

    manifest = json.loads((paths["plans"] / f"{plan_name}.manifest.json").read_text())
    story = manifest["stories"][story_key]
    final_status = story["status"]

    # Grade independent ground-truth against merged master (done) or the
    # surviving worktree (parked/failed) so we can tell "wrong code" from
    # "correct code the pipeline failed to land".
    if final_status == "done":
        gt_src, gt_where = repo, "master"
    else:
        wt = Path(story.get("worktree", ""))
        gt_src, gt_where = (wt, "worktree") if wt.is_dir() else (repo, "master")
    gt = run_groundtruth(gt_src, task["impl_file"], task["groundtruth_source"],
                         cell / "_gt")

    result = {
        "task": args.task,
        "model": args.model,
        "trial": args.trial,
        "final_status": final_status,
        "merged": final_status == "done",
        "review_verdict": story.get("review_verdict"),
        "rework_attempts": story.get("rework_attempts", 0),
        "dispatch_attempts": story.get("dispatch_attempts", 0),
        "dispatched_model": story.get("dispatched_model"),
        "groundtruth_where": gt_where,
        "groundtruth_passed": gt.get("passed", False),
        "groundtruth_ran": gt.get("ran", False),
        "elapsed_s": elapsed,
        "ticks": len(ticks),
        "timed_out": final_status not in TERMINAL,
        "tick_log": ticks,
        "groundtruth_tail": gt.get("tail", gt.get("reason", "")),
    }
    (cell / "result.json").write_text(json.dumps(result, indent=2))

    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("tick_log", "groundtruth_tail")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
