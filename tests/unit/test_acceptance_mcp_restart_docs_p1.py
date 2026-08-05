"""Tests for the A3 P1 "restart + reconnect the MCP server" doc bullet.

This is a docs-only story touching exactly one file:

``docs/plans/MATURITY_AND_UNIQUENESS_PLANS.md`` — the P1 bullet (from
``retros/tdd-split-unconditional-and-review-race_2026-07-21.md``) about
making "restart + reconnect the MCP server" an explicit checked step must
flip from ``- [ ]`` to ``- [x]``, its existing prose (through "...getting
the stale result.") must remain byte-identical, and a new "Shipped:"
continuation line must be appended documenting that
``pipeline/self_modification.py`` now detects touches to
``pipeline/server.py`` / ``app/pipeline_mcp_server.py`` and both
``approve_merge`` and ``advance_pipeline``'s merge gate notify the operator
to run ``/mcp reconnect``, with fail-open behavior on a git error.

The bullet's immediate neighbors and a handful of other known items must
not change state as a side effect of the flip (checked below); this is not
a global invariant on the whole file, since the doc is expected to gain
and check off other items over time.

These tests assert the *content* of that prose edit. They are RED until the
doc edit lands (a later implementation dispatch), which is the intended
state — the file currently referenced by this test still has the bullet in
its original ``- [ ]`` form with no "Shipped:" continuation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

MATURITY_PATH = REPO_ROOT / "docs" / "plans" / "MATURITY_AND_UNIQUENESS_PLANS.md"

# The exact original explanatory prose of the bullet, which must survive the
# edit byte-identical (only the leading checkbox changes, plus a new
# "Shipped:" line is appended after it).
ORIGINAL_HEADING = (
    "**P1 (from `retros/tdd-split-unconditional-and-review-race_2026-07-21.md`)\n"
    "      — when a story changes `pipeline/server.py` or `pipeline_mcp_server.py`\n"
    "      itself, make \"restart + reconnect the MCP server\" an explicit, checked\n"
    "      step.**"
)
ORIGINAL_BODY = (
    "The running MCP server is a long-lived stdio child of the\n"
    "      `claude` CLI (not launchd-supervised, unlike the scheduler/usage-poller\n"
    "      jobs which get a fresh process per tick) and keeps executing pre-merge\n"
    "      code until manually killed — and killing it does not auto-reconnect the\n"
    "      session's tools. Discovered by manually testing the new\n"
    "      `mark_story_done` behavior and getting the stale result."
)
FINAL_ORIGINAL_SENTENCE = (
    "Discovered by manually testing the new\n"
    "      `mark_story_done` behavior and getting the stale result."
)

UNCHECKED_BULLET_PATTERN = re.compile(
    r"^- \[ \] \*\*P1 \(from `retros/tdd-split-unconditional-and-review-race_2026-07-21\.md`\)",
    re.MULTILINE,
)
CHECKED_BULLET_PATTERN = re.compile(
    r"^- \[x\] \*\*P1 \(from `retros/tdd-split-unconditional-and-review-race_2026-07-21\.md`\)",
    re.MULTILINE,
)

BULLET_BLOCK_PATTERN = re.compile(
    r"^- \[[ xX]\] \*\*P1 \(from `retros/tdd-split-unconditional-and-review-race_2026-07-21\.md`\)"
    r"[^\n]*\n(?:[ \t]+[^\n]*\n)*",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _maturity_text() -> str:
    assert MATURITY_PATH.exists(), f"maturity plan doc missing: {MATURITY_PATH}"
    return MATURITY_PATH.read_text(encoding="utf-8")


def _p1_bullet_block(text: str) -> str:
    """Return the full logical bullet block (checkbox line + all indented
    continuation lines, including any appended Shipped: note) for the MCP
    restart/reconnect P1 item."""
    m = BULLET_BLOCK_PATTERN.search(text)
    if m is None:
        pytest.fail(
            "could not locate the MCP restart/reconnect P1 bullet block "
            "(retros/tdd-split-unconditional-and-review-race_2026-07-21.md)"
        )
    return m.group(0)


def _shipped_continuation(block: str) -> str:
    """Return the text of the appended 'Shipped:' continuation, or '' if
    absent."""
    m = re.search(r"Shipped:.*\Z", block, re.DOTALL)
    return m.group(0) if m else ""


# ---------------------------------------------------------------------------
# The unchecked form must be gone
# ---------------------------------------------------------------------------


class TestUncheckedFormRemoved:
    def test_maturity_file_exists(self):
        _maturity_text()  # asserts existence

    def test_unchecked_p1_bullet_no_longer_present(self):
        text = _maturity_text()
        assert UNCHECKED_BULLET_PATTERN.search(text) is None, (
            "the unchecked `- [ ] **P1 (from `retros/tdd-split-unconditional-"
            "and-review-race_2026-07-21.md`)` bullet must no longer exist"
        )

    def test_unchecked_p1_bullet_count_is_zero(self):
        text = _maturity_text()
        assert len(UNCHECKED_BULLET_PATTERN.findall(text)) == 0


# ---------------------------------------------------------------------------
# The checked form must exist, with original prose preserved
# ---------------------------------------------------------------------------


class TestCheckedFormWithPreservedProse:
    def test_checked_p1_bullet_present(self):
        text = _maturity_text()
        assert CHECKED_BULLET_PATTERN.search(text) is not None, (
            "the `- [x] **P1 (from `retros/tdd-split-unconditional-and-"
            "review-race_2026-07-21.md`)` bullet must exist"
        )

    def test_checked_p1_bullet_count_is_exactly_one(self):
        text = _maturity_text()
        assert len(CHECKED_BULLET_PATTERN.findall(text)) == 1

    def test_original_heading_prose_byte_identical(self):
        text = _maturity_text()
        block = _p1_bullet_block(text)
        assert ORIGINAL_HEADING in block, (
            "the bullet's original heading prose (through 'checked step.**') "
            "must remain byte-identical"
        )

    def test_original_body_prose_byte_identical(self):
        text = _maturity_text()
        block = _p1_bullet_block(text)
        assert ORIGINAL_BODY in block, (
            "the bullet's original explanatory body prose must remain "
            "byte-identical"
        )

    def test_original_final_sentence_still_present(self):
        text = _maturity_text()
        block = _p1_bullet_block(text)
        assert FINAL_ORIGINAL_SENTENCE in block, (
            "the original explanatory sentence \"Discovered by manually "
            "testing the new `mark_story_done` behavior and getting the "
            "stale result.\" must still be present"
        )

    def test_block_starts_with_checked_box(self):
        text = _maturity_text()
        block = _p1_bullet_block(text)
        first_line = block.splitlines()[0]
        assert first_line.startswith("- [x]"), (
            f"P1 bullet must be flipped to [x], got: {first_line!r}"
        )


# ---------------------------------------------------------------------------
# The new "Shipped:" continuation
# ---------------------------------------------------------------------------


class TestShippedContinuation:
    def test_shipped_continuation_present(self):
        text = _maturity_text()
        block = _p1_bullet_block(text)
        assert "Shipped:" in block, (
            "a new 'Shipped:' continuation line must be appended to the bullet"
        )

    def test_shipped_continuation_follows_original_prose(self):
        """The Shipped: note must come after the original final sentence,
        i.e. be appended, not inserted before/instead of the existing prose."""
        text = _maturity_text()
        block = _p1_bullet_block(text)
        idx_final_sentence = block.find(FINAL_ORIGINAL_SENTENCE)
        idx_shipped = block.find("Shipped:")
        assert idx_final_sentence != -1, "original final sentence missing"
        assert idx_shipped != -1, "Shipped: continuation missing"
        assert idx_shipped > idx_final_sentence, (
            "the Shipped: note must be appended after the existing prose, "
            "not placed before/instead of it"
        )

    def test_shipped_mentions_self_modification_module(self):
        text = _maturity_text()
        block = _p1_bullet_block(text)
        shipped = _shipped_continuation(block)
        assert "pipeline/self_modification.py" in shipped, (
            "Shipped: note must name `pipeline/self_modification.py`"
        )

    def test_shipped_mentions_server_source_paths_touched(self):
        text = _maturity_text()
        block = _p1_bullet_block(text)
        shipped = _shipped_continuation(block)
        assert "pipeline/server.py" in shipped, (
            "Shipped: note must mention `pipeline/server.py` as a detected path"
        )
        assert "app/pipeline_mcp_server.py" in shipped, (
            "Shipped: note must mention `app/pipeline_mcp_server.py` as a "
            "detected path"
        )

    def test_shipped_mentions_approve_merge(self):
        text = _maturity_text()
        block = _p1_bullet_block(text)
        shipped = _shipped_continuation(block)
        assert "approve_merge" in shipped, (
            "Shipped: note must mention `approve_merge`"
        )

    def test_shipped_mentions_advance_pipeline(self):
        text = _maturity_text()
        block = _p1_bullet_block(text)
        shipped = _shipped_continuation(block)
        assert "advance_pipeline" in shipped, (
            "Shipped: note must mention `advance_pipeline`"
        )

    def test_shipped_mentions_mcp_reconnect(self):
        text = _maturity_text()
        block = _p1_bullet_block(text)
        shipped = _shipped_continuation(block)
        assert "/mcp reconnect" in shipped, (
            "Shipped: note must instruct the operator to run `/mcp reconnect`"
        )

    def test_shipped_mentions_notify_user_mechanism(self):
        text = _maturity_text()
        block = _p1_bullet_block(text)
        shipped = _shipped_continuation(block)
        assert "_notify_user" in shipped, (
            "Shipped: note must name the `_notify_user` mechanism used to "
            "surface the notice to the operator"
        )

    def test_shipped_mentions_fail_open_behavior(self):
        text = _maturity_text()
        block = _p1_bullet_block(text)
        shipped = _shipped_continuation(block)
        assert "fails open" in shipped.lower() or "fail open" in shipped.lower() or "fail-open" in shipped.lower(), (
            "Shipped: note must state that detection fails open on a git error"
        )

    def test_shipped_continuation_indentation_matches_surrounding_style(self):
        """Continuation lines in this file are indented with six spaces to
        align under the bullet's `**` text, matching every other multi-line
        bullet in the document."""
        text = _maturity_text()
        block = _p1_bullet_block(text)
        shipped = _shipped_continuation(block)
        lines = shipped.splitlines()
        assert len(lines) > 1, "Shipped: note should wrap across multiple lines"
        for continuation_line in lines[1:]:
            assert continuation_line.startswith("      "), (
                f"continuation line must be indented six spaces to match "
                f"surrounding bullets, got: {continuation_line!r}"
            )

    def test_shipped_lines_do_not_grossly_exceed_wrap_width(self):
        """The file wraps prose at roughly 76 columns; the new lines should
        follow the same convention (allow slack for unbreakable long
        identifiers/paths)."""
        text = _maturity_text()
        block = _p1_bullet_block(text)
        shipped = _shipped_continuation(block)
        for line in shipped.splitlines():
            assert len(line) <= 90, (
                f"Shipped: continuation line exceeds the file's wrap "
                f"convention (~76 cols, allowing slack): {line!r}"
            )


# ---------------------------------------------------------------------------
# No other checkbox in the file changed state
# ---------------------------------------------------------------------------


class TestOtherChecklistItemsUntouched:
    """Flipping this single P1 bullet must not touch any other checklist
    item's state, including its immediate neighbors in section A3."""

    UNCHECKED_MUST_STAY_UNCHECKED: ClassVar[list[str]] = [
        "Finish the MLX validate re-run.",
        "Mode 31 (2026-07-22, NOT fixed)",
        "Get CI to an enforced green baseline",
        "One-command install story",
    ]

    CHECKED_MUST_STAY_CHECKED: ClassVar[list[str]] = [
        "Finish the `pipeline_mcp_server.py` decomposition",
        "P1 (same retro) — route rework to a stronger model",
        "Fix the test-isolation leak",
        "Split the 74 KB README",
        "P0 (same retro) — stabilize the flaky-under-load",
    ]

    def test_known_unchecked_items_still_unchecked(self):
        text = _maturity_text()
        for needle in self.UNCHECKED_MUST_STAY_UNCHECKED:
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

    def test_immediate_predecessor_bullet_still_checked(self):
        """The P1 'route rework to a stronger model' bullet directly above
        the target bullet must be untouched."""
        text = _maturity_text()
        m = re.search(
            r"^(- \[[ xX]\])[^\n]*P1 \(same retro\) — route rework to a stronger model",
            text,
            re.MULTILINE,
        )
        assert m is not None, "could not find the preceding P1 (same retro) bullet"
        assert m.group(1) == "- [x]"

    def test_immediate_successor_bullet_still_checked(self):
        """The 'Fix the test-isolation leak' bullet directly below the
        target bullet must be untouched."""
        text = _maturity_text()
        m = re.search(
            r"^(- \[[ xX]\])[^\n]*Fix the test-isolation leak",
            text,
            re.MULTILINE,
        )
        assert m is not None, "could not find the following bullet"
        assert m.group(1) == "- [x]"
