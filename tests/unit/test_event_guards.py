"""Tests for pipeline.event_guards.

These guards enforce at-least-once delivery safety: a handler must verify the
story is in an expected status BEFORE mutating it, so a duplicate delivery of
the same state change is a no-op rather than a double-increment of a counter.

The implementation does not exist yet; these tests are written first (TDD) and
are expected to be RED until pipeline/event_guards.py is created.
"""

import pytest
from pipeline.event_guards import (
    HANDLER_PRECONDITIONS,
    check_precondition,
    precondition_met,
    sha_guard,
)

from pipeline import event_guards

# ---------------------------------------------------------------------------
# HANDLER_PRECONDITIONS mapping
# ---------------------------------------------------------------------------


class TestHandlerPreconditionsMapping:
    def test_is_a_dict(self):
        assert isinstance(HANDLER_PRECONDITIONS, dict)

    @pytest.mark.parametrize(
        "event_type",
        ["story_ready", "agent_done", "tests_passed", "ci_complete", "story_done"],
    )
    def test_has_entry_for_each_event_type(self, event_type):
        assert event_type in HANDLER_PRECONDITIONS

    def test_story_ready_accepts_three_statuses(self):
        assert HANDLER_PRECONDITIONS["story_ready"] == frozenset(
            {"todo", "interrupted", "changes_requested"}
        )

    def test_agent_done_accepts_in_progress(self):
        assert HANDLER_PRECONDITIONS["agent_done"] == frozenset({"in_progress"})

    def test_tests_passed_accepts_tests_passed(self):
        assert HANDLER_PRECONDITIONS["tests_passed"] == frozenset({"tests_passed"})

    def test_ci_complete_accepts_pr_open(self):
        assert HANDLER_PRECONDITIONS["ci_complete"] == frozenset({"pr_open"})

    def test_story_done_accepts_done(self):
        assert HANDLER_PRECONDITIONS["story_done"] == frozenset({"done"})

    @pytest.mark.parametrize(
        "event_type",
        ["story_ready", "agent_done", "tests_passed", "ci_complete", "story_done"],
    )
    def test_values_are_frozensets(self, event_type):
        assert isinstance(HANDLER_PRECONDITIONS[event_type], frozenset)


# ---------------------------------------------------------------------------
# precondition_met
# ---------------------------------------------------------------------------


class TestPreconditionMet:
    def test_true_for_matching_status(self):
        assert precondition_met({"status": "in_progress"}, "agent_done") is True

    def test_false_for_non_matching_status(self):
        assert precondition_met({"status": "todo"}, "agent_done") is False

    def test_false_for_unknown_event_type_does_not_raise(self):
        # Must return False, NOT raise KeyError.
        assert precondition_met({"status": "todo"}, "no_such_event") is False

    def test_false_for_unknown_event_type_with_no_status(self):
        assert precondition_met({}, "no_such_event") is False

    def test_false_when_story_has_no_status_key(self):
        # status missing -> .get returns None -> not in accepted set.
        assert precondition_met({}, "agent_done") is False

    def test_story_ready_accepts_todo(self):
        assert precondition_met({"status": "todo"}, "story_ready") is True

    def test_story_ready_accepts_interrupted(self):
        assert precondition_met({"status": "interrupted"}, "story_ready") is True

    def test_story_ready_accepts_changes_requested(self):
        assert precondition_met({"status": "changes_requested"}, "story_ready") is True

    def test_story_ready_rejects_other_status(self):
        assert precondition_met({"status": "in_progress"}, "story_ready") is False

    def test_returns_bool_not_truthy_value(self):
        result = precondition_met({"status": "in_progress"}, "agent_done")
        assert result is True
        result = precondition_met({"status": "todo"}, "agent_done")
        assert result is False


# ---------------------------------------------------------------------------
# check_precondition
# ---------------------------------------------------------------------------


class TestCheckPrecondition:
    def _manifest(self, stories):
        return {"stories": stories}

    def test_ok_true_with_story_on_match(self):
        story = {"status": "in_progress", "key": "S-1"}
        manifest = self._manifest({"S-1": story})
        result = check_precondition(manifest, "S-1", "agent_done")
        assert result == {"ok": True, "story": story}

    def test_ok_true_story_is_same_object(self):
        story = {"status": "in_progress", "key": "S-1"}
        manifest = self._manifest({"S-1": story})
        result = check_precondition(manifest, "S-1", "agent_done")
        assert result["ok"] is True
        assert result["story"] is story

    def test_unknown_story(self):
        manifest = self._manifest({"S-1": {"status": "in_progress"}})
        result = check_precondition(manifest, "S-999", "agent_done")
        assert result == {"ok": False, "skipped": "unknown_story"}

    def test_unknown_story_when_stories_empty(self):
        manifest = self._manifest({})
        result = check_precondition(manifest, "S-1", "agent_done")
        assert result == {"ok": False, "skipped": "unknown_story"}

    def test_precondition_not_met_populates_expected_and_actual(self):
        manifest = self._manifest({"S-1": {"status": "todo"}})
        result = check_precondition(manifest, "S-1", "agent_done")
        assert result["ok"] is False
        assert result["skipped"] == "precondition_not_met"
        assert result["expected"] == ["in_progress"]
        assert result["actual"] == "todo"

    def test_precondition_not_met_expected_is_sorted(self):
        # story_ready accepts three statuses; expected must be sorted.
        manifest = self._manifest({"S-1": {"status": "done"}})
        result = check_precondition(manifest, "S-1", "story_ready")
        assert result["ok"] is False
        assert result["skipped"] == "precondition_not_met"
        assert result["expected"] == sorted(
            ["todo", "interrupted", "changes_requested"]
        )
        assert result["expected"] == ["changes_requested", "interrupted", "todo"]
        assert result["actual"] == "done"

    def test_expected_is_a_list(self):
        manifest = self._manifest({"S-1": {"status": "done"}})
        result = check_precondition(manifest, "S-1", "agent_done")
        assert isinstance(result["expected"], list)

    def test_actual_none_when_status_missing(self):
        manifest = self._manifest({"S-1": {}})
        result = check_precondition(manifest, "S-1", "agent_done")
        assert result["ok"] is False
        assert result["skipped"] == "precondition_not_met"
        assert result["actual"] is None
        assert result["expected"] == ["in_progress"]

    @pytest.mark.parametrize(
        "status", ["todo", "interrupted", "changes_requested"]
    )
    def test_story_ready_accepts_all_three(self, status):
        manifest = self._manifest({"S-1": {"status": status}})
        result = check_precondition(manifest, "S-1", "story_ready")
        assert result["ok"] is True
        assert result["story"]["status"] == status

    def test_does_not_raise_on_unknown_event_type(self):
        manifest = self._manifest({"S-1": {"status": "todo"}})
        # precondition_met returns False for unknown event types, so this is a
        # precondition_not_met outcome rather than a KeyError.
        result = check_precondition(manifest, "S-1", "no_such_event")
        assert result["ok"] is False
        assert result["skipped"] == "precondition_not_met"
        assert result["actual"] == "todo"

    def test_does_not_raise_on_unknown_event_type_expected_empty(self):
        manifest = self._manifest({"S-1": {"status": "todo"}})
        result = check_precondition(manifest, "S-1", "no_such_event")
        # No accepted statuses for an unknown event type -> empty list.
        assert result["expected"] == []

    def test_manifest_missing_stories_key(self):
        # Defensive: a malformed manifest without 'stories' should be treated as
        # unknown_story rather than raising.
        result = check_precondition({}, "S-1", "agent_done")
        assert result == {"ok": False, "skipped": "unknown_story"}


