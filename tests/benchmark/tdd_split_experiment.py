"""TDD-split experiment on ratelimiter_inspect.

Tests the theory: does splitting test-authorship from implementation help
gpt-oss, and does a STRONGER test-author (Claude/tech-lead) help more than
gpt-oss authoring its own tests?

Three cells, all on ratelimiter_inspect:
  - BASELINE: gpt-oss does TDD + impl in ONE dispatch (the 8/9 run's
    monolithic shape; it parked 0/1 there). Reproduced here to validate the
    apparatus.
  - VARIANT A: gpt-oss writes tests (dispatch 1) -> gpt-oss implements
    against those tests (dispatch 2, fresh context).
  - VARIANT B: Claude (tech-lead) writes tests (dispatch 1) -> gpt-oss
    implements against those tests (dispatch 2).

A and B differ ONLY in the test-author model. The "one cell" framing for B
is realized as: gpt-oss's impl dispatch runs in a worktree pre-seeded with
the tech-lead's tests (one implementer cell). The literal in-story
decompose-emits-tests version is out of scope for this first cut.

This is an ISOLATED orchestrator: it reuses the benchmark's worktree setup
pattern + backend.py's dispatch primitives + the harness's run_groundtruth
oracle, but does NOT go through harness.main() (no review/merge/acceptance
machinery), so a test-only dispatch isn't pushed by a rework loop to also
write the impl.

Run:  python3 tests/benchmark/tdd_split_experiment.py [--only baseline|A|B]
Writes results to tests/benchmark/_runs/tdd_split_<ts>/.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # pipeline repo root
BENCH = Path(__file__).resolve().parent
TASK = "ratelimiter_inspect"
TASKDIR = BENCH / "tasks" / TASK
VENV_PY = REPO / ".venv" / "bin" / "python3"

# gpt-oss config matching the 8/9 run (models.py "gptoss" row: temp 1.0,
# ctx 32768, max_steps 60). The 8/9 run's ratelimiter_inspect parked at this
# config, so the baseline cell here must use the same to reproduce it.
GPTOSS_TAG = os.environ.get("BENCH_GPTOSS_TAG", "gpt-oss:20b")
GPTOSS_ENV = {
    "PIPELINE_BACKEND_DISPATCH": "local",
    "PIPELINE_LOCAL_TEMPERATURE": "1.0",
    "PIPELINE_LOCAL_NUM_CTX": "32768",
    "PIPELINE_LOCAL_MAX_STEPS": "60",
    "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS": "1800",
    # LOCAL_AGENT_PARK_ENABLED deliberately UNSET: the 8/9 benchmark run left it
    # at local_agent.py's default (1 = park enabled), so the baseline agent
    # parked on the repetition guard. Setting it to 0 (the production plist
    # value) would change the park semantics and break reproducibility.
}
CLAUDE_ENV = {
    "PIPELINE_BACKEND_DISPATCH": "claude",
}
# A stronger/different cloud test-author stand-in. The user's 2026-07-12
# conservation config redirects `claude --model sonnet` -> glm-5.2:cloud
# anyway (and the user considers glm ~ Claude in performance), so B's
# test phase routes glm-5.2:cloud via the clean local Ollama backend rather
# than the Claude-CLI redirect. If a TRUE Claude-authored-tests run is ever
# wanted, disable the redirect (edit ~/.claude.json alias / set
# PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV carefully) and switch B back to
# CLAUDE_ENV + a real claude-* model id.
GLM_TAG = os.environ.get("BENCH_GLM_TAG", "glm-5.2:cloud")
GLM_ENV = {
    "PIPELINE_BACKEND_DISPATCH": "local",
    "PIPELINE_LOCAL_TEMPERATURE": "0.3",
    "PIPELINE_LOCAL_NUM_CTX": "32768",
    "PIPELINE_LOCAL_MAX_STEPS": "60",
    "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS": "1800",
}

ALLOWED_TOOLS = "Read,Write,Edit,Bash"

# The spec rules (lifted from tasks/ratelimiter_inspect/spec.json
# agent_instructions, minus the "write tests then implement" TDD directive
# which is phase-specific). Used to build phase prompts.
SPEC_RULES = """\
Implement a token-bucket rate limiter as a class TokenBucket in rate_limiter.py with exactly this interface:
  - __init__(self, capacity, refill_rate, now=0.0)
  - allow(self, tokens=1.0, now=None) -> bool   # now = current time in seconds; if None, reuse the bucket's last-known time
  - available_tokens(self, now=None) -> float   # read-only inspector: report how many tokens would be available at `now` (refilled, capped at capacity) WITHOUT consuming any and WITHOUT mutating the bucket's internal state in any way -- calling it any number of times must have zero effect on subsequent allow()/available_tokens() results.

