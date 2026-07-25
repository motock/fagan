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

    _ci_status   -> actually runs the story's worktree test suite (a second,
                    independent check before merge, mirroring real CI --
                    NOT a rubber-stamp; see install_merge_stubs)
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

def _resolve_pipeline_repo(bench_dir: Path) -> Path:
    """Resolve the canonical pipeline repo root from bench_dir.

    bench_dir may sit inside a git worktree of the pipeline repo rather than
    the canonical checkout (e.g. when harness.py runs from a story's agent
    worktree). A worktree never contains .venv (it is gitignored), so
    naively taking bench_dir.parents[1] would point VENV_PY at a
    nonexistent interpreter, making setup_workspace's pytest-ecosystem
    fixture symlink .venv to a path that doesn't exist and every trial fall
    back to a bare `pytest` that isn't on PATH.

    Mirrors pipeline_mcp_server.py's _venv_python_for: `git rev-parse
    --git-common-dir` always resolves to the MAIN repo's .git, even when
    invoked from one of its worktrees, so its parent is the canonical repo
    root regardless of where bench_dir actually lives.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(bench_dir), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            common_path = Path(result.stdout.strip())
            if not common_path.is_absolute():
                common_path = (bench_dir / common_path).resolve()
            return common_path.parent
    except Exception:
        pass
    return bench_dir.parents[1]


BENCH_DIR = Path(__file__).resolve().parent
PIPELINE_REPO = _resolve_pipeline_repo(BENCH_DIR)
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
    "ratelimiter_inspect": '''
class TokenBucket:
    def __init__(self, capacity, refill_rate, now=0.0):
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be > 0")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.tokens = float(capacity)
        self.last = float(now)

    def _refilled_level(self, now):
        if now is None:
            now = self.last
        elapsed = now - self.last
        if elapsed > 0:
            return min(self.capacity, self.tokens + elapsed * self.refill_rate), now
        return self.tokens, self.last

    def allow(self, tokens=1.0, now=None):
        if tokens < 0:
            raise ValueError("tokens must be >= 0")
        level, effective_now = self._refilled_level(now)
        self.tokens = level
        self.last = effective_now
        if tokens <= self.tokens:
            self.tokens -= tokens
            return True
        return False

    def available_tokens(self, now=None):
        level, _ = self._refilled_level(now)
        return level
''',
    "ratelimiter_bugfix": '''
class RateLimiter:
    """A simple token-bucket rate limiter."""

    def __init__(self, capacity, refill_rate, now=0.0):
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be positive")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.tokens = float(capacity)
        self.last_time = float(now)

    def allow(self, cost=1.0, now=None):
        if cost < 0:
            raise ValueError("cost must be non-negative")
        current = self.last_time if now is None else float(now)
        elapsed = max(0.0, current - self.last_time)
        refill = elapsed * self.refill_rate
        self.tokens = min(self.capacity, self.tokens + refill)
        self.last_time = current
        if cost <= self.tokens:
            self.tokens -= cost
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
    # Gap 3: ecosystem-tagged reference impls for the cargo + npm tasks.
    # Same shape as the python ones; the mock backend writes this into
    # the ecosystem's expected impl path (src/lib.rs for cargo, src/*.js
    # for npm) so the offline `mock` cell can self-test the new code
    # paths without a real model.
    "lru_cache_rs": '''
use std::collections::HashMap;

pub struct LruCache {
    capacity: usize,
    map: HashMap<i32, i32>,
    recency: Vec<i32>,  // least-recently-used first
}

impl LruCache {
    pub fn new(capacity: usize) -> Self {
        if capacity < 1 {
            panic!("capacity must be >= 1");
        }
        LruCache { capacity, map: HashMap::new(), recency: Vec::new() }
    }
    pub fn get(&mut self, key: i32) -> Option<i32> {
        let v = self.map.get(&key).copied()?;
        self.recency.retain(|k| k != &key);
        self.recency.push(key);
        Some(v)
    }
    pub fn put(&mut self, key: i32, value: i32) {
        if self.map.contains_key(&key) {
            self.map.insert(key, value);
            self.recency.retain(|k| k != &key);
            self.recency.push(key);
            return;
        }
        self.map.insert(key, value);
        self.recency.push(key);
        if self.map.len() > self.capacity {
            let evict = self.recency.remove(0);
            self.map.remove(&evict);
        }
    }
    pub fn size(&self) -> usize { self.map.len() }
}
''',
    "interval_merge_js": '''
function merge(intervals) {
  for (const [s, e] of intervals) {
    if (s > e) throw new Error("start > end");
  }
  const sorted = [...intervals].sort((a, b) => a[0] - b[0]);
  const out = [];
  for (const [s, e] of sorted) {
    if (out.length && s <= out[out.length - 1][1] + 1) {
      out[out.length - 1][1] = Math.max(out[out.length - 1][1], e);
    } else {
      out.push([s, e]);
    }
  }
  return out;
}
module.exports = { merge };
''',
}


def _sh(argv, cwd, check=True):
    return subprocess.run(argv, cwd=str(cwd), check=check,
                          capture_output=True, text=True)


# Convention per ecosystem for files that count as "tests" for the
# TDD-skip check. The T2 story brief asks the agent to add a regression
# test alongside the impl fix; the scorecard flags a cell that fixes
# the impl without adding one. Centralised here so the helper and any
# future test-discovery code stay in lockstep.
_TEST_FILE_PATTERNS: dict[str, tuple[str, ...]] = {
    "pytest": ("test_", "_test.py", "tests/"),
    "cargo":  ("tests/", "_test.rs"),
    "npm":    ("test/", ".test.js", "test_"),
}


def _is_test_path(rel: str, ecosystem: str) -> bool:
    patterns = _TEST_FILE_PATTERNS.get(ecosystem, _TEST_FILE_PATTERNS["pytest"])
    return any(p in rel for p in patterns)


def _tdd_diff(repo: Path, init_sha: str, impl_file: str,
              ecosystem: str = "pytest") -> tuple[bool, bool]:
    """Return (impl_changed, test_changed) for paths under `repo` that
    differ from `init_sha`. Used by the scorecard to flag a T2 cell that
    modified the impl without adding a regression test
    (project_t2_tdd_skip_finding.md). Pure function: no side effects,
    no manifest reads. Failures (missing init, dirty tree, etc.) report
    both-false so the scorecard does not false-positive on infra noise.
    """
    try:
        out = _sh(["git", "diff", "--name-only", init_sha, "HEAD"],
                  cwd=repo, check=True).stdout
    except subprocess.CalledProcessError:
        return (False, False)
    changed = [p.strip() for p in out.splitlines() if p.strip()]
    impl_changed = impl_file in changed
    test_changed = any(_is_test_path(p, ecosystem) for p in changed)
    return (impl_changed, test_changed)


def load_task(name: str) -> dict:
    spec = json.loads((TASKS_DIR / name / "spec.json").read_text())
    # Ecosystem dispatch (Gap 3): the harness used to be pytest-only, with
    # hard-coded `acceptance.py`/`groundtruth.py` filenames and a pyproject.toml
    # scaffold. Tasks now declare `ecosystem: "pytest" | "cargo" | "npm"` in
    # spec.json (defaulting to "pytest" for backwards compatibility with every
    # existing task). The acceptance/groundtruth files are renamed accordingly:
    #   pytest: acceptance.py / groundtruth.py  (materially unchanged)
    #   cargo:  acceptance.rs / groundtruth.rs  (Rust integration test)
    #   npm:    acceptance.test.js / groundtruth.test.js
    # The "test_" / "test_" prefix is what makes the test runner pick them up
    # (pytest auto-discovers, cargo's tests/ dir + `cargo test`, npm's `node
    # --test` walks test/ for *.test.js).
    ecosystem = spec.get("ecosystem", "pytest")
    ext_map = {"pytest": "py", "cargo": "rs", "npm": "test.js"}
    if ecosystem not in ext_map:
        raise ValueError(
            f"task {name!r} declares unknown ecosystem={ecosystem!r}; "
            f"expected one of {list(ext_map)}"
        )
    ext = ext_map[ecosystem]
    spec["ecosystem"] = ecosystem
    spec["acceptance_source"] = (TASKS_DIR / name / f"acceptance.{ext}").read_text()
    spec["groundtruth_source"] = (TASKS_DIR / name / f"groundtruth.{ext}").read_text()
    # Tier 2+ tasks ("modify existing code", vs. Tier 1's greenfield katas)
    # seed the repo with an existing, already-committed codebase via
    # tasks/<name>/seed/ - a real directory tree (not JSON-embedded strings,
    # so seed files get normal syntax highlighting/editing) mirrored
    # verbatim into the repo before setup_workspace's initial commit. Empty
    # dict (not an error) when a task has no seed/ dir - the common case for
    # every existing Tier 1 task.
    seed_dir = TASKS_DIR / name / "seed"
    seed_files: dict[str, str] = {}
    if seed_dir.is_dir():
        for path in seed_dir.rglob("*"):
            if path.is_file():
                seed_files[str(path.relative_to(seed_dir))] = path.read_text()
    spec["seed_files"] = seed_files
    return spec


def setup_workspace(cell: Path, task: dict | None = None) -> dict[str, Path]:
    """Create the isolated repo + local bare origin + plan/worktree dirs.

    When `task` carries `seed_files` (a Tier 2+ "modify existing code" task,
    see load_task), those files are written into the repo and folded into
    the SAME initial commit as the scaffold - so the dispatched agent's
    first `git log`/`git status` sees one clean "init" commit containing a
    pre-existing codebase, not a suspicious separate "seed" commit or dirty
    tree. `task=None` (or a task with no seed_files) behaves exactly as
    before this existed: an empty scaffold repo.

    Ecosystem dispatch (Gap 3): pytest writes pyproject.toml + .venv (so
    `detect_test_command` resolves to `pytest`); cargo writes a Cargo.toml
    + src/ dir; npm writes a package.json + src/ dir. Non-pytest tasks
    MUST NOT have a pyproject.toml, otherwise `detect_test_command`
    (priority: pom > gradle > package.json > make > pyproject > cargo)
    would still pick pytest and run the (cargo/node) test files through
    the wrong runner.
    """
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

    ecosystem = (task or {}).get("ecosystem", "pytest")
    if ecosystem == "pytest":
        # pyproject.toml makes detect_test_command() pick pytest; the .venv
        # symlink makes _venv_python_for() resolve to the pipeline venv
        # (with pytest + deps).
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "bench-task"\nversion = "0.0.0"\n'
        )
        (repo / "README.md").write_text("# benchmark task workspace\n")
        (repo / ".venv").symlink_to(PIPELINE_REPO / ".venv")
        (repo / ".gitignore").write_text(".venv/\n__pycache__/\n")
    elif ecosystem == "cargo":
        # Cargo.toml at the root makes `detect_test_command` pick `cargo test`.
        # Edition 2021 is the modern default; src/lib.rs is the conventional
        # layout for a library crate. We do NOT symlink a .venv (cargo has
        # its own toolchain) and we do NOT write pyproject.toml (it would
        # win the priority and route the test runner to pytest).
        (repo / "Cargo.toml").write_text(
            '[package]\nname = "bench_task"\nversion = "0.0.0"\nedition = "2021"\n'
            '[lib]\npath = "src/lib.rs"\n'
        )
        (repo / "src").mkdir()
        # Seed src/lib.rs with a placeholder so cargo can build the empty
        # crate before the agent writes its impl; the agent overwrites it
        # (impl_file is "src/lib.rs" by spec.json convention). This keeps
        # the initial commit self-consistent (a `cargo build` works, a
        # `cargo test` reports "no tests" rather than "compilation
        # failed" - so the dispatch path is unambiguously "tests fail
        # until the agent adds an impl").
        (repo / "src" / "lib.rs").write_text(
            '// placeholder; the dispatched agent replaces this file\n'
        )
        (repo / "README.md").write_text("# benchmark cargo task workspace\n")
        (repo / "target").mkdir()
        (repo / ".gitignore").write_text("target/\n")
    elif ecosystem == "npm":
        # package.json with `node --test test/*.test.js` is enough for
        # `detect_test_command` to pick it. No external deps - we use the
        # built-in `node:test` + `node:assert` so a fresh `node` install
        # (>=18) runs the suite with no `npm install` step. The agent's
        # impl file lives under src/ by convention; acceptance/groundtruth
        # under test/ (Node's --test runner walks test/ for *.test.js).
        # We use the explicit `test/*.test.js` glob because Node 22
        # treats the bare `test/` positional argument as a module path
        # to RUN, not a directory to scan (see Node issue tracker:
        # `node --test <dir>` was the documented form pre-22; Node 22
        # requires an explicit glob or a `--test-name-pattern` flag).
        (repo / "package.json").write_text(
            '{\n  "name": "bench-task",\n  "version": "0.0.0",\n'
            '  "scripts": { "test": "node --test test/*.test.js" }\n}\n'
        )
        (repo / "src").mkdir()
        (repo / "test").mkdir()
        # placeholder impl so the initial commit is non-empty; the agent
        # overwrites this (impl_file is "src/merge.js" or similar by
        # spec.json convention).
        (repo / "src" / "placeholder.js").write_text(
            "// placeholder; the dispatched agent replaces the real impl file\n"
            "module.exports = {};\n"
        )
        (repo / "README.md").write_text("# benchmark npm task workspace\n")
        (repo / ".gitignore").write_text("node_modules/\n")
    else:
        # load_task validates ecosystem; reaching here means a programmer
        # error rather than a user-facing bad task spec.
        raise ValueError(f"setup_workspace: unknown ecosystem {ecosystem!r}")

    for rel_path, content in (task or {}).get("seed_files", {}).items():
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _sh(["git", "add", "-A"], repo)
    _sh(["git", "commit", "-qm", "init"], repo)
    init_sha = _sh(["git", "rev-parse", "HEAD"], repo).stdout.strip()

    _sh(["git", "init", "--bare", "-q", "-b", "master", "."], origin)
    _sh(["git", "remote", "add", "origin", str(origin)], repo)
    _sh(["git", "push", "-q", "-u", "origin", "master"], repo)
    return {"repo": repo, "origin": origin, "plans": plans,
            "worktrees": worktrees, "init_sha": init_sha}


def build_plan(repo: Path, task: dict) -> dict:
    """One epic, one story carrying the acceptance fixture as a read-only oracle.
    The acceptance path and the file the agent must write are dispatched on
    the task's ecosystem (Gap 3): pytest writes the acceptance to
    test_acceptance.py (the agent imports lru_cache.py directly); cargo
    writes tests/test_acceptance.rs (a Rust integration test that uses
    bench_task::*); npm writes test/acceptance.test.js (Node's --test
    runner auto-discovers *.test.js under test/). The agent's impl path
    is also ecosystem-specific - src/lib.rs for cargo, src/merge.js or
    similar for npm, plain lru_cache.py for pytest."""
    ecosystem = task.get("ecosystem", "pytest")
    if ecosystem == "pytest":
        acceptance_path = "test_acceptance.py"
    elif ecosystem == "cargo":
        acceptance_path = "tests/test_acceptance.rs"
    elif ecosystem == "npm":
        acceptance_path = "test/acceptance.test.js"
    else:
        # load_task validates this; reaching here is a programmer error.
        raise ValueError(f"build_plan: unknown ecosystem {ecosystem!r}")
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
                    {"path": acceptance_path, "source": task["acceptance_source"]}
                ],
            }],
        }],
    }


def build_plan_from_stories(repo: Path, epic_summary: str, stories: list[dict]) -> dict:
    """Wrap a pre-built story list in the same plan envelope build_plan() uses,
    for compound tasks with more than one story (e.g. a product-analyst
    decomposition, or a hand-written monolithic control) -- see
    tests/benchmark/PRODUCT_ANALYST_VALIDATION_PLAN.md. Drive the result with
    drive_plan(), not drive()."""
    return {
        "repo_root": str(repo),
        "epics": [{
            "summary": epic_summary,
            "stories": stories,
        }],
    }


def install_merge_stubs(p, repo: Path) -> None:
    """Replace the three gh-calling seams with hermetic git-only equivalents.

    `p` is `pipeline_mcp_server`, a backward-compat shim that COPIES each
    name out of `pipeline.server` into its own namespace at import time
    (`globals()[_name] = getattr(_server, _name)`). Assigning `p._ci_status =
    ...` only rebinds that copy - the real review/merge-gate code inside
    pipeline/server.py resolves `_ci_status` etc. from ITS OWN module
    globals, so the shim-only assignment silently never took effect (root-
    caused live 2026-07-25: every benchmark cell's merge gate was calling
    the REAL `_ci_status`/`_open_pr`/`_merge_pr` - which shell out to `gh` -
    against this harness's local-only bare-repo remote, not these hermetic
    stubs). Patch the actual `pipeline.server` module directly; also patch
    the shim for API-surface consistency with anything still reading `p.X`.
    """
    import pipeline.server as _pserver

    def _ci_status_stub(branch, *, sha=None, timeout_s=None):
        """Actually run the story's worktree test suite, mirroring what a
        real CI pipeline would do -- unlike a rubber-stamp "always pass",
        this is a second, independent check before merge. Without it the
        hermetic benchmark has no equivalent to what a real CI-gated repo
        provides, which is one plausible contributor to a merged-but-wrong
        story slipping through undetected (see the RLI-3 incident in
        PRODUCT_ANALYST_VALIDATION_PLAN.md, 2026-07-04). Runs against the
        story's worktree (already rebased onto current master by the time
        the merge gate calls this) rather than the not-yet-merged branch
        tip in the shared repo, so this needs no extra checkout.
        """
        story_key = branch.split("/", 1)[1].upper()
        worktree = p.WORKTREE_ROOT / story_key
        if not worktree.is_dir():
            return {"state": "pass", "error": ""}
        test_dir, test_cmd = p.detect_test_command(worktree)
        if not test_cmd:
            return {"state": "pass", "error": ""}
        r = subprocess.run(test_cmd, cwd=test_dir, capture_output=True, text=True)
        if r.returncode == 0:
            return {"state": "pass", "error": ""}
        return {"state": "fail", "error": (r.stdout + r.stderr)[-500:]}

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

    for target in (p, _pserver):
        target._ci_status = _ci_status_stub
        target._open_pr = _open_pr_stub
        target._merge_pr = _merge_pr_stub


class MockBackend:
    """Offline backend for self-testing the harness with no model/network.

    dispatch() synchronously writes a correct reference implementation, commits
    it on the story branch, and returns a handle whose pid has already exited so
    the next check_story_status tick proceeds straight to the test gate.
    """

    def __init__(self, task: dict):
        self.task = task
        # Calls the guided-decomposition planner made against this backend
        # (GUIDED_DECOMPOSITION_PLAN.md) - empty unless PIPELINE_DECOMPOSE is
        # enabled for the run, since dispatch_story only calls complete() at
        # all when decompose is on. compound_harness.py surfaces the count
        # in result.json so an offline mock run can prove the planner path
        # was actually exercised, not just that dispatch() still succeeds.
        self.complete_calls: list[dict] = []

    def complete(self, prompt, *, system=None, model=None, allowed_tools=None,
                 cwd=None, max_tokens=None, cell_dir=None):
        """Canned single-shot response, so an offline `mock` run can exercise
        the guided-decomposition planner call (backend.Backend.complete())
        with no real model/network. Not meant to look like a REAL checklist -
        just enough structure for the harness's own plumbing tests."""
        self.complete_calls.append({"prompt": prompt, "system": system, "model": model})
        return "1. [mock] Write a failing test.\n2. [mock] Implement it."

    def dispatch(self, *, prompt, system, model, allowed_tools, cwd, log_path,
                 append=False, acceptance=None):
        from backend import AgentHandle  # local import; env already set
        cwd = Path(cwd)
        # BENCH_MOCK_IMPL_FILE lets the harness self-test inject a deliberately
        # wrong implementation to prove the independent ground-truth catches it.
        override = os.environ.get("BENCH_MOCK_IMPL_FILE")
        impl = Path(override).read_text() if override else _MOCK_IMPLS[self.task["name"]]
        (cwd / self.task["impl_file"]).write_text(impl.lstrip("\n"))
        # Every dispatch writes byte-identical impl content (the mock backend
        # isn't incremental), so in a multi-story chain (compound_harness.py) a
        # later story's worktree can end up byte-identical to what an earlier
        # story already merged onto master -- an empty diff that fails the
        # merge's `git commit`. agent.log can't be used to force a real diff
        # (it's excluded via .git/info/exclude, see Mode 17 in FINDINGS.md), so
        # commit a small tracked marker instead, tagged with this worktree's
        # own directory name -- always unique per story/dispatch.
        (cwd / ".mock_dispatch_marker").write_text(f"{cwd.name}\n")
        if not acceptance:
            # No acceptance oracle was materialized for this story (a
            # non-terminal story in a compound multi-story plan, see
            # compound_harness.py -- only the chain's sink story carries one).
            # A real agent writes its own TDD tests per agent_instructions;
            # the mock backend only self-tests plumbing, so give pytest a
            # trivial passing test to collect instead of "no tests ran".
            module_name = Path(self.task["impl_file"]).stem
            (cwd / "test_mock_smoke.py").write_text(
                f"def test_mock_smoke_import():\n    import {module_name}\n"
            )
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
    else:
        # Deadline passed without the story ever reaching a terminal status -
        # reap any still-running dispatch subprocess instead of leaving it
        # orphaned (observed directly: a hung MLX dispatch subprocess
        # survived past this harness's own timeout, found still running
        # minutes later, had to be killed manually).
        p.interrupt_story(plan_name, story_key)
    return ticks


