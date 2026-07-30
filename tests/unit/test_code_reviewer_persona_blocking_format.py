"""Guards the checked-in agents/code-reviewer.md Output contract against
losing the strict machine-parseable Blocking-finding format.

Root cause diagnosed live (2026-07-23, MODE-29-REVIEW-STORY-LOCK-GUARD): the
LIVE deployed persona at ~/.claude/agents/code-reviewer.md (loaded via
pipeline.paths.AGENTS_DIR, which defaults there - not this repo's checked-in
copy) was missing the "- Blocking: <path>: <description>" format instruction
entirely, so pipeline.parsers._extract_blocking_finding_files silently found
nothing in three real review cycles and the Mode 24/28 finding-target guard
(PR #158) never engaged. The checked-in copy in this repo DID have the
instruction but was never deployed to the live file - a config-drift gap
this test cannot catch (it only guards the checked-in copy from regressing
the same way). See the sibling live-file fix and memory/project_dispatch_
failure_modes.md for the full incident.
"""
from pathlib import Path

_PERSONA_PATH = Path(__file__).parent.parent.parent / "agents" / "code-reviewer.md"


def test_checked_in_persona_requires_the_exact_blocking_line_format():
    text = _PERSONA_PATH.read_text()
    assert "- Blocking: <relative/file/path>: <one-line description>" in text


def test_checked_in_persona_requires_the_line_even_alongside_richer_prose():
    """The real failure mode was a strong reviewer model writing rich
    ### Blocking / **N. Title** markdown findings with NO strict trailer
    line at all - the contract must explicitly say the strict line is
    required IN ADDITION TO richer prose, not instead of it."""
    text = _PERSONA_PATH.read_text()
    assert "REQUIRED even when" in text
    assert "not a\nreplacement for that explanation" in text or "not a replacement for that explanation" in text.replace("\n", " ")
