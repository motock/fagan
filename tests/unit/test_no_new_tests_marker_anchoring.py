"""Anchoring of the ``[no-new-tests]`` opt-out sentinel to the start of the brief.

Regression guard for the 2026-09-17 incident: ``_story_opts_out_of_test_author``
used a plain substring search, so a brief that merely *mentioned* the sentinel
while forbidding it (e.g. "[no-new-tests] must NOT be used for this story")
silently skipped the test-author phase for stories that needed it. The marker is
now only recognized as the first non-whitespace text in ``agent_instructions``.
"""

from pipeline.test_author import _story_opts_out_of_test_author


def test_marker_first_thing_opts_out():
    """The marker as the very first text opts the story out."""
    story = {"agent_instructions": "[no-new-tests] some explanation"}
    assert _story_opts_out_of_test_author(story) is True


def test_marker_after_leading_whitespace_opts_out():
    """Leading whitespace/newlines are stripped before the anchor check."""
    story = {"agent_instructions": "\n  [no-new-tests] foo"}
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


def test_empty_instructions_does_not_opt_out():
    """An empty brief never opts out."""
    assert _story_opts_out_of_test_author({"agent_instructions": ""}) is False


def test_missing_key_does_not_opt_out():
    """A story with no agent_instructions key never opts out."""
    assert _story_opts_out_of_test_author({}) is False


def test_marker_prefix_of_longer_token_still_opts_out():
    """Boundary: ``.startswith`` is the contract -- a marker-first brief whose
    first line continues with more text still counts as opted out. No extra
    punctuation requirement is imposed."""
    story = {"agent_instructions": "[no-new-tests]-ish approach"}
    assert _story_opts_out_of_test_author(story) is True


def test_does_not_mutate_story_dict():
    """The check is a pure read; it must not rewrite the story dict."""
    story = {"agent_instructions": "\n  [no-new-tests] foo"}
    _story_opts_out_of_test_author(story)
    assert story == {"agent_instructions": "\n  [no-new-tests] foo"}
