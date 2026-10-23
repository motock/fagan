import copy

import pytest

from pipeline.context_sources import normalize_context_sources, validate_context_config


def _source(**overrides):
    source = {
        "name": "org-ux-guide",
        "provider": "dir",
        "uri": "/mnt/org-context/ux",
        "phases": ["dispatch", "review"],
    }
    source.update(overrides)
    return source


def _context(**source_overrides):
    return {"sources": [_source(**source_overrides)]}


def _errors_mention(errors, text):
    return any(text in error for error in errors)


def test_should_accept_valid_context():
    context = {
        "max_total_chars": 60000,
        "sources": [
            _source(include=["**/*.md"], exclude=["**/drafts/**"], max_chars=20000,
                    authority="reference", priority=10, required=True, mode="prompt"),
            _source(name="legal", authority="policy", phases=["security_review"]),
        ],
    }
    assert validate_context_config(context) == []


def test_should_apply_defaults_and_keep_declaration_order():
    context = {"sources": [_source(name="b-src"), _source(name="a-src")]}

    result = normalize_context_sources(context)

    assert [s["name"] for s in result] == ["b-src", "a-src"]
    assert result[0] == {
        "name": "b-src",
        "provider": "dir",
        "uri": "/mnt/org-context/ux",
        "include": ["**/*"],
        "exclude": [],
        "phases": ["dispatch", "review"],
        "mode": "prompt",
        "max_chars": 20000,
        "authority": "reference",
        "priority": 0,
        "required": True,
    }


def test_should_not_mutate_input_when_normalizing():
    context = _context()
    snapshot = copy.deepcopy(context)

    normalize_context_sources(context)

    assert context == snapshot


def test_should_keep_explicit_values_when_normalizing():
    context = _context(max_chars=100000, authority="policy", priority=-3, required=False)

    result = normalize_context_sources(context)[0]

    assert (result["max_chars"], result["authority"], result["priority"], result["required"]) == (
        100000, "policy", -3, False)


@pytest.mark.parametrize("bad", [None, [], "x", 5])
def test_should_reject_non_dict_context(bad):
    assert validate_context_config(bad) != []


def test_should_reject_missing_sources():
    assert _errors_mention(validate_context_config({}), "sources")


def test_should_reject_non_list_sources():
    assert _errors_mention(validate_context_config({"sources": {"name": "x"}}), "sources")


def test_should_reject_empty_sources():
    assert _errors_mention(validate_context_config({"sources": []}), "sources")


def test_should_reject_non_dict_source_entry():
    assert validate_context_config({"sources": ["nope"]}) != []


def test_should_reject_missing_name():
    source = _source()
    del source["name"]
    assert _errors_mention(validate_context_config({"sources": [source]}), "name")


@pytest.mark.parametrize("name", ["", "Upper", "-lead", "has space", "a" * 65, "under_score", 5])
def test_should_reject_bad_name(name):
    assert _errors_mention(validate_context_config(_context(name=name)), "name")


def test_should_accept_name_at_max_length():
    assert validate_context_config(_context(name="a" * 64)) == []


def test_should_reject_duplicate_name():
    context = {"sources": [_source(), _source()]}
    assert _errors_mention(validate_context_config(context), "duplicate")


def test_should_reject_unsupported_provider():
    assert _errors_mention(validate_context_config(_context(provider="git")), "provider")


def test_should_reject_missing_provider():
    source = _source()
    del source["provider"]
    assert _errors_mention(validate_context_config({"sources": [source]}), "provider")


@pytest.mark.parametrize("uri", ["relative/path", "", "/a/../b", "/a/..", 5])
def test_should_reject_bad_uri(uri):
    assert _errors_mention(validate_context_config(_context(uri=uri)), "uri")


def test_should_accept_uri_with_dotted_segment_name():
    assert validate_context_config(_context(uri="/a/..b/c")) == []


def test_should_reject_empty_phases():
    assert _errors_mention(validate_context_config(_context(phases=[])), "phases")


def test_should_reject_missing_phases():
    source = _source()
    del source["phases"]
    assert _errors_mention(validate_context_config({"sources": [source]}), "phases")


@pytest.mark.parametrize("phase", ["planner", "test_author", "decompose", "bogus"])
def test_should_reject_unsupported_phase(phase):
    assert _errors_mention(validate_context_config(_context(phases=["dispatch", phase])), phase)


def test_should_reject_non_list_phases():
    assert _errors_mention(validate_context_config(_context(phases="dispatch")), "phases")


def test_should_reject_wrong_mode():
    assert _errors_mention(validate_context_config(_context(mode="files")), "mode")


def test_should_reject_unknown_source_key():
    assert _errors_mention(validate_context_config(_context(bogus=1)), "bogus")


def test_should_reject_unknown_plan_level_key():
    context = _context()
    context["bogus"] = 1
    assert _errors_mention(validate_context_config(context), "bogus")


@pytest.mark.parametrize("key", ["include", "exclude"])
@pytest.mark.parametrize("patterns", [["/abs/*.md"], ["../x"], ["a/../b"], "**/*.md", [5]])
def test_should_reject_bad_glob_lists(key, patterns):
    assert _errors_mention(validate_context_config(_context(**{key: patterns})), key)


@pytest.mark.parametrize("value", [0, -1, 100001, "5", 1.5, True])
def test_should_reject_bad_max_chars(value):
    assert _errors_mention(validate_context_config(_context(max_chars=value)), "max_chars")


def test_should_accept_max_chars_boundaries():
    assert validate_context_config(_context(max_chars=1)) == []
    assert validate_context_config(_context(max_chars=100000)) == []


def test_should_reject_unknown_authority():
    assert _errors_mention(validate_context_config(_context(authority="mandatory")), "authority")


@pytest.mark.parametrize("value", ["1", 1.5, True, None])
def test_should_reject_bad_priority(value):
    assert _errors_mention(validate_context_config(_context(priority=value)), "priority")


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_should_reject_non_bool_required(value):
    assert _errors_mention(validate_context_config(_context(required=value)), "required")


@pytest.mark.parametrize("value", [0, -1, "10", True, 1.5])
def test_should_reject_bad_max_total_chars(value):
    context = _context()
    context["max_total_chars"] = value
    assert _errors_mention(validate_context_config(context), "max_total_chars")


def test_should_report_every_error_not_just_first():
    errors = validate_context_config(_context(provider="git", mode="files", authority="x"))
    assert len(errors) == 3


def test_normalize_should_raise_value_error_listing_errors():
    with pytest.raises(ValueError, match="provider"):
        normalize_context_sources(_context(provider="git"))
