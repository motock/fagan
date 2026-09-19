"""LD90: the executor steering must not tell a weak model to revert a task's
required change just because a PRE-EXISTING test fails.

Three prompt strings previously carried the unqualified sentence "if a test
fails, the bug is in the implementation", which on 2026-09-18 drove an
executor whose story was to DELETE a config key to re-add it, because a
pre-existing survivor-list test failed. The new rule keeps "tests written for
THIS task mean fix the implementation" but adds: when a PRE-EXISTING test
(one this task did not add) fails and the change the task asks for is what
breaks it, stop and report the conflict instead of undoing the task's
required change.

The "NEVER edit, rename, weaken, or delete the test files" prohibition stays
intact in all three strings, and the constant keeps its name and its
re-exports (planner/server re-export, dispatch._ServerRef).

These tests assert membership of the phrases this story adds/keeps - never
the exact total contents of a shared prompt string or __all__ list, so later
stories can extend them freely.
"""

import pytest

from pipeline import dispatch, planner, server, test_author

# (module, attribute name, prohibition anchor that must survive verbatim)
_TARGETS = [
    pytest.param(
        test_author,
        "_NEVER_TOUCH_TESTS_STEERING",
        "NEVER edit",
        id="test_author._NEVER_TOUCH_TESTS_STEERING",
    ),
    pytest.param(
        planner,
        "_PLANNER_SYSTEM",
        "NEVER edit, rename,",
        id="planner._PLANNER_SYSTEM",
    ),
    pytest.param(
        planner,
        "_REWORK_PLANNER_SYSTEM",
        "NEVER edit, rename,",
        id="planner._REWORK_PLANNER_SYSTEM",
    ),
]

# Sentences that must survive the reword, per target. The planner prompts
# carry the EDITING MECHANICS block and the state-persistence / follow-up
# worked-example guidance; the bare constant does not.
_SURVIVOR_SUBSTRINGS = {
    "_NEVER_TOUCH_TESTS_STEERING": ("NEVER", "test_", "implementation"),
    "_PLANNER_SYSTEM": (
        "NEVER",
        "test_",
        "implementation file",
        "create_file",
        "str_replace",
        "persist",
        "follow-up",
        "EDITING MECHANICS",
    ),
    "_REWORK_PLANNER_SYSTEM": (
        "NEVER",
        "test_",
        "implementation file",
        "create_file",
        "str_replace",
        "persist",
        "follow-up",
        "EDITING MECHANICS",
    ),
}

# The phrases the new rule is built from.
_NEW_RULE_PHRASES = (
    "PRE-EXISTING test",
    "do NOT undo the task's required change",
    "report the conflict",
    "naming the failing test and the task requirement it contradicts",
    "written for THIS task",
)

# The unqualified old sentence, lowercased for a case-insensitive check.
_OLD_UNQUALIFIED_SENTENCE = "if a test fails, the bug is in the implementation"


def _steering(module, attr_name):
    """Fetch the prompt string by name so a missing/renamed symbol fails as a
    clear AttributeError inside the test rather than at collection time."""
    value = getattr(module, attr_name)
    assert isinstance(value, str), f"{attr_name} must be a str, got {type(value)!r}"
    assert value.strip(), f"{attr_name} must not be empty"
    return value


@pytest.mark.parametrize("module, attr_name, prohibition_anchor", _TARGETS)
def test_never_touch_tests_prohibition_survives(module, attr_name, prohibition_anchor):
    text = _steering(module, attr_name)
    assert "delete the test files (anything matching" in text
    assert prohibition_anchor in text
    assert (
        "NEVER edit, rename, weaken, or delete the test files "
        "(anything matching test_*.py)" in text
    )


@pytest.mark.parametrize("module, attr_name, prohibition_anchor", _TARGETS)
def test_preexisting_test_conflict_rule_is_present(module, attr_name, prohibition_anchor):
    text = _steering(module, attr_name)
    for phrase in _NEW_RULE_PHRASES:
        assert phrase in text, f"{attr_name} is missing {phrase!r}"


@pytest.mark.parametrize("module, attr_name, prohibition_anchor", _TARGETS)
def test_unqualified_old_sentence_is_gone(module, attr_name, prohibition_anchor):
    text = _steering(module, attr_name)
    assert _OLD_UNQUALIFIED_SENTENCE not in text.lower(), (
        f"{attr_name} still carries the unqualified "
        f"{_OLD_UNQUALIFIED_SENTENCE!r} sentence"
    )


@pytest.mark.parametrize("module, attr_name, prohibition_anchor", _TARGETS)
def test_survivor_sentences_are_untouched(module, attr_name, prohibition_anchor):
    text = _steering(module, attr_name)
    for substring in _SURVIVOR_SUBSTRINGS[attr_name]:
        assert substring in text, f"{attr_name} lost the {substring!r} sentence"


def test_planner_reexport_points_at_reworded_constant():
    # planner re-exports the constant by name; the re-export must still be the
    # very same (reworded) object, not a stale copy.
    assert (
        planner._NEVER_TOUCH_TESTS_STEERING
        is test_author._NEVER_TOUCH_TESTS_STEERING
    )


def test_server_reexport_points_at_reworded_constant():
    assert (
        server._NEVER_TOUCH_TESTS_STEERING
        is test_author._NEVER_TOUCH_TESTS_STEERING
    )


def test_constant_name_is_exported_from_both_modules():
    assert "_NEVER_TOUCH_TESTS_STEERING" in test_author.__all__
    assert "_NEVER_TOUCH_TESTS_STEERING" in planner.__all__


def test_dispatch_server_ref_still_resolves_the_constant():
    ref = dispatch._NEVER_TOUCH_TESTS_STEERING
    assert isinstance(ref, dispatch._ServerRef)
    assert ref._name == "_NEVER_TOUCH_TESTS_STEERING"
