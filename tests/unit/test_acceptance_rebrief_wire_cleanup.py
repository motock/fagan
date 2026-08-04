"""Acceptance oracle: a step-cap resume must be warned about worktree
hygiene (stray files left by the interrupted attempt) unconditionally -
not just when the diagnosis role happens to succeed. Grounded in a live
incident: DASHBOARD_PROGRESS_TIER1's frontend-progress-bar rebrief
(2026-08-04) shipped a stray test file from the interrupted first attempt,
which broke the review gate's test collection and required manual manifest
surgery to recover.

Source-inspection for the wiring check, mirroring
test_acceptance_rebrief_wire_step_cap.py - grading the call site (not just
the pure helper) is the point: a helper nothing calls fixes nothing.
"""
import inspect

from pipeline import rebrief
from pipeline import server as p


def test_append_cleanup_guidance_adds_the_header():
    out = rebrief.append_cleanup_guidance("GOAL: build the thing.")
    assert rebrief.CLEANUP_HEADER in out
    assert "GOAL: build the thing." in out


def test_append_cleanup_guidance_is_idempotent_not_stacked():
    once = rebrief.append_cleanup_guidance("GOAL: x")
    twice = rebrief.append_cleanup_guidance(once)
    assert twice.count(rebrief.CLEANUP_HEADER) == 1
    assert "GOAL: x" in twice


def test_append_cleanup_guidance_handles_empty_instructions():
    out = rebrief.append_cleanup_guidance("")
    assert rebrief.CLEANUP_HEADER in out


def test_check_story_status_step_cap_branch_calls_append_cleanup_guidance():
    src = inspect.getsource(p.check_story_status)
    assert "append_cleanup_guidance" in src
