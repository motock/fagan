"""Manifest-mutation helpers for check_story_status's grade path.

Extracted verbatim from pipeline/story_status.py (behavior-preserving move);
story_status re-exports both names.
"""

import os
import subprocess
from datetime import datetime, timezone


def _record_test_check(
    story: dict, test_cmd: list[str], test_dir, test_result, worktree: str
) -> str | None:
    """Persist this run's test result on the story, regardless of pass/fail.

    Returns the worktree's HEAD sha (or None when it cannot be read) so the
    caller can stamp the lint/dead-code caches with the same revision.
    """
    check_sha = None
    if worktree and os.path.isdir(worktree):
        try:
            check_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, OSError):
            check_sha = None
    story["last_test_check"] = {
        "cmd": test_cmd,
        "cwd": str(test_dir),
        "returncode": test_result.returncode,
        "stdout_tail": (test_result.stdout or "")[-2000:],
        "stderr_tail": (getattr(test_result, "stderr", "") or "")[-2000:],
        "ts": datetime.now(timezone.utc).isoformat(),
        "sha": check_sha,
    }
    return check_sha


def _clear_failure_streaks(story: dict) -> None:
    """Clear the consecutive-failure streaks a completed grade breaks."""
    for _streak_key in (
        "dispatch_attempts",
        "watchdog_streak",
        "step_cap_streak",
        "step_cap_streak_model",
        "infra_failure_streak",
        "infra_failure_streak_model",
    ):
        story.pop(_streak_key, None)
