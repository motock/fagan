"""Open questions from the decomposer: ``take_questions`` and the persona section.

The product-analyst may return a top-level ``open_questions`` list beside its
plan when a request leaves a decision open. ``pipeline.intake_guidance`` pops
that key before the plan is saved, so ``save_plan`` never sees an unknown key,
and returns the sanitised questions. The questions are untrusted text from an
outside ticket: a malformed value yields no questions and never fails intake.

Persona-text checks are membership probes against fixed anchors, never a hash
or a total-content comparison, because later stories may extend the persona.
"""

import json
from pathlib import Path

import pytest

from pipeline import intake_guidance

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PERSONA_FILE = _REPO_ROOT / "agents" / "product-analyst.md"


def _plan(**extra):
    plan = {"epics": [{"summary": "Login", "stories": [{"summary": "Form"}]}]}
    plan.update(extra)
    return plan


# ---------------------------------------------------------------------------
# positive: the key is popped and well-formed questions are returned
# ---------------------------------------------------------------------------


def test_well_formed_questions_are_returned_and_the_key_is_removed():
    plan = _plan(open_questions=["Which database?", "Who approves refunds?"])

    questions = intake_guidance.take_questions(plan)

    assert questions == ["Which database?", "Who approves refunds?"]
    assert "open_questions" not in plan


def test_plan_without_the_key_yields_no_questions_and_is_unchanged():
    plan = _plan()
    before = json.dumps(plan, sort_keys=True)

    assert intake_guidance.take_questions(plan) == []
    assert json.dumps(plan, sort_keys=True) == before


def test_control_characters_are_stripped_from_each_question():
    plan = _plan(open_questions=["Which\x00 database\x1b[31m?\n"])

    assert intake_guidance.take_questions(plan) == ["Which database[31m?"]


def test_question_is_truncated_to_300_characters():
    plan = _plan(open_questions=["q" * 1000])

    questions = intake_guidance.take_questions(plan)

    assert questions == ["q" * 300]


def test_list_is_capped_at_five_questions_and_the_rest_are_dropped():
    plan = _plan(open_questions=[f"question {n}?" for n in range(9)])

    questions = intake_guidance.take_questions(plan)

    assert questions == [f"question {n}?" for n in range(5)]
    assert "open_questions" not in plan


def test_non_string_entries_are_dropped_and_the_valid_ones_kept():
    plan = _plan(open_questions=[3, "Which database?", None, {"q": 1}, "Who pays?"])

    assert intake_guidance.take_questions(plan) == ["Which database?", "Who pays?"]


def test_blank_entries_are_dropped():
    plan = _plan(open_questions=["   ", "", "\n\t", "Which database?"])

    assert intake_guidance.take_questions(plan) == ["Which database?"]


# ---------------------------------------------------------------------------
# negative: malformed values yield no questions and never raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Which database should it use?",
        {"question": "Which database?"},
        [1, 2, 3],
        [],
        None,
        True,
        42,
    ],
)
def test_malformed_open_questions_yield_no_questions_and_never_raise(value):
    plan = _plan(open_questions=value)

    assert intake_guidance.take_questions(plan) == []
    assert "open_questions" not in plan


def test_a_list_of_entries_that_are_all_control_characters_yields_no_questions():
    plan = _plan(open_questions=["\x00\x01", "\x7f"])

    assert intake_guidance.take_questions(plan) == []


# ---------------------------------------------------------------------------
# persona: the analyst section that asks for open questions
# ---------------------------------------------------------------------------


def test_persona_has_an_open_questions_section():
    src = _PERSONA_FILE.read_text(encoding="utf-8")

    assert "`open_questions`" in src, (
        "product-analyst.md must document the top-level open_questions key"
    )


def test_persona_section_names_the_five_question_cap():
    src = _PERSONA_FILE.read_text(encoding="utf-8")
    start = src.index("`open_questions`")
    section = src[start:]

    assert "at most 5" in section, (
        "the open_questions section must name the 5-question cap"
    )


def test_persona_section_says_the_key_is_omitted_when_the_request_is_clear():
    src = _PERSONA_FILE.read_text(encoding="utf-8")
    start = src.index("`open_questions`")
    section = src[start:]

    assert "omit" in section.lower(), (
        "the section must say a clear request omits the key"
    )
