"""The test-author prompt steers fixtures to behavior through the public API.

A fixture that greps implementation source text (``include_str!`` and the
like), or turns a brief's scope note ("do NOT touch ws.rs in this story")
into an assertion, breaks the next story and cannot be edited afterwards.
"""

import pytest

from pipeline.test_author import _test_author_prompt


@pytest.mark.parametrize("brief", ["", "Add a Queue to store.rs."])
def test_prompt_forbids_reading_implementation_source_text(brief):
    assert "include_str!" in _test_author_prompt(brief)


@pytest.mark.parametrize("brief", ["", "Add a Queue to store.rs."])
def test_prompt_requires_the_public_api(brief):
    assert "public API" in _test_author_prompt(brief)


@pytest.mark.parametrize("brief", ["", "Add a Queue to store.rs."])
def test_prompt_keeps_scope_constraints_out_of_assertions(brief):
    assert "scope constraint" in _test_author_prompt(brief)


def test_prompt_still_carries_the_brief_verbatim():
    brief = "Add a Queue to store.rs. Do NOT touch ws.rs in this story."

    assert _test_author_prompt(brief).startswith(brief)
