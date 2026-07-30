"""Tests for the Mode 30 resolution doc edits.

This is a docs-only story touching exactly two files:

1. ``retros/tdd-split-unconditional-and-review-race_2026-07-21.md`` — a new
   dated ``## RESOLUTION 2026-07-27`` section must be *appended* (the original
   findings above it must remain intact) stating that Mode 30's working-tree-
   mutating ops were removed, dispatch now creates worktrees directly from
   ``origin/<branch>`` via ``git fetch`` + ``git worktree add ... origin/<branch>``,
   the remaining .git-only write (the fetch) is guarded by a non-blocking
   advisory ``flock`` (``_try_acquire_git_lock`` in ``pipeline/server.py``),
   and ``pause_plan`` before manual git surgery is now defense-in-depth rather
   than load-bearing. It must reference the actual PR numbers that landed the
   core fix and the advisory-lock fix.

2. ``MATURITY_AND_UNIQUENESS_PLANS.md`` — the Mode 30 bullet under section A3
   (currently ``[ ]``) must be flipped to ``[x]`` with a short one-line note
   pointing at the resolving PR(s). No other checklist item may change.

These tests assert the *content* of those prose edits. They are RED until the
doc edits land (the implementation dispatch), which is the intended state.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

RETRO_PATH = REPO_ROOT / "retros" / "tdd-split-unconditional-and-review-race_2026-07-21.md"
MATURITY_PATH = REPO_ROOT / "docs" / "plans" / "MATURITY_AND_UNIQUENESS_PLANS.md"

# PR numbers that landed the Mode 30 fixes, taken from `git log --oneline` on
# master:
#   86b41d9 fix(dispatch): create worktrees from origin, drop working-tree mutation (#177)
#   483c70f fix(dispatch): guard scheduler's git fetch with a non-blocking advisory lock (#178)
CORE_FIX_PR = "#177"
ADVISORY_LOCK_PR = "#178"
RESOLUTION_DATE = "2026-07-27"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _retro_text() -> str:
    assert RETRO_PATH.exists(), f"retro file missing: {RETRO_PATH}"
    return RETRO_PATH.read_text(encoding="utf-8")


def _maturity_text() -> str:
    assert MATURITY_PATH.exists(), f"maturity file missing: {MATURITY_PATH}"
    return MATURITY_PATH.read_text(encoding="utf-8")


def _resolution_section(text: str) -> str:
    """Return the text of the appended RESOLUTION section (everything from the
    ``## RESOLUTION`` heading to EOF), or empty string if absent."""
    m = re.search(r"^## RESOLUTION[^\n]*\n(.*)\Z", text, re.DOTALL | re.MULTILINE)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Retro file — original findings preserved
# ---------------------------------------------------------------------------

class TestRetroOriginalFindingsPreserved:
    """The original findings above the RESOLUTION section must not be deleted
    or rewritten."""

    def test_retro_file_exists(self):
        _retro_text()  # asserts existence

    def test_original_timeline_section_intact(self):
        text = _retro_text()
        assert "## 1. Timeline" in text, "original Timeline section was removed"

    def test_original_learnings_section_intact(self):
        text = _retro_text()
        assert "## 2. Learnings" in text, "original Learnings section was removed"

    def test_mode30_learning_L5_intact(self):
        text = _retro_text()
        assert "Mode 30" in text
        assert "L5" in text, "original L5 (Mode 30) learning was removed"

    def test_improvements_section_intact(self):
        text = _retro_text()
        assert "## 3. Concrete improvements, prioritized" in text

    def test_what_went_well_section_intact(self):
        text = _retro_text()
        assert "## 4. What went well" in text

    def test_resolution_is_appended_not_replacing_findings(self):
        """The RESOLUTION heading must come *after* the original sections."""
        text = _retro_text()
        idx_timeline = text.find("## 1. Timeline")
        idx_resolution = text.find("## RESOLUTION")
        assert idx_timeline != -1, "original Timeline section missing"
        assert idx_resolution != -1, "RESOLUTION section missing"
        assert idx_resolution > idx_timeline, (
            "RESOLUTION section must be appended after the original findings, "
            "not placed before/instead of them"
        )


# ---------------------------------------------------------------------------
# Retro file — RESOLUTION section content
# ---------------------------------------------------------------------------

class TestRetroResolutionSection:
    """The appended RESOLUTION section must state the specific fixes."""

    def test_resolution_heading_present_and_dated(self):
        text = _retro_text()
        assert re.search(
            r"^## RESOLUTION\s+" + re.escape(RESOLUTION_DATE) + r"\b",
            text,
            re.MULTILINE,
        ), f"missing '## RESOLUTION {RESOLUTION_DATE}' heading"

    def test_resolution_mentions_removed_merge_ff_only(self):
        section = _resolution_section(_retro_text())
        assert "git merge --ff-only" in section, (
            "RESOLUTION must mention the removed `git merge --ff-only`"
        )
        assert "_sync_local_default_branch" in section, (
            "RESOLUTION must name the now-deleted `_sync_local_default_branch`"
        )

    def test_resolution_mentions_removed_pull_ff_only(self):
        section = _resolution_section(_retro_text())
        assert "git pull --ff-only" in section, (
            "RESOLUTION must mention the removed `git pull --ff-only` previously in dispatch"
        )

    def test_resolution_mentions_worktree_from_origin(self):
        section = _resolution_section(_retro_text())
        assert "git fetch" in section, "RESOLUTION must mention `git fetch`"
        assert "git worktree add" in section, (
            "RESOLUTION must mention `git worktree add`"
        )
        assert "origin/" in section, (
            "RESOLUTION must mention creating worktrees from `origin/<branch>`"
        )

    def test_resolution_mentions_advisory_flock(self):
        section = _resolution_section(_retro_text())
        assert "flock" in section.lower() or "flock" in section, (
            "RESOLUTION must mention the advisory flock"
        )
        assert "_try_acquire_git_lock" in section, (
            "RESOLUTION must name `_try_acquire_git_lock`"
        )
        assert "pipeline/server.py" in section, (
            "RESOLUTION must locate `_try_acquire_git_lock` in pipeline/server.py"
        )

    def test_resolution_states_non_blocking(self):
        section = _resolution_section(_retro_text())
        assert "non-blocking" in section.lower(), (
            "RESOLUTION must state the advisory lock is non-blocking"
        )

    def test_resolution_states_concurrent_human_not_raced(self):
        section = _resolution_section(_retro_text())
        # Must convey that a concurrent human git operation is not raced.
        assert "concurrent" in section.lower(), (
            "RESOLUTION must address concurrent human git operations"
        )
        assert "human" in section.lower(), (
            "RESOLUTION must reference a human git operation"
        )

    def test_resolution_states_pause_plan_defense_in_depth(self):
        section = _resolution_section(_retro_text())
        assert "pause_plan" in section, (
            "RESOLUTION must reference `pause_plan`"
        )
        assert "defense-in-depth" in section.lower() or "defense in depth" in section.lower(), (
            "RESOLUTION must state pause_plan is now defense-in-depth, not load-bearing"
        )
        assert "load-bearing" in section.lower(), (
            "RESOLUTION must contrast defense-in-depth against load-bearing"
        )

    def test_resolution_references_core_fix_pr(self):
        section = _resolution_section(_retro_text())
        assert CORE_FIX_PR in section, (
            f"RESOLUTION must reference the core-fix PR {CORE_FIX_PR}"
        )

    def test_resolution_references_advisory_lock_pr(self):
        section = _resolution_section(_retro_text())
        assert ADVISORY_LOCK_PR in section, (
            f"RESOLUTION must reference the advisory-lock PR {ADVISORY_LOCK_PR}"
        )

    def test_resolution_references_both_prs(self):
        section = _resolution_section(_retro_text())
        assert CORE_FIX_PR in section and ADVISORY_LOCK_PR in section


# ---------------------------------------------------------------------------
# Maturity file — Mode 30 bullet flipped
# ---------------------------------------------------------------------------

class TestMaturityMode30Bullet:
    """The Mode 30 bullet under A3 must be flipped to [x] with a PR note."""

    def _mode30_bullet_line(self, text: str) -> str:
        """Return the full logical bullet (the line starting with the
        checkbox) for the Mode 30 P0 item under A3."""
        # The bullet starts with `- [ ]` or `- [x]` and references Mode 30.
        for m in re.finditer(r"^(\- \[[ xX]\][^\n]*\n(?:[ \t]+[^\n]*\n)*)", text, re.MULTILINE):
            block = m.group(1)
            if "Mode" in block and "30" in block and "P0" in block:
                return block
        pytest.fail("could not locate the Mode 30 P0 bullet under A3")

    def test_mode30_bullet_is_checked(self):
        text = _maturity_text()
        block = self._mode30_bullet_line(text)
        first_line = block.splitlines()[0]
        assert first_line.startswith("- [x]"), (
            f"Mode 30 bullet must be flipped to [x], got: {first_line!r}"
        )

    def test_mode30_bullet_references_resolving_prs(self):
        text = _maturity_text()
        block = self._mode30_bullet_line(text)
        assert CORE_FIX_PR in block or ADVISORY_LOCK_PR in block, (
            "Mode 30 bullet must include a one-line note pointing at the resolving PR(s)"
        )

    def test_mode30_bullet_note_is_short(self):
        """The added note should be a short one-liner, not a multi-paragraph
        expansion. The first line of the bullet (the checkbox line) should
        contain the PR reference."""
        text = _maturity_text()
        block = self._mode30_bullet_line(text)
        first_line = block.splitlines()[0]
        assert CORE_FIX_PR in first_line or ADVISORY_LOCK_PR in first_line, (
            "the PR note should appear on the checkbox line itself (short one-liner)"
        )


# ---------------------------------------------------------------------------
# Maturity file — no other checklist items changed
# ---------------------------------------------------------------------------

class TestMaturityOtherChecklistItemsUntouched:
    """Flipping Mode 30 must not touch any other checklist item. We assert
    that every *other* `[ ]`/`[x]` bullet retains its original checkbox state
    by checking a representative sample of known-unrelated items that must
    remain unchecked, and known-checked items that must remain checked."""

    UNCHECKED_MUST_STAY_UNCHECKED: ClassVar[list[str]] = [
        "Bound the failure-mode discovery rate",
        "Mode 31 (2026-07-22, NOT fixed)",
        "Mode 32 (2026-07-22, NOT fixed)",
        "Get CI to an enforced green baseline",
        "One-command install story",
    ]

    CHECKED_MUST_STAY_CHECKED: ClassVar[list[str]] = [
        "Finish the `pipeline_mcp_server.py` decomposition",
        "Ship or kill guided decomposition",
        "Split the 74 KB README",
        "Fix the test-isolation leak",
    ]

    def test_known_unchecked_items_still_unchecked(self):
        text = _maturity_text()
        for needle in self.UNCHECKED_MUST_STAY_UNCHECKED:
            # find the bullet line containing this needle
            m = re.search(r"^(- \[[ xX]\])[^\n]*" + re.escape(needle), text, re.MULTILINE)
            assert m is not None, f"could not find checklist item: {needle!r}"
            assert m.group(1) == "- [ ]", (
                f"checklist item {needle!r} must remain unchecked, got {m.group(1)!r}"
            )

    def test_known_checked_items_still_checked(self):
        text = _maturity_text()
        for needle in self.CHECKED_MUST_STAY_CHECKED:
            m = re.search(r"^(- \[[ xX]\])[^\n]*" + re.escape(needle), text, re.MULTILINE)
            assert m is not None, f"could not find checklist item: {needle!r}"
            assert m.group(1) == "- [x]", (
                f"checklist item {needle!r} must remain checked, got {m.group(1)!r}"
            )

    def test_mode29_bullet_still_checked(self):
        """Mode 29 (the sibling P0 in the same retro) was already fixed and
        must remain checked — flipping Mode 30 must not disturb it."""
        text = _maturity_text()
        block = self._find_bullet_block_containing(text, "Mode 29")
        assert block is not None, "could not find Mode 29 bullet"
        first_line = block.splitlines()[0]
        assert first_line.startswith("- [x]"), (
            f"Mode 29 bullet must remain [x], got: {first_line!r}"
        )

    @staticmethod
    def _find_bullet_block_containing(text: str, needle: str) -> str | None:
        """Return the full logical bullet block (checkbox line + indented
        continuation lines) whose text contains `needle`."""
        for m in re.finditer(
            r"^(- \[[ xX]\][^\n]*\n(?:[ \t]+[^\n]*\n)*)",
            text,
            re.MULTILINE,
        ):
            if needle in m.group(1):
                return m.group(1)
        return None