Behaviour requirements - ALL must hold:
  1. The bucket starts FULL (capacity tokens).
  2. Tokens refill continuously at refill_rate tokens/second based on elapsed time since the last state-changing call; the refilled level is CAPPED at capacity (never more).
  3. allow(t, now): first refill up to `now`, then if at least t tokens are available, deduct t and return True; otherwise deduct NOTHING and return False. Either way, the refill itself (not the deduction) is recorded so a later call computes elapsed time from `now`, not from the original last-call time.
  4. available_tokens(now): compute and return what the refilled level would be at `now` (same refill math as allow(), capped at capacity) but do NOT store it back onto the bucket and do NOT advance its internal last-call time. Calling available_tokens() one or many times must never change what a subsequent allow() call sees.
  5. Fractional tokens and fractional seconds must work for both allow() and available_tokens().
  6. Time only moves forward: if `now` (for either method) is earlier than the bucket's last-known time, treat elapsed time as 0 (no refill, no error) rather than raising or refilling negatively.
  7. now=None on either method reuses the bucket's last-known time (no refill).
  8. allow(t) where t > capacity can never succeed.
  9. Validation (raise ValueError): constructing a TokenBucket with capacity <= 0 or refill_rate <= 0; calling allow(tokens) with a negative tokens value. A negative tokens value must be rejected, not deducted (deducting a negative amount would inflate the bucket past capacity).
