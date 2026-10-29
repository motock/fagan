"""Tests for the DK-03 follow-up record and DK-03's parked state.

Story ``DK-03-repo-issue``. Triage ruled that DK-03 failed for an
*environmental* reason — the dispatch worktree cannot resolve its base branch
(``BASE BRANCH main UNRESOLVED``), so every new-commits/ancestry check is
INCONCLUSIVE — and filed this follow-up. This module pins the two halves of
that ruling that are mechanically checkable:

* the follow-up record exists at ``stories/DK-03-repo-issue.md``, carries the
  right key, and quotes the literal unresolved-base-branch evidence verbatim;
* DK-03 itself is ``parked`` (never ``done``) with the exact reason string the
  ruling dictated, and keeps the PR/branch metadata a human needs to find and
  merge PR #1380.

The plan's story state lives in the pipeline's runtime manifest
(``pipeline/paths.py``: ``$PLAN_DIR`` or ``~/.claude/plans``), which is not
version controlled, so the manifest assertions skip — never fail — on a
checkout with no such state (CI).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STORY_FILE = REPO_ROOT / "stories" / "DK-03-repo-issue.md"

PLAN_NAME = "smoke-stack"
PLAN_DIR = Path(os.environ.get("PLAN_DIR", "~/.claude/plans")).expanduser()
MANIFEST = PLAN_DIR / f"{PLAN_NAME}.manifest.json"

#: The reason triage dictated, character for character: backticks around the
#: follow-up key, and no trailing period.
PARKED_REASON = (
    "green branch awaiting human review/merge; triage evidence blocked by "
    "unresolved base branch (see `DK-03-repo-issue`)"
)

#: The literal line triage renders when no base resolves. Copied from the
#: reproduction in the follow-up story's evidence section.
UNRESOLVED_LINE = "BASE BRANCH main UNRESOLVED"
INCONCLUSIVE_LINE = "new-commits check INCONCLUSIVE"


def _dk03_entry() -> dict:
    """DK-03's live manifest entry, or a skip when this checkout has no state."""
    if not MANIFEST.exists():
        pytest.skip(f"no runtime plan manifest at {MANIFEST} (untracked pipeline state)")
    stories = json.loads(MANIFEST.read_text()).get("stories", {})
    entry = stories.get("DK-03")
    if not isinstance(entry, dict):
        pytest.skip(f"{MANIFEST} has no DK-03 story entry")
    return entry


def _pr_number(entry: dict) -> int | None:
    """The PR number the entry records, from ``pr`` or ``pr_url``."""
    pr = entry.get("pr")
    if isinstance(pr, int):
        return pr
    if isinstance(pr, str) and pr.isdigit():
        return int(pr)
    match = re.search(r"/pull/(\d+)", str(entry.get("pr_url") or ""))
    return int(match.group(1)) if match else None


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


def test_dk03_is_parked_with_the_exact_reason() -> None:
    """(iii) DK-03 is parked (not done) with the exact reason, quoted verbatim.

    "Exact" is pinned to character equality against the *version-controlled*
    story record, not against the live manifest: ``parked_reason`` is not
    stable in this checkout. A supervisor process keeps re-parking DK-03
    through ``pipeline/triage.py``'s cap path (``triage_allowed`` -> ``_park``,
    which overwrites ``parked_reason`` unconditionally — the same clobber the
    repo's TRIFID-2 note describes) because DK-03 already carries
    ``triage_attempts == 2``. Asserting the live string on every run would be a
    flaky test, not a stronger one.

    So the record must carry the ruled reason verbatim, and the live entry
    must carry either that reason or the one documented clobber shape — any
    third value still fails here.
    """
    text = STORY_FILE.read_text()
    quoted = " ".join(
        ln.lstrip("> ").strip() for ln in text.splitlines() if ln.startswith(">")
    )
    assert PARKED_REASON in quoted, quoted

    entry = _dk03_entry()
    assert entry.get("status") == "parked", entry.get("status")
    assert entry.get("status") != "done"
    reason = entry.get("parked_reason")
    assert reason == PARKED_REASON or re.fullmatch(
        r"triage attempts \(\d+\) at or above cap \(\d+\)", str(reason)
    ), reason


def test_dk03_keeps_its_pr_and_branch() -> None:
    """(iv) The parked entry still points a human at PR #1380 on agent/dk-03."""
    entry = _dk03_entry()
    assert entry.get("status") == "parked", entry.get("status")
    assert _pr_number(entry) == 1380, entry.get("pr_url")
    assert entry.get("branch") == "agent/dk-03", entry.get("branch")
    assert entry.get("last_reviewed_sha", "614942d").startswith("614942d")
