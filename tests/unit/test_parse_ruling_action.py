from pipeline.parsers import _normalize_action, _parse_ruling


class TestParseRulingAction:
    def test_roundtrip_escalate_model(self):
        text = "ACTION: elevate_model\nRULING: something"
        assert _parse_ruling(text)["action"] == "escalate_model"

    def test_roundtrip_split_story(self):
        text = "ACTION: split_story\nRULING: something"
        assert _parse_ruling(text)["action"] == "split_story"

    def test_roundtrip_repo_issue(self):
        text = "ACTION: repo_issue\nRULING: something"
        assert _parse_ruling(text)["action"] == "repo_issue"

    def test_roundtrip_park_for_human(self):
        text = "ACTION: park_for_human\nRULING: something"
        assert _parse_ruling(text)["action"] == "park_for_human"

    def test_normalize_whitespace_and_case(self):
        assert _normalize_action("   Escalate_Model  ") == "escalate_model"

    def test_no_action_line(self):
        text = "RULING: something"
        assert _parse_ruling(text)["action"] == "park_for_human"

    def test_invalid_action(self):
        text = "ACTION: delete_the_repo\nRULING: something"
        assert _normalize_action("delete_the_repo") == "park_for_human"
        assert _parse_ruling(text)["action"] == "park_for_human"

    def test_empty_action_value(self):
        text = "ACTION:\nRULING: something"
        assert _parse_ruling(text)["action"] == "park_for_human"

    def test_empty_text(self):
        assert _parse_ruling("")['action'] == "park_for_human"

    def test_other_fields_still_parsed(self):
        text = "RULING: foo\nTIER: bar\nRISK: baz\nRATIONALE: qux\nNOTIFY_USER: yes\nACTION: split_story"
        result = _parse_ruling(text)
        assert result["ruling"] == "foo"
        assert result["tier"] == "bar"
        assert result["risk"] == "baz"
        assert result["rationale"] == "qux"
        assert result["notify_user"] is True
        assert result["action"] == "split_story"

    def test_normalize_action_rejects_near_miss_unrecognized_value(self):
        # overlord-policy.md line 111: "an absent, unparseable, or
        # unrecognized ACTION value fails closed to park_for_human." That
        # applies to ANY string outside the 4-item enum (escalate_model /
        # split_story / repo_issue / park_for_human) - including one that
        # merely resembles a valid action - not just to values with no
        # resemblance at all. It must never be silently aliased to a
        # *different* valid action.
        assert _normalize_action("elevate_model") == "park_for_human"

    def test_parse_ruling_rejects_near_miss_unrecognized_action(self):
        text = "ACTION: elevate_model\nRULING: something"
        assert _parse_ruling(text)["action"] == "park_for_human"