"""

SYSTEM = (
    "You are a software engineer. You work by editing files with the tools "
    "available. Run pytest to verify your work. When the task is complete, "
    "say you are done. Follow instructions exactly."
)

# ---- reference + FM-G-mutated impls for grading phase-1 test quality ----
# A known-correct TokenBucket (author-verified against groundtruth.py) used to
# check phase-1 tests are VALID (pass against correct code).
REF_IMPL = '''\
class TokenBucket:
    def __init__(self, capacity, refill_rate, now=0.0):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be positive")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.tokens = float(capacity)
        self.last = float(now)

    def _refilled(self, now):
        elapsed = max(0.0, now - self.last)
        return min(self.capacity, self.tokens + elapsed * self.refill_rate)

    def allow(self, tokens=1.0, now=None):
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        if now is None:
            now = self.last
        if now < self.last:
            now = self.last
        elapsed = now - self.last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last = now
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def available_tokens(self, now=None):
        if now is None:
            now = self.last
        if now < self.last:
            now = self.last
        elapsed = now - self.last
        return min(self.capacity, self.tokens + elapsed * self.refill_rate)
'''

# The FM-G bug class: available_tokens() reuses allow()'s refill-AND-STORE
# logic, so a peek mutates state. Used to check phase-1 tests are
# DISCRIMINATING (catch the bug). groundtruth.py's
# test_available_tokens_speculative_future_peek_does_not_lock_in_early_refill
# catches this; a good agent-authored suite should too.
FMG_IMPL = REF_IMPL.replace(
    "        elapsed = now - self.last\n"
    "        return min(self.capacity, self.tokens + elapsed * self.refill_rate)\n",
    "        elapsed = now - self.last\n"
    "        # FM-G bug: available_tokens mutates state (peeks lock in)\n"
    "        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)\n"
    "        self.last = now\n"
    "        return self.tokens\n",
    1,
)


def sh(argv, cwd, check=True, capture=True):
    r = subprocess.run(argv, cwd=str(cwd), capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd {argv} failed in {cwd}: {r.stderr[:500]}")
    return r


def setup_worktree(cell: Path, seed_files: dict[str, str] | None = None) -> Path:
    """Minimal repo with pyproject + .venv symlink + optional seed files,
    committed as one 'init' commit. Mirrors harness.setup_workspace's pytest
    branch without the origin/plans/worktrees dirs (we don't need them)."""
    if cell.exists():
        shutil.rmtree(cell)
    cell.mkdir(parents=True)
    repo = cell / "repo"
    repo.mkdir()
    sh(["git", "init", "-q", "-b", "master", "."], repo)
    sh(["git", "config", "user.email", "tddsplit@local"], repo)
    sh(["git", "config", "user.name", "tddsplit"], repo)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "bench-task"\nversion = "0.0.0"\n'
    )
    (repo / "README.md").write_text("# tdd-split experiment workspace\n")
    (repo / ".venv").symlink_to(REPO / ".venv")
    (repo / ".gitignore").write_text(".venv/\n__pycache__/\n.agent_transcript.json\n")
    for rel, content in (seed_files or {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    sh(["git", "add", "-A"], repo)
    sh(["git", "commit", "-qm", "init"], repo)
    return repo


def dispatch(prompt: str, model: str, backend_env: dict, cwd: Path,
             log_path: Path, timeout: float = 5400) -> tuple[int, str]:
    """Dispatch one agent run via backend.py's driver and block until exit.
    Returns (exit_code, log_tail)."""
    # Backend env must be set before importing/constructing the driver.
    for k, v in backend_env.items():
        os.environ[k] = v
    sys.path.insert(0, str(REPO))
    import backend  # noqa: E402
    driver = backend.get_backend("dispatch")
    h = driver.dispatch(
        prompt, system=SYSTEM, model=model,
        allowed_tools=ALLOWED_TOOLS, cwd=cwd, log_path=log_path, append=False,
    )
    pid = h.pid
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            wp = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            wp = (pid, 0)  # already reaped -> exited
        if wp != (0, 0):
            break
        time.sleep(3)
    else:
        # timeout: kill it
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        time.sleep(2)
    # reap if still hanging
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    tail = ""
    if log_path.exists():
        tail = log_path.read_text(errors="replace")[-1500:]
    exit_code = 0
    m = tail.rfind("parking")
    if "DONE" in tail or "done" in tail.lower().split("\n")[-1][:20]:
        exit_code = 0
    if m != -1:
        exit_code = 2  # parked
    return exit_code, tail


def run_pytest_against(test_file: Path, impl_src: str, scratch: Path,
                       impl_name: str = "rate_limiter.py") -> tuple[bool, str]:
    """Run a test file against a given impl source in a scratch dir.
    Returns (passed, tail)."""
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    (scratch / impl_name).write_text(impl_src)
    shutil.copy(test_file, scratch / test_file.name)
    r = subprocess.run(
        [str(VENV_PY), "-m", "pytest", test_file.name, "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=str(scratch), capture_output=True, text=True,
    )
    return (r.returncode == 0, (r.stdout + r.stderr)[-800:])


def run_groundtruth(impl_src: Path) -> tuple[bool, str]:
    """Run the task's independent groundtruth.py against an impl dir."""
    gt = (TASKDIR / "groundtruth.py").read_text()
    scratch = impl_src.parent / "_gt_scratch"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir()
    impl_path = impl_src / "rate_limiter.py"
    if not impl_path.exists():
        return False, f"no rate_limiter.py at {impl_src}"
    shutil.copy(impl_path, scratch / "rate_limiter.py")
    (scratch / "test_groundtruth.py").write_text(gt)
    r = subprocess.run(
        [str(VENV_PY), "-m", "pytest", "test_groundtruth.py", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=str(scratch), capture_output=True, text=True,
    )
    return (r.returncode == 0, (r.stdout + r.stderr)[-800:])


# ---- phase prompts ----
def phase1_prompt() -> str:
    return (
        "Write a pytest test suite in a NEW file test_rate_limiter.py for a "
        "TokenBucket rate limiter. Write ONLY the test file -- do NOT create "
        "rate_limiter.py (the implementation will be written separately later; "
        "your tests will fail with an import error until it exists, which is "
        "the expected TDD red state).\n\n"
        + SPEC_RULES
        + "\n\nThe tests must `from rate_limiter import TokenBucket`. Cover EVERY "
          "rule above, including negative and boundary cases (zero tokens, "
          "negative tokens, non-positive capacity/refill_rate, t > capacity, "
          "time going backwards, exact-capacity drain, fractional tokens/"
          "seconds, now=None reuse, and -- critically -- that "
          "available_tokens() never mutates state: call it repeatedly and "
          "then assert allow() still behaves as if available_tokens was never "
          "called (the speculative-future-peek must not lock in an early "
          "refill). Run pytest to confirm the tests fail (import error). "
          "Then say you are done."
    )


def phase2_prompt() -> str:
    return (
        "Implement a TokenBucket rate limiter in a NEW file rate_limiter.py so "
        "that the EXISTING test_rate_limiter.py (already present in the repo) "
        "passes. Do NOT modify test_rate_limiter.py -- only write "
        "rate_limiter.py. Run pytest to verify the tests pass.\n\n"
        "The contract the tests enforce:\n" + SPEC_RULES
        + "\n\nThen say you are done."
    )


def baseline_prompt() -> str:
    # The original monolithic TDD-mandated prompt (tests first, then impl,
    # in one dispatch) -- matches spec.json's agent_instructions.
    return (
        "Implement a token-bucket rate limiter in a NEW file rate_limiter.py.\n\n"
        + SPEC_RULES
        + "\n\nFollow strict TDD: write pytest tests in test_rate_limiter.py "
          "covering every one of these rules FIRST, confirm they fail, then "
          "implement until they pass. Cover negative and boundary cases (zero "
          "tokens, negative tokens, non-positive capacity/refill_rate, "
          "t > capacity, time going backwards, exact-capacity drain, and -- "
          "critically -- that available_tokens() never mutates state, "
          "verified by calling it repeatedly and then checking allow() still "
          "behaves as if it was never called). Then say you are done."
    )


def run_variant(name: str, outdir: Path, phases: list[tuple[str, str, dict, str]],
                seed_from_prev: bool = False) -> dict:
    """Run a sequence of (label, prompt, backend_env, model) dispatches.
    If seed_from_prev, phase N's worktree is seeded with the previous phase's
    produced test_rate_limiter.py."""
    print(f"\n=== {name} ===", flush=True)
    result = {"name": name, "phases": [], "groundtruth_passed": None,
              "test_quality": None}
    prev_tests = None
    for label, prompt, env, model in phases:
        cell = outdir / f"{name}__{label}"
        seed = {"test_rate_limiter.py": prev_tests} if (seed_from_prev and prev_tests) else None
        repo = setup_worktree(cell, seed_files=seed)
        log = cell / "agent.log"
        t0 = time.monotonic()
        print(f"[{name}/{label}] dispatching {model} ...", flush=True)
        ec, tail = dispatch(prompt, model, env, repo, log)
        dt = time.monotonic() - t0
        print(f"[{name}/{label}] exit={ec} elapsed={dt:.0f}s", flush=True)
        produced = None
        tf = repo / "test_rate_limiter.py"
        implf = repo / "rate_limiter.py"
        if tf.exists():
            produced = tf.read_text()
        impl = implf.read_text() if implf.exists() else None
        result["phases"].append({
            "label": label, "model": model, "exit": ec, "elapsed_s": round(dt),
            "wrote_tests": bool(produced), "wrote_impl": bool(impl),
            "log_tail": tail[-400:],
        })
        if produced:
            prev_tests = produced
        # grade impl if this phase produced one
        if impl:
            gp, gt_tail = run_groundtruth(repo)
            result["groundtruth_passed"] = gp
            result["groundtruth_tail"] = gt_tail
            print(f"[{name}/{label}] groundtruth_passed={gp}", flush=True)
        # grade test quality if this phase produced tests
        if produced:
            tq = grade_tests(tf, cell / "_tq_scratch")
            result["test_quality"] = tq
            print(f"[{name}/{label}] test_quality={tq}", flush=True)
    return result


def grade_tests(test_file: Path, scratch_base: Path) -> dict:
    """Phase-1 test quality: valid (pass vs REF_IMPL) + discriminating
    (fail vs FMG_IMPL)."""
    s1 = scratch_base / "ref"
    ref_ok, ref_tail = run_pytest_against(test_file, REF_IMPL, s1)
    s2 = scratch_base / "fmg"
    fmg_ok, fmg_tail = run_pytest_against(test_file, FMG_IMPL, s2)
    # count test functions
    ntests = sum(1 for line in test_file.read_text().splitlines()
                 if line.strip().startswith("def test_"))
    return {
        "ntests": ntests,
        "pass_vs_correct_impl": ref_ok,   # True = valid tests (not over-specified)
        "fails_vs_fmg_impl": not fmg_ok,  # True = discriminating (catches FM-G)
        "ref_tail": ref_tail[-300:],
        "fmg_tail": fmg_tail[-300:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["baseline", "A", "B"], default=None,
                    nargs="+",
                    help="run a subset of variants; repeat or space-separate (e.g. --only A B)")
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()
    only = args.only  # None or a list of variant names

    ts = time.strftime("%Y%m%d_%H%M%S")
    outdir = Path(args.outdir) if args.outdir else BENCH / "_runs" / f"tdd_split_{ts}"
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"TDD-split experiment -> {outdir}", flush=True)

    # Ensure gpt-oss is warm (avoids a cold-start skew on the first cell).
    try:
        subprocess.run(["ollama", "run", GPTOSS_TAG, ""],
                       capture_output=True, text=True, timeout=120)
    except Exception as e:
        print(f"ollama warm-up skipped: {e}", flush=True)

    results = {}

    if only is None or "baseline" in only:
        r = run_variant("baseline", outdir,
                        [("tdd_impl", baseline_prompt(), GPTOSS_ENV, GPTOSS_TAG)],
                        seed_from_prev=False)
        results["baseline"] = r

    if only is None or "A" in only:
        r = run_variant("A", outdir, [
            ("tests", phase1_prompt(), GPTOSS_ENV, GPTOSS_TAG),
            ("impl", phase2_prompt(), GPTOSS_ENV, GPTOSS_TAG),
        ], seed_from_prev=True)
        results["A"] = r

    if only is None or "B" in only:
        r = run_variant("B", outdir, [
            ("tests", phase1_prompt(), GLM_ENV, GLM_TAG),
            ("impl", phase2_prompt(), GPTOSS_ENV, GPTOSS_TAG),
        ], seed_from_prev=True)
        results["B"] = r

    (outdir / "results.json").write_text(json.dumps(results, indent=2))

    # comparison table
    print("\n\n========== TDD-SPLIT COMPARISON ==========")
    print(f"task={TASK}  outdir={outdir}\n")
    print(f"{'cell':<10} {'impl_gt':<9} {'tests_written':<14} {'test_quality':<40}")
    print("-" * 75)
    for name, r in results.items():
        gt = r.get("groundtruth_passed")
        gt_s = "n/a (no impl)" if gt is None else ("PASS" if gt else "FAIL")
        # tests-written: from the phase that wrote tests (baseline: the one phase; A/B: phase 1)
        wrote = any(p["wrote_tests"] for p in r["phases"])
        tq = r.get("test_quality")
        if tq:
            tq_s = f"ntests={tq['ntests']} valid={tq['pass_vs_correct_impl']} catchesFMG={tq['fails_vs_fmg_impl']}"
        else:
            tq_s = "n/a"
        print(f"{name:<10} {gt_s:<9} {str(wrote):<14} {tq_s:<40}")
    print("\nBaseline reproduces 8/9 run's parked 0/1 => apparatus valid.")
    print("A beats baseline => fresh impl dispatch unblocks the stuck implementer.")
    print("B beats A => stronger test-author is the bigger lever.")
    print(f"\nFull results: {outdir / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())