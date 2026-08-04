"""Supplementary coverage for the worktree-hygiene rebrief guidance, filling
gaps the acceptance oracle (test_acceptance_rebrief_wire_cleanup.py) doesn't
grade: the exact CLEANUP_HEADER contract, __all__ membership, that
pipeline.server actually imports append_cleanup_guidance (not just that
pipeline.rebrief defines it), and content-level checks that the guidance
text says something actionable rather than being an empty stub.

Do NOT modify test_acceptance_rebrief_wire_cleanup.py, test_acceptance_
rebrief_wire_step_cap.py, or test_acceptance_rebrief_compose.py - those
encode a separate, protected contract.
"""
import inspect

from pipeline import rebrief
from pipeline import server as p


def test_cleanup_header_has_the_exact_required_value():
    assert rebrief.CLEANUP_HEADER == "=== WORKTREE HYGIENE (read this too) ==="


def test_cleanup_header_and_helper_are_exported_in_all():
    assert "CLEANUP_HEADER" in rebrief.__all__
    assert "append_cleanup_guidance" in rebrief.__all__


def test_server_module_imports_append_cleanup_guidance_from_rebrief():
    # server.py must import the name directly (not merely reach it via
    # pipeline.rebrief.append_cleanup_guidance), and it must be the same
    # function object - not a reimplementation.
    assert hasattr(p, "append_cleanup_guidance")
    assert p.append_cleanup_guidance is rebrief.append_cleanup_guidance


def test_guidance_block_mentions_checking_git_status_against_default_branch():
    out = rebrief.append_cleanup_guidance("GOAL: x")
    block = out.split(rebrief.CLEANUP_HEADER, 1)[1]
    assert "git status" in block or "git diff" in block


def test_guidance_block_warns_about_stray_test_files_breaking_review():
    out = rebrief.append_cleanup_guidance("GOAL: x")
    block = out.split(rebrief.CLEANUP_HEADER, 1)[1].lower()
    assert "test file" in block or "stray" in block
    assert "review" in block


def test_guidance_block_explains_the_checkpoint_commits_wip_including_experiments():
    out = rebrief.append_cleanup_guidance("GOAL: x")
    block = out.split(rebrief.CLEANUP_HEADER, 1)[1].lower()
    assert "interrupted" in block or "earlier" in block


def test_cleanup_guidance_applied_after_a_diagnosis_block_preserves_the_diagnosis():
    # Real call order in check_story_status: compose_rebriefed_instructions
    # (diagnosis) runs first, THEN append_cleanup_guidance runs on the
    # result. The diagnosis block must survive the cleanup block being
    # (re)appended on top of it.
    with_diagnosis = rebrief.compose_rebriefed_instructions(
        "GOAL: x", "root cause here")
    out = rebrief.append_cleanup_guidance(with_diagnosis)
    assert rebrief.DIAGNOSIS_HEADER in out
    assert "root cause here" in out
    assert "GOAL: x" in out
    assert out.count(rebrief.CLEANUP_HEADER) == 1

    # A second rework cycle re-runs both composers again on the FULL prior
    # result (diagnosis+cleanup persisted from cycle 1, mirroring how
    # agent_instructions survives across resumes) - still exactly one of
    # each block, no stacking.
    with_diagnosis_2 = rebrief.compose_rebriefed_instructions(
        out, "second root cause")
    out2 = rebrief.append_cleanup_guidance(with_diagnosis_2)
    assert out2.count(rebrief.CLEANUP_HEADER) == 1
    assert out2.count(rebrief.DIAGNOSIS_HEADER) == 1
    assert "second root cause" in out2
    assert "root cause here" not in out2


def test_check_story_status_calls_append_cleanup_guidance_after_step_cap_rebrief():
    src = inspect.getsource(p.check_story_status)
    rebrief_idx = src.index("_rebrief_step_cap_struggle")
    cleanup_idx = src.index("append_cleanup_guidance")
    assert cleanup_idx > rebrief_idx, (
        "append_cleanup_guidance must be wired in AFTER the "
        "_rebrief_step_cap_struggle call in the step-cap branch"
    )
