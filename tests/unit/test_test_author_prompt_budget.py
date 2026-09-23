"""Tests for the test-author prompt's structural-assertion and
suite-proportion guidance (LDC-5).

The test-author dispatch prompt must tell the tech lead to assert structural
facts (a name, key or row is present / gone) rather than the exact wording of
a docstring, comment or prose sentence, and to keep the suite proportionate
(one focused test per requirement, well under 200 lines for a change of a few
dozen lines).  The old "trivially assertable" phrasing, which invited
prose-wording assertions, must be gone.

The prompt is a shared artifact that later stories also extend, so these
tests assert membership of the new guidance and absence of the removed
phrasing - never the prompt's total contents.
"""

from __future__ import annotations

import pytest

from pipeline.test_author import _test_author_prompt

TASK = "TASK"

# The exact guidance this story adds (whitespace-normalized).
FORBIDS_PROSE_WORDING = (
    "never assert the exact wording of a docstring, comment or prose sentence"
)
STRUCTURAL_ASSERTION = (
    "assert them STRUCTURALLY (read the file, assert the new name, key or row "
    "is present and the old one is gone)"
)
PROPORTIONATE_SUITE = "well under 200 lines"
ONE_TEST_PER_REQUIREMENT = (
    "one focused test per requirement plus its negative and boundary cases"
)
NO_PROSE_MATRICES = "no parametrized matrices over prose"

# Phrasing this story removes.
OLD_TRIVIALLY_ASSERTABLE = (
    "docstring/comment updates all count: they are trivially assertable"
)
OLD_PROSE_ASSERTION = (
    "they are trivially assertable (read the file, assert the new name is "
    "present and the old one is gone)"
)

# Text that must survive the edit untouched.
PRESERVED_SENTENCE = (
    "Renames, removals of a now-dead name, and docstring/comment updates all "
    "count"
)


@pytest.fixture()
def prompt() -> str:
    return _test_author_prompt(TASK)


@pytest.fixture()
def flat(prompt: str) -> str:
    """The prompt with all runs of whitespace collapsed to single spaces."""
    return " ".join(prompt.split())


def test_prompt_forbids_asserting_prose_wording(flat: str) -> None:
    assert FORBIDS_PROSE_WORDING in flat


def test_prompt_requires_structural_assertions(flat: str) -> None:
    assert STRUCTURAL_ASSERTION in flat


def test_prompt_requires_proportionate_suite(flat: str) -> None:
    assert PROPORTIONATE_SUITE in flat


def test_prompt_requires_one_focused_test_per_requirement(flat: str) -> None:
    assert ONE_TEST_PER_REQUIREMENT in flat


def test_prompt_forbids_parametrized_matrices_over_prose(flat: str) -> None:
    assert NO_PROSE_MATRICES in flat


def test_old_trivially_assertable_phrase_is_gone(flat: str) -> None:
    assert OLD_TRIVIALLY_ASSERTABLE not in flat


def test_old_prose_assertion_instruction_is_gone(flat: str) -> None:
    assert OLD_PROSE_ASSERTION not in flat


def test_surrounding_sentence_is_preserved(flat: str) -> None:
    assert PRESERVED_SENTENCE in flat


def test_task_is_still_the_first_line(prompt: str) -> None:
    assert prompt.splitlines()[0] == TASK


def test_exception_marker_appears_exactly_once(prompt: str) -> None:
    assert prompt.count("EXCEPTION:") == 1


def test_empty_instructions_still_get_the_new_guidance() -> None:
    flat = " ".join(_test_author_prompt("").split())
    assert FORBIDS_PROSE_WORDING in flat
    assert PROPORTIONATE_SUITE in flat
    assert OLD_TRIVIALLY_ASSERTABLE not in flat


def test_multiline_instructions_keep_their_first_line() -> None:
    prompt = _test_author_prompt("TASK\n\nmore detail here")
    assert prompt.splitlines()[0] == "TASK"
    assert "more detail here" in prompt
