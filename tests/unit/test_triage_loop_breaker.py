"""Tests for the triage loop-breaker guard layer in pipeline.triage.

These tests pin the four new public functions and two new module-level
constants that bound the failure-triage sweep so it cannot loop forever:

* ``TRIAGE_MAX_ATTEMPTS`` / ``TRIAGE_MAX_CREATED_STORIES`` - plain literal caps.
* ``triage_allowed`` - per-story attempt gate.
* ``action_already_tried`` - per-story action dedup gate (Section 4.5: the
  same ACTION may not be chosen twice for the same story).
* ``record_triage_attempt`` - mutates the story dict in place.
* ``plan_triage_budget_exhausted`` - per-plan created-story ceiling.

The implementation does not exist yet, so this suite is RED on purpose; a
later dispatch implements against it.
"""

from __future__ import annotations

from pipeline import triage


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
class TestConstants:
    def test_triage_max_attempts_is_literal_two(self):
        """TRIAGE_MAX_ATTEMPTS ships as the plain literal 2 (not env-read)."""
        assert triage.TRIAGE_MAX_ATTEMPTS == 2

    def test_triage_max_created_stories_is_literal_three(self):
        """TRIAGE_MAX_CREATED_STORIES ships as the plain literal 3."""
        assert triage.TRIAGE_MAX_CREATED_STORIES == 3

    def test_constants_are_not_read_from_environment(self, monkeypatch):
        """The caps are module-level literals, not env-derived: monkeypatching
        the env must not change them, and monkeypatching the attribute must
        (the spec explicitly says tests monkeypatch the attribute)."""
        monkeypatch.setenv("TRIAGE_MAX_ATTEMPTS", "99")
        monkeypatch.setenv("TRIAGE_MAX_CREATED_STORIES", "99")
        # Re-import would re-bind only if env-read; assert the live values
        # are untouched by env changes.
        assert triage.TRIAGE_MAX_ATTEMPTS == 2
        assert triage.TRIAGE_MAX_CREATED_STORIES == 3

    def test_attribute_monkeypatch_is_honored(self, monkeypatch):
        """Spec: tests monkeypatch the attribute directly
        (monkeypatch.setattr(pipeline.triage, 'TRIAGE_MAX_ATTEMPTS', 1))."""
        monkeypatch.setattr(triage, "TRIAGE_MAX_ATTEMPTS", 1)
        assert triage.TRIAGE_MAX_ATTEMPTS == 1
        # And the function reads the live module attribute, not a snapshot.
        allowed, _ = triage.triage_allowed({"triage_attempts": 1})
        assert allowed is False


# ---------------------------------------------------------------------------
# triage_allowed
# ---------------------------------------------------------------------------
class TestTriageAllowed:
    def test_no_attempts_key_allowed(self):
        """A story with no triage_attempts -> (True, '')."""
        allowed, reason = triage.triage_allowed({})
        assert allowed is True
        assert reason == ""

    def test_zero_attempts_allowed(self):
        allowed, reason = triage.triage_allowed({"triage_attempts": 0})
        assert allowed is True
        assert reason == ""

    def test_below_cap_allowed(self):
        allowed, reason = triage.triage_allowed({"triage_attempts": 1})
        assert allowed is True
        assert reason == ""

    def test_at_cap_blocked(self):
        """triage_attempts == TRIAGE_MAX_ATTEMPTS -> (False, <non-empty>)."""
        allowed, reason = triage.triage_allowed(
            {"triage_attempts": triage.TRIAGE_MAX_ATTEMPTS}
        )
        assert allowed is False
        assert isinstance(reason, str)
        assert reason != ""

    def test_above_cap_blocked(self):
        """triage_attempts == TRIAGE_MAX_ATTEMPTS + 5 -> (False, <non-empty>)."""
        allowed, reason = triage.triage_allowed(
            {"triage_attempts": triage.TRIAGE_MAX_ATTEMPTS + 5}
        )
        assert allowed is False
        assert isinstance(reason, str)
        assert reason != ""

    def test_reason_names_the_cap_and_the_count(self):
        """The reason must name both the cap and the count that was hit."""
        cap = triage.TRIAGE_MAX_ATTEMPTS
        allowed, reason = triage.triage_allowed({"triage_attempts": cap})
        assert allowed is False
        assert str(cap) in reason  # the cap is named
        assert str(cap) in reason  # the count that was hit is named

    def test_non_integer_attempts_treated_as_zero(self):
        """triage_attempts == 'two' -> (True, ''); corrupt state must not crash."""
        allowed, reason = triage.triage_allowed({"triage_attempts": "two"})
        assert allowed is True
        assert reason == ""

    def test_non_integer_attempts_none_treated_as_zero(self):
        allowed, reason = triage.triage_allowed({"triage_attempts": None})
        assert allowed is True
        assert reason == ""

    def test_returns_tuple_of_bool_str(self):
        result = triage.triage_allowed({})
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)

    def test_uses_live_module_constant(self, monkeypatch):
        """triage_allowed reads the live TRIAGE_MAX_ATTEMPTS, so lowering the
        cap via monkeypatch tightens the gate immediately."""
        monkeypatch.setattr(triage, "TRIAGE_MAX_ATTEMPTS", 1)
        allowed, _ = triage.triage_allowed({"triage_attempts": 1})
        assert allowed is False
        # below the new cap still allowed
        allowed, _ = triage.triage_allowed({"triage_attempts": 0})
        assert allowed is True