def drive_plan(p, plan_name: str, story_keys: list[str], deadline: float,
               tick_interval: float, max_defer_extension: float = 14400.0) -> list[dict]:
    """Like drive(), but for a dependency-chained plan with several stories:
    ticks until EVERY key in story_keys is terminal, instead of a single one.

    Deadline extension (FM-H, see drive()) triggers if ANY story in the plan
    is deferred on a given tick, since a rate-limited reviewer blocks the
    whole chain regardless of which story it's currently reviewing.
    """
    # A story with an unmet dependency is never dispatched (list_ready_stories
    # requires every dependency to be "done", see pipeline_mcp_server.py) --
    # so once a dependency lands on a terminal status OTHER than "done"
    # (failed/parked), its dependents can never become ready and will sit at
    # their initial status forever. This propagates transitively: a story
    # three links down a chain is just as stuck as the one directly blocked,
    # even though ITS direct dependency's status string is still "todo" (it
    # never got a chance to fail/park because it was never dispatched).
    blocked_terminal = {"failed", "parked"}

    def _is_permanently_blocked(key: str, statuses: dict[str, str],
                                stories: dict, memo: dict[str, bool]) -> bool:
        if key in memo:
            return memo[key]
        memo[key] = False  # guard against a cyclic dependency graph
        blocked = any(
            statuses.get(dep) in blocked_terminal
            or _is_permanently_blocked(dep, statuses, stories, memo)
            for dep in stories.get(key, {}).get("dependencies", [])
        )
        memo[key] = blocked
        return blocked

    ticks: list[dict] = []
    extension_used = 0.0
    statuses: dict[str, str] = {}
    stories: dict = {}
    while time.time() < deadline:
        summary = p.advance_pipeline(plan_name)
        manifest = json.loads(
            (Path(p.PLAN_DIR) / f"{plan_name}.manifest.json").read_text()
        )
        stories = manifest["stories"]
        statuses = {key: stories[key]["status"] for key in story_keys}
        ticks.append({"t": round(time.time(), 1), "statuses": statuses,
                      "skipped": summary.get("skipped")})
        if all(status in TERMINAL for status in statuses.values()):
            break
        pending = [key for key in story_keys if statuses[key] not in TERMINAL]
        if pending and all(
            _is_permanently_blocked(key, statuses, stories, {}) for key in pending
        ):
            # No further tick can make progress: stop instead of spinning out
            # the wall-clock budget until the deadline.
            break
        deferred = summary.get("review_deferred", [])
        if any(key in deferred for key in story_keys) and extension_used < max_defer_extension:
            deadline += tick_interval
            extension_used += tick_interval
        time.sleep(tick_interval)
    # Reap any story left non-terminal when the loop above stopped (deadline
    # passed, or the chain is permanently blocked) instead of leaving its
    # dispatch subprocess orphaned - same reasoning as drive()'s single-story
    # case. A never-dispatched (permanently blocked) story has no pid, so it's
    # excluded rather than issuing a no-op interrupt call for it.
    for key in story_keys:
        if statuses.get(key) not in TERMINAL and "pid" in stories.get(key, {}):
            p.interrupt_story(plan_name, key)
    return ticks


