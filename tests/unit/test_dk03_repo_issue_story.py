"""Tests for the DK-03 follow-up record.

Story ``DK-03-repo-issue``. Triage ruled that DK-03 failed for an
*environmental* reason — the dispatch worktree cannot resolve its base branch
(``BASE BRANCH main UNRESOLVED``), so every new-commits/ancestry check is
INCONCLUSIVE — and filed this follow-up. This module pins the mechanically
checkable half of that ruling that is version controlled:

* the follow-up record exists at ``stories/DK-03-repo-issue.md``, carries the
  right key, and quotes the literal unresolved-base-branch evidence verbatim.

DK-03's own runtime status — ``parked`` versus ``pr_open``/``done``, and its
PR/branch metadata — is deliberately *not* asserted here. That state lives in
the pipeline's runtime manifest (``pipeline/paths.py``: ``$PLAN_DIR`` or
``~/.claude/plans``), which is not version controlled and which the pipeline
itself legitimately advances, so pinning it would fail the moment the system
did its job.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STORY_FILE = REPO_ROOT / "stories" / "DK-03-repo-issue.md"

#: The literal line triage renders when no base resolves. Copied from the
#: reproduction in the follow-up story's evidence section.
UNRESOLVED_LINE = "BASE BRANCH main UNRESOLVED"
INCONCLUSIVE_LINE = "new-commits check INCONCLUSIVE"


def test_followup_story_file_exists_with_its_own_key() -> None:
    """(i) stories/DK-03-repo-issue.md exists, keyed exactly DK-03-repo-issue."""
    assert STORY_FILE.is_file(), f"missing follow-up story file {STORY_FILE}"

    text = STORY_FILE.read_text()
    heading = next((ln for ln in text.splitlines() if ln.startswith("# ")), "")
    assert heading, f"{STORY_FILE} has no '# <key> — <title>' heading"
    assert heading.split()[1] == "DK-03-repo-issue", heading

    # A todo status, and a title that says what the story repairs.
    assert re.search(r"^[Ss]tatus:\s*todo\s*$", text, re.MULTILINE), text[:400]
    assert "base-branch resolution" in heading.lower() or "base branch" in heading.lower(), heading


def test_followup_story_quotes_the_unresolved_base_branch_evidence() -> None:
    """(ii) The record carries the literal evidence and names DK-03 / PR #1380."""
    text = STORY_FILE.read_text()
    assert UNRESOLVED_LINE in text, f"evidence line {UNRESOLVED_LINE!r} missing"
    assert INCONCLUSIVE_LINE in text, f"evidence line {INCONCLUSIVE_LINE!r} missing"
    assert "DK-03" in text
    assert "1380" in text, "the record must name PR #1380"
    # The follow-up must not become a vehicle for DK-03's own deliverable.
    assert "must NOT implement" in text or "must not implement" in text.lower(), text[:400]
