"""Per-story tests: the `split` payload key on `_parse_ruling` (story OPSA-4).

The overlord's output contract gains a `SPLIT: <child A> || <child B>` line.
`_parse_ruling` must surface the child summaries under a new `split` key:
every line starting with `SPLIT:` is split on `||`, each side stripped, empty
strings dropped, and the surviving children collected in order.

The parser is deliberately NOT the fail-closed gate: an absent or malformed
payload yields `split: []` and must leave the parsed ACTION untouched. The
executor (a later sibling story) is what fails closed on an empty payload.
`_normalize_action`'s fail-closed behaviour is out of scope and must not move.
"""

from pipeline.parsers import TRIAGE_ACTIONS, _normalize_action, _parse_ruling


class TestSplitPayloadHappyPath:
    def test_single_split_line_yields_two_children(self):
        text = "SPLIT: child A || child B"
        assert _parse_ruling(text)["split"] == ["child A", "child B"]

    def test_split_children_are_whitespace_stripped(self):
        text = "SPLIT:    child A    ||    child B   "
        assert _parse_ruling(text)["split"] == ["child A", "child B"]

    def test_multiple_split_lines_are_collected_in_order(self):
        text = "SPLIT: A1 || A2\nRULING: x\nSPLIT: B1 || B2"
        assert _parse_ruling(text)["split"] == ["A1", "A2", "B1", "B2"]

    def test_split_is_a_list_of_strings(self):
        result = _parse_ruling("SPLIT: A || B")["split"]
        assert isinstance(result, list)
        assert all(isinstance(item, str) for item in result)

    def test_split_alongside_a_valid_action_parses_both_keys(self):
        text = "ACTION: split_story\nSPLIT: child A || child B"
        result = _parse_ruling(text)
        assert result["action"] == "split_story"
        assert result["split"] == ["child A", "child B"]

    def test_split_does_not_disturb_the_other_contract_fields(self):
        text = (
            "RULING: foo\nTIER: notify-async\nRISK: medium\n"
            "RATIONALE: bar\nNOTIFY_USER: yes\n"
            "ACTION: split_story\nSPLIT: child A || child B"
        )
        result = _parse_ruling(text)
        assert result["ruling"] == "foo"
        assert result["tier"] == "notify-async"
        assert result["risk"] == "medium"
        assert result["rationale"] == "bar"
        assert result["notify_user"] is True
        assert result["action"] == "split_story"
        assert result["split"] == ["child A", "child B"]


class TestSplitPayloadAbsentOrMalformed:
    def test_no_split_line_yields_empty_list(self):
        text = "RULING: something\nACTION: park_for_human"
        assert _parse_ruling(text)["split"] == []

    def test_empty_text_yields_empty_list(self):
        assert _parse_ruling("")["split"] == []

    def test_split_key_is_always_present(self):
        assert "split" in _parse_ruling("RULING: something")

    def test_split_line_with_empty_payload(self):
        assert _parse_ruling("SPLIT:")["split"] == []

    def test_split_line_with_whitespace_only_payload(self):
        assert _parse_ruling("SPLIT:      ")["split"] == []

    def test_split_line_with_both_sides_empty(self):
        assert _parse_ruling("SPLIT: ||")["split"] == []

    def test_split_line_with_an_empty_side(self):
        assert _parse_ruling("SPLIT: child A ||")["split"] == []

    def test_split_line_without_separator(self):
        assert _parse_ruling("SPLIT: child A")["split"] == []

    def test_split_token_mid_line_is_not_a_split_line(self):
        text = "RATIONALE: we could SPLIT: child A || child B"
        assert _parse_ruling(text)["split"] == []


class TestActionIsPreservedRegardlessOfSplitPayload:
    def test_empty_split_payload_does_not_change_the_action(self):
        text = "ACTION: split_story\nSPLIT:"
        result = _parse_ruling(text)
        assert result["action"] == "split_story"
        assert result["split"] == []

    def test_malformed_split_payload_does_not_change_the_action(self):
        text = "ACTION: split_story\nSPLIT: child A"
        result = _parse_ruling(text)
        assert result["action"] == "split_story"
        assert result["split"] == []

    def test_unrecognized_action_still_fails_closed_with_split_present(self):
        text = "ACTION: elevate_model\nSPLIT: child A || child B"
        result = _parse_ruling(text)
        assert result["action"] == "park_for_human"
        assert result["split"] == ["child A", "child B"]

    def test_absent_action_still_defaults_to_park_for_human_with_split(self):
        text = "SPLIT: child A || child B"
        assert _parse_ruling(text)["action"] == "park_for_human"


class TestNormalizeActionFailClosedUntouched:
    def test_recognized_actions_pass_through(self):
        for action in ("escalate_model", "split_story", "repo_issue", "park_for_human"):
            assert _normalize_action(action) == action

    def test_whitespace_and_case_are_normalized(self):
        assert _normalize_action("   SPLIT_STORY  ") == "split_story"

    def test_unrecognized_value_fails_closed(self):
        assert _normalize_action("delete_the_repo") == "park_for_human"
        assert _normalize_action("elevate_model") == "park_for_human"

    def test_non_string_fails_closed(self):
        assert _normalize_action(None) == "park_for_human"
        assert _normalize_action(42) == "park_for_human"

    def test_triage_action_set_still_contains_the_four_actions(self):
        for action in ("escalate_model", "split_story", "repo_issue", "park_for_human"):
            assert action in TRIAGE_ACTIONS
