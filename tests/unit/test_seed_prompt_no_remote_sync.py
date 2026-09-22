"""The seed prompt must forbid the agent from resyncing the story branch from origin.

WHY: the harness force-pushes the story branch after a resume rebase so the
agent's own push is a fast-forward. That closes only half the hole -- an agent
that pulls, merges, or rebases the remote branch can still resurrect commits a
rebase rewrote away (exactly the mechanism that produced commit 4c0ea9b on
agent/oa2-01: "Merge remote-tracking branch 'origin/agent/oa2-01' into
agent/oa2-01", with an add/add conflict on its own test file, which cost that
story four merge-gate cycles).

Both prompt variants in ``pipeline.persona._build_dispatch_command`` (the
resumed variant and the fresh variant) must carry the new sentence immediately
before the final "When finished, commit your work, push the branch, and exit."
line.

These tests are membership/ordering assertions only -- they never pin the exact
full prompt string, so later stories can keep extending the prompt.
"""
import pytest

from pipeline import server as p
from tests.unit import _pipeline_mcp_server_test_helpers as _helpers
from tests.unit._pipeline_mcp_server_test_helpers import _story

# Re-export the shared agents_dir fixture under its own name so pytest can
# resolve it as a test-function parameter. Bound via the helper module (rather
# than a direct import) so ruff's F811 does not mistake the fixture parameter
# for a shadowed import -- this file is not covered by the
# tests/unit/test_pipeline_mcp_server_*.py per-file ignore.
agents_dir = _helpers.agents_dir

# The sentence this story adds, verbatim as it must appear in the prompt.
NEW_SENTENCE = (
    "The pipeline owns this branch's remote state: never run git pull, "
    "git merge, or git rebase against origin — just commit and push. "
    "If a push is rejected, stop and report it rather than merging.\n\n"
)

# The pre-existing final line of both prompt variants; a fixed anchor.
FINAL_LINE = "When finished, commit your work, push the branch, and exit."

# Individual clauses of the new sentence. Asserting each one separately means a
# partial implementation (e.g. only the "git pull" half) fails rather than
# sneaking through on a single substring match.
NEW_SENTENCE_CLAUSES = [
    "The pipeline owns this branch's remote state",
    "never run git pull",
    "git merge",
    "git rebase against origin",
    "just commit and push",
    "If a push is rejected, stop and report it rather than merging.",
]


def _fresh_prompt(**kwargs):
    """Prompt built with no resume_journal -> the fresh variant."""
    spec = p._build_dispatch_command(_story(), "PIPE-1", **kwargs)
    return spec["prompt"]


def _resumed_prompt(journal, **kwargs):
    """Prompt built with a non-empty resume_journal -> the resumed variant."""
    spec = p._build_dispatch_command(_story(), "PIPE-1", resume_journal=journal, **kwargs)
    return spec["prompt"]


JOURNAL = [
    {"step": "step-1", "summary": "Wrote the parser",
     "next_hint": "add validation", "commit": "sha-1", "ts": "x"},
]


# ---------- Happy path: both prompt variants ----------


def test_fresh_variant_prompt_forbids_remote_resync(agents_dir):
    prompt = _fresh_prompt()
    assert NEW_SENTENCE in prompt


def test_resumed_variant_prompt_forbids_remote_resync(agents_dir):
    prompt = _resumed_prompt(JOURNAL)
    assert NEW_SENTENCE in prompt


@pytest.mark.parametrize("clause", NEW_SENTENCE_CLAUSES)
def test_fresh_variant_prompt_contains_every_clause(agents_dir, clause):
    assert clause in _fresh_prompt()


@pytest.mark.parametrize("clause", NEW_SENTENCE_CLAUSES)
def test_resumed_variant_prompt_contains_every_clause(agents_dir, clause):
    assert clause in _resumed_prompt(JOURNAL)


# ---------- Ordering: immediately before the final line ----------


def test_fresh_variant_sentence_immediately_precedes_final_line(agents_dir):
    prompt = _fresh_prompt()
    assert prompt.endswith(NEW_SENTENCE + FINAL_LINE)


def test_resumed_variant_sentence_immediately_precedes_final_line(agents_dir):
    prompt = _resumed_prompt(JOURNAL)
    assert prompt.endswith(NEW_SENTENCE + FINAL_LINE)


def test_fresh_variant_sentence_appears_exactly_once(agents_dir):
    assert _fresh_prompt().count(NEW_SENTENCE) == 1


def test_resumed_variant_sentence_appears_exactly_once(agents_dir):
    assert _resumed_prompt(JOURNAL).count(NEW_SENTENCE) == 1


# ---------- The pre-existing final line is untouched ----------


def test_final_line_still_present_in_both_variants(agents_dir):
    assert FINAL_LINE in _fresh_prompt()
    assert FINAL_LINE in _resumed_prompt(JOURNAL)


# ---------- Boundary / negative cases ----------


def test_empty_resume_journal_takes_fresh_variant_and_still_forbids_resync(agents_dir):
    # An empty journal is falsy, so this is the fresh variant -- and it must
    # still carry the sentence.
    prompt = _fresh_prompt(resume_journal=[])
    assert "RESUMING" not in prompt
    assert NEW_SENTENCE in prompt
    assert prompt.endswith(NEW_SENTENCE + FINAL_LINE)


def test_resumed_variant_without_next_hint_still_forbids_resync(agents_dir):
    # The resumed variant falls back to a default next_hint when the last
    # journal entry has none; the sentence must be present regardless.
    journal = [{"step": "step-1", "summary": "Wrote the parser"}]
    prompt = _resumed_prompt(journal)
    assert "RESUMING" in prompt
    assert NEW_SENTENCE in prompt
    assert prompt.endswith(NEW_SENTENCE + FINAL_LINE)


def test_multi_entry_resume_journal_still_forbids_resync(agents_dir):
    journal = [
        {"step": "step-1", "summary": "Wrote the parser", "next_hint": "add validation"},
        {"step": "step-2", "summary": "Added validation", "next_hint": "wire it up"},
        {"step": "step-3", "summary": "Wired it up", "next_hint": "run the suite"},
    ]
    prompt = _resumed_prompt(journal)
    assert "Wired it up" in prompt
    assert NEW_SENTENCE in prompt
    assert prompt.endswith(NEW_SENTENCE + FINAL_LINE)


def test_rework_feedback_variant_still_forbids_resync(agents_dir):
    # review_feedback prepends a rework instruction; the sentence must survive.
    prompt = _fresh_prompt(review_feedback="Please rename the helper.")
    assert "REQUESTED CHANGES" in prompt
    assert NEW_SENTENCE in prompt
    assert prompt.endswith(NEW_SENTENCE + FINAL_LINE)


def test_sentence_is_not_duplicated_across_the_two_variants(agents_dir):
    # Both variants carry it once each -- the resumed variant must not inherit
    # the fresh variant's copy on top of its own.
    fresh = _fresh_prompt()
    resumed = _resumed_prompt(JOURNAL)
    assert fresh.count(NEW_SENTENCE) == 1
    assert resumed.count(NEW_SENTENCE) == 1