def run_groundtruth(impl_src: Path, impl_file: str, groundtruth: str,
                    scratch: Path, ecosystem: str = "pytest") -> dict:
    """Run the independent ground-truth suite against a copy of the impl file.

    Ecosystem dispatch (Gap 3): pytest copies the impl into a scratch dir
    alongside test_groundtruth.py and invokes `pytest`. cargo scaffolds a
    throwaway crate (Cargo.toml + src/lib.rs + tests/test_groundtruth.rs)
    and runs `cargo test` (this acquires _heavy_lock upstream of here --
    see install_merge_stubs for the same lock the reviewer's test run
    takes, so we don't double-allocate VRAM compiling two crates at once).
    npm writes package.json + src/<impl> + test/groundtruth.test.js and
    runs `node --test` (no _heavy_lock - node is light, but it does
    share node_modules, so we point HOME at the scratch to keep it
    isolated).

    A cargo impl that fails to compile returns `passed=False` with the
    compiler error in `tail` (the same channel pytest uses for failures) -
    so a "model wrote something that doesn't build" looks identical to
    "model wrote something that builds but fails the tests" in the
    result. The test-orchestrator scripts (`tests/benchmark/_post/*`)
    should treat either as wrong, which is the correct reading.
    """
    impl_path = impl_src / impl_file
    if not impl_path.exists():
        return {"ran": False, "passed": False, "reason": f"no {impl_file} at {impl_src}"}
    scratch.mkdir(parents=True, exist_ok=True)

    if ecosystem == "pytest":
        shutil.copy(impl_path, scratch / impl_file)
        (scratch / "test_groundtruth.py").write_text(groundtruth)
        r = subprocess.run(
            [str(VENV_PY), "-m", "pytest", "test_groundtruth.py", "-q",
             "--no-header", "-p", "no:cacheprovider"],
            cwd=str(scratch), capture_output=True, text=True,
        )
        return {"ran": True, "passed": r.returncode == 0,
                "tail": (r.stdout + r.stderr)[-700:]}

    if ecosystem == "cargo":
        # Copy the impl to the conventional src/lib.rs (the cargo way -
        # integration tests in tests/ import the crate via `use bench_task::*`,
        # so the impl file must be at the lib path, not at the spec's
        # impl_file location in the source tree). If the agent's spec put
        # the impl somewhere else, fall back to the src/lib.rs that was
        # used in the source repo (impl_path in the source tree IS what
        # the agent wrote; copy it to where cargo expects to find a lib).
        (scratch / "src").mkdir(exist_ok=True)
        (scratch / "tests").mkdir(exist_ok=True)
        (scratch / "Cargo.toml").write_text(
            '[package]\nname = "bench_task"\nversion = "0.0.0"\nedition = "2021"\n'
            '[lib]\npath = "src/lib.rs"\n'
        )
        shutil.copy(impl_path, scratch / "src" / "lib.rs")
        (scratch / "tests" / "test_groundtruth.rs").write_text(groundtruth)
        # `cargo test` is heavy (compiles the entire dependency graph for
        # the first invocation; warm-runs are faster but still 10-30s on
        # a cold target dir). The harness doesn't take _heavy_lock here
        # because run_groundtruth is called from main() after the cell is
        # already terminal, so there's no concurrent dispatch to
        # serialize against. If a future caller wants concurrent cells,
        # they should wrap this call in _heavy_lock.
        r = subprocess.run(
            ["cargo", "test", "--quiet"],
            cwd=str(scratch), capture_output=True, text=True,
        )
        return {"ran": True, "passed": r.returncode == 0,
                "tail": (r.stdout + r.stderr)[-700:]}

    if ecosystem == "npm":
        # Copy the impl to the conventional src/ path Node's --test runner
        # expects to require() from. The spec's impl_file is honored (the
        # agent's task told it exactly where to put the file - mirroring
        # that under src/ in the scratch dir keeps the test code's
        # `require()` paths consistent).
        (scratch / "src").mkdir(exist_ok=True)
        (scratch / "test").mkdir(exist_ok=True)
        (scratch / "package.json").write_text(
            '{\n  "name": "bench-task",\n  "version": "0.0.0",\n'
            '  "scripts": { "test": "node --test test/groundtruth.test.js" }\n}\n'
        )
        shutil.copy(impl_path, scratch / impl_file)
        (scratch / "test" / "groundtruth.test.js").write_text(groundtruth)
        r = subprocess.run(
            ["node", "--test", "test/groundtruth.test.js"],
            cwd=str(scratch), capture_output=True, text=True,
        )
        return {"ran": True, "passed": r.returncode == 0,
                "tail": (r.stdout + r.stderr)[-700:]}

    return {"ran": False, "passed": False,
            "reason": f"unknown ecosystem {ecosystem!r}"}


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
    paths = setup_workspace(cell, task)
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
        # **kwargs absorbs whatever backend_name=/plan_role_config=/acceptance=
        # the real call sites in pipeline/server.py pass so this stub doesn't
        # go stale again. Must patch pipeline.server directly (see
        # install_merge_stubs's docstring) - `p._run_reviewer = ...` alone
        # only rebinds the compat-shim's copy and never actually intercepts
        # the real reviewer call, which is why every mock-model cell parked
        # with review_verdict "UNKNOWN" after REVIEW_INCONCLUSIVE_MAX
        # attempts instead of completing (root-caused live 2026-07-25).
        import pipeline.server as _pserver

        def mock_reviewer(worktree, branch, **kwargs):
            return "VERDICT: APPROVE"

        p._run_reviewer = mock_reviewer
        _pserver._run_reviewer = mock_reviewer

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
                         cell / "_gt", ecosystem=task.get("ecosystem", "pytest"))

    # TDD-skip signal (project_t2_tdd_skip_finding.md): a T2 cell that
    # touches the impl without adding a regression test still scores
    # green today. Compute against the merged repo (or the surviving
    # worktree, same diff target as the GT path) so the scorecard can
    # surface a yellow flag on T2 stories. On any infra failure
    # (_tdd_diff returns both-false) we omit the booleans rather than
    # record a misleading signal.
    init_sha = paths.get("init_sha", "")
    tdd_src = repo if final_status == "done" else (
        Path(story.get("worktree", "")) if Path(story.get("worktree", "")).is_dir()
        else repo
    )
    if init_sha and tdd_src.is_dir():
        impl_ch, test_ch = _tdd_diff(tdd_src, init_sha, task["impl_file"],
                                      ecosystem=task.get("ecosystem", "pytest"))
    else:
        impl_ch, test_ch = False, False

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
        "task_tier": task.get("tier", ""),
        "impl_changed": impl_ch,
        "test_changed": test_ch,
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
