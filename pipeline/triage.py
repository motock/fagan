"""
This module implements the failure‑triage layer used by the scheduler.

All public functions in this module are fails open: on any error they
return a minimal, well‑formed prompt that contains the TRIAGE QU
and the park‑and‑notify invariant.
"""

import os
import subprocess
from pathlib import Path

from .build_detect import detect_test_command
from .concurrency import _heavy_lock, _is_heavy
from .config import STEP_CAP_FALLBACK_THRESHOLD
from .rebrief import collect_failure_evidence
from .repo_health import format_findings

# expose subprocess.run for monkeypatching
globals()["subprocess.run"] = subprocess.run

__all__ = ["_auto_triage_enabled", "_current_suite_state", "collect_triage_evidence", "triage_candidates"]
def _auto_triage_enabled() -> bool:
    """Return True if PIPELINE_AUTO_TRIAGE is set to a truthy value."""
    override = os.environ.get("PIPELINE_AUTO_TRIAGE", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    return False


def triage_candidates(stories: dict) -> list[str]:
    """Return sorted list of story keys that should be triaged."""
    candidates = []
    for key, story in stories.items():
        status = story.get("status")
        if status in ("parked", "failed"):
            candidates.append(key)
            continue
        streak = story.get("step_cap_streak", 0)
        if not isinstance(streak, int):
            streak = 0
        if streak >= STEP_CAP_FALLBACK_THRESHOLD:
            candidates.append(key)
    return sorted(candidates)
# ---------------------------------------------------------------------------
# Helper: run the real test suite against the worktree's CURRENT HEAD
# ---------------------------------------------------------------------------

def _current_suite_state(worktree: str) -> str:
    """Run the REAL test suite against the worktree's CURRENT HEAD.

    This runs the full test suite against the worktree's current HEAD (unlike
    :func:`collect_failure_evidence`, which only reads cached/stale state) and
    is the fix for the gap found live on story 30e5f9fc-3681-40db-ae54-dd95387dd1e7,
    where a story sat parked with an already‑passing suite because nothing
    ever re‑checked. Never raises. Fails open to ``""`` (silence) on any problem - silence preserves today's behavior exactly, it never asserts something false.
    """
    if not worktree:
        return ""
    raw_timeout = os.environ.get("PIPELINE_TRIAGE_SUITE_TIMEOUT", "").strip()
    try:
        timeout_s = int(raw_timeout)
    except (TypeError, ValueError):
        timeout_s = 240
    if timeout_s <= 0:
        return ""
    try:
        test_dir, test_cmd = detect_test_command(Path(worktree))
        if not test_cmd:
            return ""
        needs_heavy = bool(test_cmd) and _is_heavy(test_cmd)
        if needs_heavy:
            with _heavy_lock():
                r = globals()["subprocess.run"](
                    test_cmd,
                    cwd=test_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_s,
                )
        else:
            r = globals()["subprocess.run"](
                test_cmd,
                cwd=test_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s,
            )
        if r.returncode == 0:
            return "CURRENT STATE: full test suite PASSES at the worktree's current HEAD."
        if r.returncode == 5:
            return ""
        return f"CURRENT STATE: full test suite FAILS at the worktree's current HEAD (rc={r.returncode}):\n{(r.stdout + r.stderr)[-500:]}"
    except Exception:  # noqa: BLE001
        return ""

# ---------------------------------------------------------------------------
# Main triage evidence collection
# ---------------------------------------------------------------------------

def collect_triage_evidence(worktree: str, story: dict, findings: list | None = None, limit: int = 8000) -> str:
    triage_question = f"TRIAGE QUESTION: this story is terminal (status={story.get('status', '?')}). Decide what to do about it."
    story_state = "\n".join(
        f"{k}: {story.get(k, '-') }"
        for k in [
            "status",
            "parked_reason",
            "backend",
            "model",
            "dispatched_model",
            "escalated",
            "risk",
            "persona",
            "dispatch_attempts",
            "rework_attempts",
            "review_inconclusive_count",
            "step_cap_streak",
            "merge_attempts",
            "triage_attempts",
            "triage_actions",
        ]
    )
    # Section (b2) – current suite state: a LIVE check against the
    # worktree's current HEAD, not cached/stale state (see
    # _current_suite_state's docstring for why this exists).
    current_state_section = ""
    try:
        current_state_section = _current_suite_state(worktree)
    except Exception:  # noqa: BLE001
        current_state_section = ""
    # Section (c) – repo‑health findings
    findings_section = ""
    if findings:
        try:
            findings_section = format_findings(findings)
        except Exception:  # noqa: BLE001
            findings_section = ""
    # Compute the length of the fixed sections
    fixed_parts = [triage_question, "STORY STATE:\n" + story_state]
    if current_state_section:
        fixed_parts.append(current_state_section)
    if findings_section:
        fixed_parts.append(findings_section)
    fixed_text = "\n".join(fixed_parts)
    remaining = max(0, limit - len(fixed_text))
    try:
        failure_evidence = collect_failure_evidence(worktree, story, limit=remaining)
    except Exception:  # noqa: BLE001
        failure_evidence = ""
    if failure_evidence:
        result = (fixed_text + "\n" + failure_evidence)[:limit]
    else:
        result = fixed_text[:limit]
    return result

