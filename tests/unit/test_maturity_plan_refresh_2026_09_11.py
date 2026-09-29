"""Acceptance oracle: MATURITY_AND_UNIQUENESS_PLANS.md must reflect the
current refresh state (originally 2026-09-11, re-pinned for the 2026-09-14
refresh).

Four bullets that the doc still carries as unchecked actually landed:
enforced-green CI + a real release tag (A3), the macOS CI leg re-enable
(A4), releases+changelog (B6) and contributor docs + ADRs (B6). The tail
rollup ("What's left as of 2026-09-08") repeats all four as outstanding.

This grades the refresh structurally -- anchors and evidence tokens, never
the exact prose -- so the implementer keeps editorial latitude while the
genuinely-open bullets are protected from drive-by flipping. "Protected"
means by name: the guard asserts that no unchecked bullet appears outside
the named open set, never that the document's total stayed at five. A total
pin made the retro-feedback sink (see ``RETRO_FEEDBACK_HEADING``) unwritable.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC = REPO_ROOT / "docs" / "plans" / "MATURITY_AND_UNIQUENESS_PLANS.md"


def _text() -> str:
    assert DOC.exists(), f"maturity plan doc missing: {DOC}"
    return DOC.read_text(encoding="utf-8")


def _bullet_block(text: str, anchor: str) -> str:
    """The full logical bullet (checkbox line + indented continuations) whose
    first line contains ``anchor``."""
    pattern = re.compile(
        r"^- \[[ xX~]\][^\n]*"
        + re.escape(anchor)
        + r"[^\n]*\n(?:[ \t]+[^\n]*\n)*",
        re.MULTILINE,
    )
    match = pattern.search(text)
    assert match, f"could not locate a bullet block anchored on {anchor!r}"
    return match.group(0)


# The subsection the retro process appends its P0/P1 items to, exempt from the
# drive-by open-item guard below (see that test for why).
RETRO_FEEDBACK_HEADING = "### A5. Retro-derived harness improvements"


def _without_retro_feedback_sink(text: str) -> str:
    """``text`` with the retro-feedback subsection removed, if it is present.

    Returns the text unchanged when the subsection has not been added yet, so
    the guard degrades to grading the whole document rather than failing to
    collect.
    """
    start = text.find(RETRO_FEEDBACK_HEADING)
    if start == -1:
        return text
    end = text.find("\n## ", start)
    return text[:start] + (text[end:] if end != -1 else "")


# --- the four bullets that must now be checked, with their evidence ---------

# Anchors are the stable HEADLINE PREFIX of each bullet, not its full
# sentence: the refresh appends a "- DONE <date>" annotation to the headline,
# so pinning the whole sentence would pin prose the story is meant to rewrite.
CLOSED = {
    "Get CI to an enforced green baseline": "v0.1.0",
    "Re-enable the macOS CI leg": "#674",
    "Tag releases + changelog": "#681",
    "Contributor docs + the ADR pattern": "#682",
}

# --- bullets that are genuinely still open and must stay unchecked ----------

STILL_OPEN = (
    "Bound the failure-mode discovery rate",
    "RBAC / multi-user.",
    "Concurrency & queueing beyond",
    "Standard benchmark integration",
    "A public demo / writeup of the MCP-native inversion",
)

# Bullets that are genuinely partially done (work landed, work remains) and
# must stay marked in-progress rather than open or closed.
PARTIAL = (
    "Multi-repo fleet as a first-class concept.",
)

# --- anchors other test modules pin byte-identically -----------------------

PINNED_ELSEWHERE = (
    "**P1 (from `retros/tdd-split-unconditional-and-review-race_2026-07-21.md`)",
    "Mode 29",
)

STALE_SENTENCES = (
    "Manual local suite + `gh pr merge` remains the operating mode until then.",
    "**What's left as of 2026-09-08:**",
)


class TestClosedItemsAreChecked:
    def test_each_closed_bullet_is_checked(self):
        text = _text()
        for anchor in CLOSED:
            block = _bullet_block(text, anchor)
            assert block.startswith("- [x]"), (
                f"bullet {anchor!r} is still unchecked; it landed 2026-09-11"
            )

    def test_each_closed_bullet_cites_its_evidence(self):
        text = _text()
        for anchor, evidence in CLOSED.items():
            block = _bullet_block(text, anchor)
            assert evidence in block, (
                f"bullet {anchor!r} must cite {evidence} as evidence it landed"
            )

    def test_each_closed_bullet_is_dated(self):
        text = _text()
        for anchor in CLOSED:
            block = _bullet_block(text, anchor)
            assert "2026-09-11" in block, (
                f"bullet {anchor!r} must record the 2026-09-11 completion date"
            )


class TestStaleClaimsAreGone:
    def test_no_stale_sentence_survives(self):
        text = _text()
        for sentence in STALE_SENTENCES:
            assert sentence not in text, (
                f"stale claim still present: {sentence!r}"
            )

    def test_rollup_is_restated_for_the_current_date(self):
        text = _text()
        match = re.search(r"\*\*What's left as of (\d{4}-\d{2}-\d{2}):\*\*", text)
        assert match and match.group(1) >= "2026-09-14", (
            "the tail rollup must be restated as of the 2026-09-14 refresh "
            "(a literal date pin would break on every future refresh; grade "
            "recency, not the exact date)"
        )

    def test_rollup_no_longer_lists_the_billing_cap_as_a_blocker(self):
        text = _text()
        rollup_anchor = re.search(r"\*\*What's left as of \d{4}-\d{2}-\d{2}:\*\*", text)
        assert rollup_anchor, "no current rollup anchor found"
        rollup = text[rollup_anchor.end() : rollup_anchor.end() + 1200]
        assert "billing cap" not in rollup, (
            "the rollup still blames the GHA billing cap, which is resolved"
        )


class TestOpenItemsAreProtected:
    def test_still_open_bullets_remain_unchecked(self):
        text = _text()
        for anchor in STILL_OPEN:
            block = _bullet_block(text, anchor)
            assert block.startswith("- [ ]"), (
                f"bullet {anchor!r} is still open and must stay unchecked"
            )

    def test_partial_bullets_remain_in_progress(self):
        text = _text()
        for anchor in PARTIAL:
            block = _bullet_block(text, anchor)
            assert block.startswith("- [~]"), (
                f"bullet {anchor!r} is partially done and must stay marked "
                "in-progress ([~]), neither open nor closed"
            )

    def test_no_unchecked_bullets_outside_the_retro_feedback_sink(self):
        """Guard against drive-by open items: every unchecked bullet in the
        document body must be one of the named ``STILL_OPEN`` items.

        The retro-feedback subsection is exempted, not counted: it is the
        process's designated sink for a retro's P0/P1 items
        (`PLAN_RETROSPECTIVE_PROCESS_PLAN.md` §2), so it is *expected* to grow
        unchecked bullets over time. Pinning the document's total instead made
        that sink unwritable, which is why no retro's items reached this doc
        between 2026-07-21 and this change.
        """
        text = _without_retro_feedback_sink(_text())
        for line in text.splitlines():
            if not line.startswith("- [ ] "):
                continue
            assert any(anchor in line for anchor in STILL_OPEN), (
                f"unexpected new open item outside the retro-feedback sink: "
                f"{line!r} — the document's open set is pinned to the "
                f"{len(STILL_OPEN)} named items; a retro's P0/P1 items belong "
                f"in the retro-feedback subsection"
            )

    def test_bullets_pinned_by_other_tests_are_untouched(self):
        text = _text()
        for anchor in PINNED_ELSEWHERE:
            assert anchor in text, (
                f"{anchor!r} is pinned by another test module and must survive"
            )
