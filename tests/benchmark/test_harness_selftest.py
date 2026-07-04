"""Offline self-tests for the benchmark harness (no model/network required).

Runs harness.py with the `mock` backend as a subprocess and asserts the full
pipeline drives to `done` and grades correctly. Crucially, it also injects a
deliberately wrong implementation that PASSES the visible acceptance oracle but
FAILS the independent ground-truth -- proving the ground-truth is what actually
catches a model that games its own tests, which is the whole point of the
benchmark.

Run: pytest tests/benchmark/test_harness_selftest.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
PIPELINE_REPO = BENCH.parents[1]
VENV_PY = PIPELINE_REPO / ".venv" / "bin" / "python"
PY = str(VENV_PY) if VENV_PY.exists() else sys.executable

# Passes every token_bucket acceptance test (all of which pass `now` explicitly)
# but mishandles now=None by jumping to the far future and over-refilling --
# which only the ground-truth's test_now_none_reuses_last_time exercises.
GAMER = '''
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
            now = self.last + 1e9   # BUG: untracked time treated as far future
        elapsed = now - self.last
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last = now
        if tokens <= self.tokens:
            self.tokens -= tokens
            return True
        return False
'''


def _run(model, workdir, extra_env=None, trial=0, task="token_bucket"):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    subprocess.run(
        [PY, str(BENCH / "harness.py"), "--task", task, "--model", model,
         "--trial", str(trial), "--workdir", str(workdir),
         "--timeout", "120", "--tick", "1"],
        check=True, capture_output=True, text=True, env=env,
    )
    cell = Path(workdir) / f"{task}__{model}__t{trial}"
    return json.loads((cell / "result.json").read_text())


def test_mock_good_drives_to_done_and_passes_groundtruth(tmp_path):
    r = _run("mock", tmp_path)
    assert r["final_status"] == "done"
    assert r["merged"] is True
    assert r["review_verdict"] == "APPROVE"
    assert r["groundtruth_passed"] is True
    assert r["timed_out"] is False


def test_mock_ratelimiter_bugfix_seeds_existing_files_and_drives_to_done(tmp_path):
    """End-to-end proof for the Tier 2 ("modify existing code") mechanism:
    the seeded ratelimiter.py/test_ratelimiter.py land in the initial
    commit, the mock backend's fix (overwriting only ratelimiter.py, never
    touching the seeded test file) drives the story through review and
    merge, and BOTH the seeded existing tests and the new acceptance/
    groundtruth oracles pass against the merged result - proving seed files
    survive the whole dispatch -> review -> merge pipeline intact."""
    r = _run("mock", tmp_path, task="ratelimiter_bugfix")
    assert r["final_status"] == "done"
    assert r["merged"] is True
    assert r["review_verdict"] == "APPROVE"
    assert r["groundtruth_passed"] is True

    cell = Path(tmp_path) / "ratelimiter_bugfix__mock__t0"
    merged_test_file = (cell / "repo" / "test_ratelimiter.py").read_text()
    assert "test_starts_full" in merged_test_file
    assert "test_single_refill_after_time_passes" in merged_test_file


def test_groundtruth_catches_oracle_gaming_impl(tmp_path):
    gamer = tmp_path / "gamer.py"
    gamer.write_text(GAMER)
    r = _run("mock", tmp_path, extra_env={"BENCH_MOCK_IMPL_FILE": str(gamer)},
             trial=1)
    # The gamer passes the visible acceptance oracle, so the pipeline still
    # merges it...
    assert r["merged"] is True
    assert r["review_verdict"] == "APPROVE"
    # ...but the INDEPENDENT ground-truth exposes it as wrong.
    assert r["groundtruth_ran"] is True
    assert r["groundtruth_passed"] is False


# Passes the visible acceptance oracle for ratelimiter_inspect (which never
# exercises the no-mutation contract discriminately, matching how the
# existing token_bucket acceptance suite doesn't catch every groundtruth-only
# bug either) but reuses allow()'s mutate-and-store refill logic inside
# available_tokens() instead of a pure read -- only groundtruth's speculative-
# future-peek test exposes it.
INSPECTOR_GAMER = '''
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

    def available_tokens(self, now=None):
        # BUG: mutates self.tokens/self.last instead of a pure read.
        if now is None:
            now = self.last
        elapsed = now - self.last
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last = now
        return self.tokens
'''


def test_mock_ratelimiter_inspect_drives_to_done_and_passes_groundtruth(tmp_path):
    r = _run("mock", tmp_path, task="ratelimiter_inspect")
    assert r["final_status"] == "done"
    assert r["merged"] is True
    assert r["review_verdict"] == "APPROVE"
    assert r["groundtruth_passed"] is True
    assert r["timed_out"] is False


def test_ratelimiter_inspect_groundtruth_catches_mutating_inspector_gamer(tmp_path):
    gamer = tmp_path / "inspector_gamer.py"
    gamer.write_text(INSPECTOR_GAMER)
    r = _run("mock", tmp_path, extra_env={"BENCH_MOCK_IMPL_FILE": str(gamer)},
             trial=1, task="ratelimiter_inspect")
    # The gamer passes the visible acceptance oracle, so the pipeline still
    # merges it...
    assert r["merged"] is True
    assert r["review_verdict"] == "APPROVE"
    # ...but the INDEPENDENT ground-truth exposes the mutating inspector.
    assert r["groundtruth_ran"] is True
    assert r["groundtruth_passed"] is False