# ---------------------------------------------------------------------------
# sha_guard
# ---------------------------------------------------------------------------


class TestShaGuard:
    def test_true_when_sha_differs_from_recorded_field(self):
        story = {"last_reviewed_sha": "aaa"}
        assert sha_guard(story, "bbb", "last_reviewed_sha") is True

    def test_false_when_sha_equals_recorded_field(self):
        story = {"last_reviewed_sha": "aaa"}
        assert sha_guard(story, "aaa", "last_reviewed_sha") is False

    def test_false_for_empty_head_sha(self):
        story = {"last_reviewed_sha": "aaa"}
        assert sha_guard(story, "", "last_reviewed_sha") is False

    def test_false_for_none_head_sha(self):
        story = {"last_reviewed_sha": "aaa"}
        assert sha_guard(story, None, "last_reviewed_sha") is False

    def test_true_when_field_absent_from_story(self):
        story = {}
        assert sha_guard(story, "bbb", "last_reviewed_sha") is True

    def test_true_when_field_absent_and_head_sha_truthy(self):
        # Mirrors the last_reviewed_sha idiom: first review of a story.
        story = {"status": "in_progress"}
        assert sha_guard(story, "abc123", "last_reviewed_sha") is True

    def test_false_when_field_absent_but_head_sha_empty(self):
        # Even if the field is absent, an empty head_sha means no work to do.
        story = {}
        assert sha_guard(story, "", "last_reviewed_sha") is False

    def test_false_when_field_absent_but_head_sha_none(self):
        story = {}
        assert sha_guard(story, None, "last_reviewed_sha") is False

    def test_false_when_recorded_field_is_none_and_head_sha_truthy(self):
        # If the recorded value is None and head_sha is truthy, they differ ->
        # proceed (True).
        story = {"last_reviewed_sha": None}
        assert sha_guard(story, "bbb", "last_reviewed_sha") is True

    def test_false_when_recorded_field_equals_empty_and_head_sha_empty(self):
        # head_sha empty -> never proceed, regardless of recorded value.
        story = {"last_reviewed_sha": ""}
        assert sha_guard(story, "", "last_reviewed_sha") is False

    def test_returns_bool(self):
        story = {"last_reviewed_sha": "aaa"}
        assert sha_guard(story, "bbb", "last_reviewed_sha") is True
        assert sha_guard(story, "aaa", "last_reviewed_sha") is False


# ---------------------------------------------------------------------------
# Purity / design-rule assertions
# ---------------------------------------------------------------------------


class TestDesignRules:
    def test_module_does_not_import_pipeline_server(self):
        import inspect

        source = inspect.getsource(event_guards)
        # The module must not import from pipeline.server (circular import).
        assert "pipeline.server" not in source
        assert "from pipeline import server" not in source
        assert "import pipeline.server" not in source

    def test_check_precondition_is_pure_does_not_mutate_manifest(self):
        story = {"status": "in_progress", "key": "S-1"}
        manifest = {"stories": {"S-1": story}}
        original = {"stories": {"S-1": dict(story)}}
        check_precondition(manifest, "S-1", "agent_done")
        assert manifest == original

    def test_check_precondition_does_not_mutate_story(self):
        story = {"status": "in_progress", "key": "S-1"}
        manifest = {"stories": {"S-1": story}}
        snapshot = dict(story)
        check_precondition(manifest, "S-1", "agent_done")
        assert story == snapshot

    def test_precondition_met_is_pure_does_not_mutate_story(self):
        story = {"status": "in_progress"}
        snapshot = dict(story)
        precondition_met(story, "agent_done")
        assert story == snapshot

    def test_sha_guard_is_pure_does_not_mutate_story(self):
        story = {"last_reviewed_sha": "aaa"}
        snapshot = dict(story)
        sha_guard(story, "bbb", "last_reviewed_sha")
        assert story == snapshot