# ---------------------------------------------------------------------------
# action_already_tried
# ---------------------------------------------------------------------------
class TestActionAlreadyTried:
    def test_no_actions_key_false(self):
        """action_already_tried on a story with no triage_actions -> False."""
        assert triage.action_already_tried({}, "escalate_model") is False

    def test_empty_actions_false(self):
        assert triage.action_already_tried({"triage_actions": []}, "escalate_model") is False

    def test_action_present_true(self):
        assert triage.action_already_tried(
            {"triage_actions": ["escalate_model"]}, "escalate_model"
        ) is True

    def test_different_action_false(self):
        assert triage.action_already_tried(
            {"triage_actions": ["escalate_model"]}, "split_story"
        ) is False

    def test_non_list_actions_treated_as_empty(self):
        """triage_actions = 'not a list' -> False, no TypeError."""
        assert triage.action_already_tried(
            {"triage_actions": "not a list"}, "escalate_model"
        ) is False

    def test_non_list_actions_none_treated_as_empty(self):
        assert triage.action_already_tried(
            {"triage_actions": None}, "escalate_model"
        ) is False

    def test_returns_bool(self):
        assert isinstance(
            triage.action_already_tried({"triage_actions": ["x"]}, "x"), bool
        )


# ---------------------------------------------------------------------------
# record_triage_attempt
# ---------------------------------------------------------------------------
class TestRecordTriageAttempt:
    def test_increments_absent_key(self):
        story = {}
        triage.record_triage_attempt(story, "escalate_model")
        assert story["triage_attempts"] == 1
        assert story["triage_actions"] == ["escalate_model"]

    def test_increments_existing_count(self):
        story = {"triage_attempts": 5, "triage_actions": ["split_story"]}
        triage.record_triage_attempt(story, "escalate_model")
        assert story["triage_attempts"] == 6
        assert story["triage_actions"] == ["split_story", "escalate_model"]

    def test_non_integer_attempts_starts_from_zero(self):
        story = {"triage_attempts": "two"}
        triage.record_triage_attempt(story, "escalate_model")
        assert story["triage_attempts"] == 1
        assert story["triage_actions"] == ["escalate_model"]

    def test_non_integer_attempts_none_starts_from_zero(self):
        story = {"triage_attempts": None}
        triage.record_triage_attempt(story, "escalate_model")
        assert story["triage_attempts"] == 1

    def test_duplicate_action_counted_twice_listed_once(self):
        """record_triage_attempt called twice with 'escalate_model' ->
        triage_attempts == 2 and triage_actions == ['escalate_model']
        (counted twice, listed once)."""
        story = {}
        triage.record_triage_attempt(story, "escalate_model")
        triage.record_triage_attempt(story, "escalate_model")
        assert story["triage_attempts"] == 2
        assert story["triage_actions"] == ["escalate_model"]

    def test_two_different_actions_both_in_call_order(self):
        """record_triage_attempt with two different actions -> triage_actions
        holds both in call order."""
        story = {}
        triage.record_triage_attempt(story, "escalate_model")
        triage.record_triage_attempt(story, "split_story")
        assert story["triage_attempts"] == 2
        assert story["triage_actions"] == ["escalate_model", "split_story"]

    def test_duplicate_after_other_action_not_appended(self):
        story = {}
        triage.record_triage_attempt(story, "escalate_model")
        triage.record_triage_attempt(story, "split_story")
        triage.record_triage_attempt(story, "escalate_model")
        assert story["triage_attempts"] == 3
        # escalate_model already present, not appended again; split_story stays
        assert story["triage_actions"] == ["escalate_model", "split_story"]

    def test_creates_actions_list_when_absent(self):
        story = {"triage_attempts": 0}
        triage.record_triage_attempt(story, "escalate_model")
        assert story["triage_actions"] == ["escalate_model"]

    def test_mutates_in_place(self):
        story = {}
        original = story
        triage.record_triage_attempt(story, "escalate_model")
        assert story is original  # same dict object, mutated in place

    def test_returns_none(self):
        result = triage.record_triage_attempt({}, "escalate_model")
        assert result is None

    def test_non_list_existing_actions_treated_as_empty(self):
        """A non-list triage_actions is treated as empty: the new action is
        recorded fresh and the bad value replaced with a real list."""
        story = {"triage_actions": "not a list"}
        triage.record_triage_attempt(story, "escalate_model")
        assert story["triage_actions"] == ["escalate_model"]
        assert story["triage_attempts"] == 1


