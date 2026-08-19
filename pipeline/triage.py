"""
This module implements the failure‑triage layer used by the scheduler.

All public functions in this module are fails open: on any error they
return a minimal, well‑formed prompt that contains the TRIAGE QUESTION and
STORY STATE sections.  The default behaviour for any error path is to
park the story and notify the user (C2 of
`docs/plans/OVERLORD_FAILURE_TRIAGE_PLAN.md`).  The design mirrors
`pipeline.rebrief.diagnose_failure` which also returns ``None`` on error.
"""

from __future__ import annotations

# Safe imports – these modules do not import ``pipeline.server`` at module
# level.
import os
import subprocess
from pathlib import Path

from .build_detect import detect_test_command
from .concurrency import _heavy_lock, _is_heavy
from .rebrief import collect_failure_evidence
from .repo_health import format_findings

globals()["subprocess.run"] = subprocess.run

__all__ = ["_current_suite_state", "collect_triage_evidence"]

# ---------------------------------------------------------------------------
# Helper constants
# ---------------------------------------------------------------------------
# Ordered list of keys to include in the STORY STATE block.
_STORY_STATE_KEYS = [
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

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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
                r = subprocess.run(
                    test_cmd,
                    cwd=test_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_s,
                )
        else:
            r = subprocess.run(
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
        return (
            f"CURRENT STATE: full test suite FAILS at the worktree's current HEAD (rc={r.returncode}):\n"
            f"{(r.stdout + r.stderr)[-500:]}"
        )
    except (OSError, subprocess.SubprocessError):
        return ""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_triage_evidence(
    worktree: str,
    story: dict,
    findings: list | None = None,
    limit: int = 8000,
) -> str:
    """Return a bounded prompt string for triage.

    Parameters
    ----------
    worktree:
        Path to the repository worktree.
    story:
        Dictionary containing story metadata.
    findings:
        Optional list of repo‑health findings.  If ``None`` or empty the
        section is omitted.
    limit:
        Maximum number of characters in the returned string.

    The function never raises.  On any exception it returns a minimal
    string that still contains the ``TRIAGE QUESTION`` and ``STORY STATE``
    sections.
    """

    try:
        # Section (a) – TRIAGE QUESTION
        status = story.get("status", "?")
        triage_question = (
            f"TRIAGE QUESTION: this story is terminal (status={status}). Decide what to do about it."
        )

        # Section (b) – STORY STATE block
        state_lines = []
        for key in _STORY_STATE_KEYS:
            value = story.get(key, "-")
            state_lines.append(f"{key}: {value}")
        story_state = "\n".join(state_lines)

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
        fixed_text = "\n\n".join(fixed_parts)
        fixed_len = len(fixed_text)

        # Section (d) – failure evidence
        remaining = max(0, limit - fixed_len)
        try:
            failure_evidence = collect_failure_evidence(
                worktree, story, limit=remaining
            )
        except Exception:  # noqa: BLE001
            failure_evidence = ""

        # Assemble final string
        full_text = f"{fixed_text}\n\n{failure_evidence}" if failure_evidence else fixed_text
        if len(full_text) > limit:
            # Trim only the failure evidence part if over budget.
            excess = len(full_text) - limit
            if failure_evidence:
                failure_evidence = failure_evidence[:-excess]
                full_text = f"{fixed_text}\n\n{failure_evidence}"
            else:
                full_text = full_text[:limit]

        return full_text

    except Exception:  # noqa: BLE001
        # Fallback minimal string – still contains the required sections.
        fallback_status = story.get("status", "?") if isinstance(story, dict) else "?"
        return (
            f"TRIAGE QUESTION: this story is terminal (status={fallback_status}). Decide what to do about it."
            "\n\nSTORY STATE:\n"
        )
