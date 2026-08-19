"""
This module implements the failure‑triage layer used by the scheduler.

All public functions in this module are fails open: on any error they
return a minimal, well‑formed prompt that contains the TRIAGE QUESTION and
STORY STATE sections.  The default behaviour for any error path is to
park the story and notify the user (C2 of
`docs/plans/OVERLORD_FAILURE_TRIAGE_PLAN.md`).  The design mirrors
`pipeline.rebrief.diagnose_failure` which also returns ``None`` on error.

The main entry point is :func:`collect_triage_evidence` which builds a
bounded prompt string from the supplied story, optional repo‑health
findings, and the failure evidence collected by
``pipeline.rebrief.collect_failure_evidence``.
"""

from __future__ import annotations

# Safe imports – these modules do not import ``pipeline.server`` at module
# level.
from .rebrief import collect_failure_evidence
from .repo_health import format_findings

__all__ = ["collect_triage_evidence"]

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

        # Section (c) – repo‑health findings
        findings_section = ""
        if findings:
            try:
                findings_section = format_findings(findings)
            except Exception:  # noqa: BLE001
                findings_section = ""

        # Compute the length of the fixed sections
        fixed_parts = [triage_question, "STORY STATE:\n" + story_state]
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