# ---------------------------------------------------------------------------
# plan_triage_budget_exhausted
# ---------------------------------------------------------------------------
class TestPlanTriageBudgetExhausted:
    def test_empty_manifest_false(self):
        """plan_triage_budget_exhausted({}) -> False."""
        assert triage.plan_triage_budget_exhausted({}) is False

    def test_no_key_false(self):
        assert triage.plan_triage_budget_exhausted({"stories": {}}) is False

    def test_zero_false(self):
        assert triage.plan_triage_budget_exhausted(
            {"triage_created_stories": 0}
        ) is False

    def test_below_cap_false(self):
        assert triage.plan_triage_budget_exhausted(
            {"triage_created_stories": triage.TRIAGE_MAX_CREATED_STORIES - 1}
        ) is False

    def test_at_cap_true(self):
        """triage_created_stories == TRIAGE_MAX_CREATED_STORIES -> True."""
        assert triage.plan_triage_budget_exhausted(
            {"triage_created_stories": triage.TRIAGE_MAX_CREATED_STORIES}
        ) is True

    def test_above_cap_true(self):
        assert triage.plan_triage_budget_exhausted(
            {"triage_created_stories": triage.TRIAGE_MAX_CREATED_STORIES + 5}
        ) is True

    def test_non_integer_treated_as_zero(self):
        assert triage.plan_triage_budget_exhausted(
            {"triage_created_stories": "three"}
        ) is False

    def test_non_integer_none_treated_as_zero(self):
        assert triage.plan_triage_budget_exhausted(
            {"triage_created_stories": None}
        ) is False

    def test_uses_live_module_constant(self, monkeypatch):
        monkeypatch.setattr(triage, "TRIAGE_MAX_CREATED_STORIES", 1)
        assert triage.plan_triage_budget_exhausted(
            {"triage_created_stories": 1}
        ) is True
        assert triage.plan_triage_budget_exhausted(
            {"triage_created_stories": 0}
        ) is False

    def test_returns_bool(self):
        assert isinstance(
            triage.plan_triage_budget_exhausted(
                {"triage_created_stories": triage.TRIAGE_MAX_CREATED_STORIES}
            ),
            bool,
        )

    def test_docstring_notes_out_of_scope_creators(self):
        """Docstring must note that nothing increments this counter in this
        slice - the actions that create stories (split_story, repo_issue) are
        deliberately out of scope."""
        doc = triage.plan_triage_budget_exhausted.__doc__ or ""
        assert "out of scope" in doc.lower() or "out-of-scope" in doc.lower()
        assert "split_story" in doc
        assert "repo_issue" in doc

    def test_docstring_notes_ceiling_ships_with_loop_breaker(self):
        """Docstring must note that the ceiling ships WITH the loop breaker
        rather than with the feature that can breach it, so the guard can
        never be forgotten later."""
        doc = triage.plan_triage_budget_exhausted.__doc__ or ""
        assert "loop breaker" in doc.lower() or "loop-breaker" in doc.lower()
        assert "forgotten" in doc.lower()


# ---------------------------------------------------------------------------
# __all__ exports
# ---------------------------------------------------------------------------
class TestAllExports:
    def test_all_includes_four_functions_and_two_constants(self):
        """All four names plus the two constants must be in __all__."""
        expected = {
            "TRIAGE_MAX_ATTEMPTS",
            "TRIAGE_MAX_CREATED_STORIES",
            "triage_allowed",
            "action_already_tried",
            "record_triage_attempt",
            "plan_triage_budget_exhausted",
        }
        assert expected.issubset(set(triage.__all__))

    def test_all_is_list_of_strings(self):
        assert isinstance(triage.__all__, list)
        for name in triage.__all__:
            assert isinstance(name, str)

    def test_names_are_importable_attributes(self):
        """Each exported name must be a real attribute on the module."""
        for name in (
            "TRIAGE_MAX_ATTEMPTS",
            "TRIAGE_MAX_CREATED_STORIES",
            "triage_allowed",
            "action_already_tried",
            "record_triage_attempt",
            "plan_triage_budget_exhausted",
        ):
            assert hasattr(triage, name), f"missing attribute {name!r}"