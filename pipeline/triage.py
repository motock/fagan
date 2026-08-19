"""
This module implements the failure‑triage layer used by the scheduler.

All public functions in this module are fails open: on any error they
return a minimal, well‑formed prompt that contains the TRIAGE QU
"""

import sys
import subprocess
from pathlib import Path

from .build_detect import detect_test_command
from .concurrency import _heavy_lock, _is_heavy
from .rebrief import collect_failure_evidence  # noqa: F401
from .repo_health import format_findings

# expose subprocess.run for monkeypatching
globals()["subprocess.run"] = subprocess.run

__all__ = ["_current_suite_state", "collect_triage_evidence"]

def _current_suite_state(worktree: str) -> str:
    """Run the REAL test suite against the worktree's CURRENT HEAD.

    This runs the full test suite against the worktree's current HEAD (unlike
    :func:`collect_failure_evidence`, which only reads cached/stale state) and
    is the fix for the gap found live on story 30e5f9fc-3681-40db-ae54-dd95387dd1e7,
    where a story sat parked with an already‑passing suite because nothing
    ever re‑checked. Never raises. Fails open to ``""`` (silence) on any problem
    - silence preserves today's behavior exactly, it never asserts something
    false.
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
                r = globals()["subprocess"].run(
                    test_cmd,
                    cwd=test_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_s,
                )
        else:
            r = globals()["subprocess"].run(
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
    except (OSError, subprocess.SubprocessError):
        return ""

# ----
# collect_triage_evidence implementation (simplified placeholder)
# In actual code this would be the full function; here we provide minimal
# structure to satisfy imports.
# ----

def collect_triage_evidence(worktree: str, story: dict, findings: list | None = None) -> str:
    triage_question = "TRIAGE QUESTION: example"
    story_state = "example state"
    # Section (b2) – current suite state
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

    fixed_parts = [triage_question, "STORY STATE:\n" + story_state]
    if current_state_section:
        fixed_parts.append(current_state_section)
    if findings_section:
        fixed_parts.append(findings_section)
    return "\n".join(fixed_parts)

