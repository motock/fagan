"""``[no-new-tests]`` opt-out detection: invocation vs. mere mention.

Regression guard for the 2026-09-17 incident: ``_story_opts_out_of_test_author``
used a plain substring search, so a brief that merely *mentioned* the sentinel
while forbidding it (e.g. "[no-new-tests] must NOT be used for this story")
silently skipped the test-author phase for stories that needed it. A token whose
own sentence negates its use is now read as a mention, not an opt-out, while
every other occurrence -- leading the brief or trailing it -- still opts out.
"""

from pipeline.test_author import _story_opts_out_of_test_author


def test_marker_first_thing_opts_out():
    """The marker as the very first text opts the story out."""
    story = {"agent_instructions": "[no-new-tests] some explanation"}
    assert _story_opts_out_of_test_author(story) is True


def test_marker_after_leading_whitespace_opts_out():
    """Leading whitespace/newlines do not stop the opt-out."""
    story = {"agent_instructions": "\n  [no-new-tests] foo"}
    assert _story_opts_out_of_test_author(story) is True


def test_marker_trailing_the_brief_opts_out():
    """A trailing marker still opts out -- the established contract pinned by
    tests/unit/test_test_author_park_only_and_optout.py, which must keep
    passing unmodified."""
    story = {
        "agent_instructions": (
            "Move decompose_plan body onto PipelineService; existing "
            "test_pipeline_mcp_server.py covers it. [no-new-tests]"
        )
    }
    assert _story_opts_out_of_test_author(story) is True


def test_marker_mid_document_does_not_opt_out():
    """THE regression: a brief that mentions the sentinel mid-document while
    forbidding it must NOT opt the story out of the test-author phase."""
    story = {
        "agent_instructions": (
            "some brief text.\n\n"
            "[no-new-tests] must NOT be used for this story."
        )
    }
    assert _story_opts_out_of_test_author(story) is False


def test_marker_negation_without_use_verb_still_opts_out():
    """Boundary: negation alone is NOT enough to suppress the opt-out -- a
    legitimate brief may say "[no-new-tests] not needed, the existing suite
    covers it". Only a negation of the token's *use* marks a mention."""
    story = {
        "agent_instructions": (
            "[no-new-tests] not needed, the existing suite covers it."
        )
    }
    assert _story_opts_out_of_test_author(story) is True


def test_marker_mention_then_real_opt_out_still_opts_out():
    """A brief that mentions the token while forbidding it AND later invokes
    it for real still opts out -- the scan continues past the mention."""
    story = {
        "agent_instructions": (
            "[no-new-tests] must NOT be used for this story.\n\n"
            "Actually, the existing suite covers it. [no-new-tests]"
        )
    }
    assert _story_opts_out_of_test_author(story) is True


def test_empty_instructions_does_not_opt_out():
    """An empty brief never opts out."""
    assert _story_opts_out_of_test_author({"agent_instructions": ""}) is False


def test_missing_key_does_not_opt_out():
    """A story with no agent_instructions key never opts out."""
    assert _story_opts_out_of_test_author({}) is False


def test_marker_prefix_of_longer_token_still_opts_out():
    """Boundary: the marker is matched as a literal token, so a marker-first
    brief whose first line continues with more text still opts out. No extra
    punctuation requirement is imposed."""
    story = {"agent_instructions": "[no-new-tests]-ish approach"}
    assert _story_opts_out_of_test_author(story) is True


def test_does_not_mutate_story_dict():
    """The check is a pure read; it must not rewrite the story dict."""
    story = {"agent_instructions": "\n  [no-new-tests] foo"}
    _story_opts_out_of_test_author(story)
    assert story == {"agent_instructions": "\n  [no-new-tests] foo"}


def test_guard_is_stateless_across_calls():
    """The opt-out must not leave sticky state behind: a later story with no
    sentinel must not inherit the previous story's opt-out."""
    assert _story_opts_out_of_test_author(
        {"agent_instructions": "[no-new-tests] refactor only"}
    ) is True
    assert _story_opts_out_of_test_author(
        {"agent_instructions": "Add retry to uploader"}
    ) is False
