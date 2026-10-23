import os
import subprocess
from pathlib import Path

_BASELINE_TEST_TIMEOUT_S = 900


def _baseline_test_env() -> dict[str, str]:
    """The dispatch process's env minus the operational overrides that
    false-fail a suite in a repo that vendors the pipeline's own tests.

    Mirrors the grading gate's env (story_status.py's test_env: the same
    PIPELINE_*/LOCAL_AGENT_*/REPO_ROOT strips) so the baseline snapshot and
    the real gate agree on what "failing" means. The server carries
    PIPELINE_* config (pause/resume thresholds, backend dispatch, model
    defaults) that overrides the defaults the suite asserts against, and
    REPO_ROOT is a per-plan sentinel (/nonexistent-...) that isn't a
    developer default - either one surviving into the run false-fails the
    suite for every story in the pipeline repo itself.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }


def _run_baseline_test_snapshot(worktree_path: Path) -> dict | None:
    """Run the detected test command once against a freshly created
    worktree, BEFORE the dispatched agent (or the test-author/planner
    phases) touch anything, and return its result in the same shape
    story_status.py's last_test_check already uses. Used to tell the
    agent up front which failures (if any) predate its own changes, so it
    doesn't spend budget investigating or "fixing" something it didn't
    cause. Observed live 2026-09-17: 4 separate stories independently
    rediscovered the same pre-existing failure pattern from scratch.
    Never raises; returns None on any failure (missing worktree, no
    detectable test command that can run, etc.) -- this is purely
    informational and must never block or slow down a normal dispatch on
    a repo where nothing is wrong."""
    from .build_detect import detect_test_command, failed_node_ids
    if not worktree_path.is_dir():
        return None
    test_dir, test_cmd = detect_test_command(worktree_path)
    # detect_test_command's documented no-op for a repo with no build system
    # at all (`[sys.executable, "-c", "pass"]`): there is no test suite to
    # snapshot, so spawning it would be a pointless extra subprocess on every
    # fresh dispatch of such a repo (and a spurious non-git spawn for callers
    # that assert on the subprocesses a dispatch makes).
    if len(test_cmd) == 3 and test_cmd[1:] == ["-c", "pass"]:
        return None
    try:
        r = subprocess.run(
            test_cmd, check=False, cwd=test_dir, capture_output=True, text=True,
            env=_baseline_test_env(), timeout=_BASELINE_TEST_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # Fail open: a suite that outruns the bound tells us nothing about
        # whether it was ALREADY failing, and the snapshot must never hold the
        # dispatch path open indefinitely (the docstring's "never block or
        # slow down a normal dispatch").
        return None
    return {
        "cmd": test_cmd,
        "returncode": r.returncode,
        "stdout_tail": (r.stdout or "")[-1000:],
        "failed_node_ids": failed_node_ids(r.stdout or ""),
        "stderr_tail": (r.stderr or "")[-1000:],
    }

