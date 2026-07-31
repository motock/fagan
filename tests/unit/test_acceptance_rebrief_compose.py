"""Acceptance oracle: compose_rebriefed_instructions must prepend exactly ONE
prior-attempt diagnosis block and stay idempotent across repeated reworks -
stacked blocks would grow the prompt without bound over a rework loop.
"""
from pipeline.rebrief import (
    DIAGNOSIS_HEADER,
    compose_rebriefed_instructions,
)

BRIEF = "GOAL: build the thing.\nSCOPE: one file."


def test_adds_a_diagnosis_block():
    out = compose_rebriefed_instructions(BRIEF, "the generator used the wrong dir")
    assert DIAGNOSIS_HEADER in out
    assert "the generator used the wrong dir" in out


def test_original_brief_is_preserved():
    out = compose_rebriefed_instructions(BRIEF, "root cause")
    assert "GOAL: build the thing." in out
    assert "SCOPE: one file." in out


def test_second_call_replaces_rather_than_stacks():
    once = compose_rebriefed_instructions(BRIEF, "first cause")
    twice = compose_rebriefed_instructions(once, "second cause")
    assert twice.count(DIAGNOSIS_HEADER) == 1
    assert "second cause" in twice
    assert "first cause" not in twice


def test_no_diagnosis_returns_the_brief_unchanged():
    assert compose_rebriefed_instructions(BRIEF, None) == BRIEF
    assert compose_rebriefed_instructions(BRIEF, "  ") == BRIEF


def test_empty_brief_still_produces_the_block():
    out = compose_rebriefed_instructions("", "root cause")
    assert DIAGNOSIS_HEADER in out
