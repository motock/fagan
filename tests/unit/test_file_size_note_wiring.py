"""The advisory file-size note must reach the implementer and reviewer prompts.

WHY: ``file_size_advisory_note`` (pipeline.file_size_policy_dispatch) renders
the advisory text only for a story stamped ``advisory``. Wiring it into
``pipeline.persona._build_dispatch_command`` (both prompt variants) and
``pipeline.review.build_review_story_context`` is what makes the note reach
the agents. An unstamped or enforced story must keep today's prompts exactly.

The note text is a fixed literal owned by the previous story; these tests
assert membership and ordering against it, never the full prompt string.
"""

from pipeline import review as review_mod
from pipeline import server as p
from tests.unit import _pipeline_mcp_server_test_helpers as _helpers
from tests.unit._pipeline_mcp_server_test_helpers import _story

agents_dir = _helpers.agents_dir

ADVISORY_MARKER = "file_size_policy is advisory"
FINAL_LINE = "When finished, commit your work, push the branch, and exit."
JOURNAL = [
    {
        "step": "step-1",
        "summary": "Wrote the parser",
        "next_hint": "add validation",
        "commit": "sha-1",
        "ts": "x",
    },
]


def _advisory_story(**over):
    return _story(file_size_policy_resolution={"file_size_policy": "advisory"}, **over)


def _enforced_story(**over):
    return _story(file_size_policy_resolution={"file_size_policy": "enforced"}, **over)


def _fresh_prompt(story):
    return p._build_dispatch_command(story, "PIPE-1")["prompt"]


def _resumed_prompt(story):
    return p._build_dispatch_command(story, "PIPE-1", resume_journal=JOURNAL)["prompt"]


# ---------- Implementer: advisory stamp adds the note in both variants ----------


def test_advisory_story_fresh_prompt_contains_size_note(agents_dir):
    assert ADVISORY_MARKER in _fresh_prompt(_advisory_story())


def test_advisory_story_resumed_prompt_contains_size_note(agents_dir):
    assert ADVISORY_MARKER in _resumed_prompt(_advisory_story())


def test_advisory_note_sits_before_the_final_line_in_fresh_prompt(agents_dir):
    prompt = _fresh_prompt(_advisory_story())
    assert prompt.index(ADVISORY_MARKER) < prompt.index(FINAL_LINE)


def test_advisory_note_sits_before_the_final_line_in_resumed_prompt(agents_dir):
    prompt = _resumed_prompt(_advisory_story())
    assert prompt.index(ADVISORY_MARKER) < prompt.index(FINAL_LINE)


# ---------- Implementer: unstamped and enforced stories are unchanged ----------


def test_unstamped_story_fresh_prompt_has_no_size_note(agents_dir):
    assert ADVISORY_MARKER not in _fresh_prompt(_story())


def test_unstamped_story_resumed_prompt_has_no_size_note(agents_dir):
    assert ADVISORY_MARKER not in _resumed_prompt(_story())


def test_unstamped_story_fresh_prompt_equals_empty_resolution_prompt(agents_dir):
    empty = _story(file_size_policy_resolution={})
    assert _fresh_prompt(_story()) == _fresh_prompt(empty)


def test_unstamped_story_resumed_prompt_equals_empty_resolution_prompt(agents_dir):
    empty = _story(file_size_policy_resolution={})
    assert _resumed_prompt(_story()) == _resumed_prompt(empty)


def test_enforced_story_fresh_prompt_has_no_size_note(agents_dir):
    assert ADVISORY_MARKER not in _fresh_prompt(_enforced_story())


def test_enforced_story_resumed_prompt_has_no_size_note(agents_dir):
    assert ADVISORY_MARKER not in _resumed_prompt(_enforced_story())


# ---------- Reviewer: build_review_story_context ----------

REVIEW_NOTE_MARKER = "A file over about 1000 lines is at most a Suggestion or a Nit."


def _manifest(story):
    return {"stories": {"story-a": story}}


def _review_ctx(story):
    return review_mod.build_review_story_context("story-a", _manifest(story))


def test_advisory_story_review_context_contains_reviewer_note():
    ctx = _review_ctx(_advisory_story(agent_instructions="Build it."))
    assert REVIEW_NOTE_MARKER in ctx


def test_advisory_story_review_context_still_ends_with_scope_rule():
    ctx = _review_ctx(_advisory_story(agent_instructions="Build it."))
    assert ctx.endswith(review_mod.REVIEW_CONTEXT_SCOPE_RULE)


def test_unstamped_story_review_context_has_no_size_note():
    ctx = _review_ctx(_story(agent_instructions="Build it."))
    assert REVIEW_NOTE_MARKER not in ctx


def test_unstamped_story_review_context_equals_empty_resolution_context():
    unstamped = _review_ctx(_story(agent_instructions="Build it."))
    empty = _review_ctx(
        _story(agent_instructions="Build it.", file_size_policy_resolution={})
    )
    assert unstamped == empty


def test_enforced_story_review_context_has_no_size_note():
    ctx = _review_ctx(_enforced_story(agent_instructions="Build it."))
    assert REVIEW_NOTE_MARKER not in ctx
