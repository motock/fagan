from pipeline.scope_conflict import (
    SCOPE_CONFLICT_PREFIX,
    ScopeConflict,
    parse_scope_conflict,
)
from pipeline.scope_gate import SCOPE_GATE_REMEDY


def _line(path, reason="needed"):
    return f"SCOPE-CONFLICT: {path} :: {reason}"


def test_should_parse_one_marker():
    text = "done\n" + _line("pipeline/a.py", "  brief needs it ")
    assert parse_scope_conflict(text) == [ScopeConflict("pipeline/a.py", "brief needs it")]


def test_should_accept_five_and_drop_sixth():
    text = "\n".join(_line(f"f{i}.py") for i in range(6))
    assert [c.path for c in parse_scope_conflict(text)] == [f"f{i}.py" for i in range(5)]


def test_should_skip_absolute_path():
    assert parse_scope_conflict(_line("/etc/passwd")) == []


def test_should_skip_parent_component():
    assert parse_scope_conflict(_line("a/../b.py")) == []


def test_should_skip_backslash_path():
    assert parse_scope_conflict(_line("a\\b.py")) == []


def test_should_skip_empty_path():
    assert parse_scope_conflict(_line("")) == []


def test_should_skip_nul_path():
    assert parse_scope_conflict(_line("a\x00b.py")) == []


def test_should_keep_dotdot_inside_a_name():
    assert [c.path for c in parse_scope_conflict(_line("a..b.py"))] == ["a..b.py"]


def test_should_truncate_reason_at_200():
    assert len(parse_scope_conflict(_line("a.py", "x" * 500))[0].reason) == 200


def test_should_strip_control_characters_from_reason():
    assert parse_scope_conflict(_line("a.py", "ab\x07c\x1bd"))[0].reason == "abcd"


def test_should_keep_first_duplicate_path():
    text = _line("a.py", "first") + "\n" + _line("a.py", "second")
    assert [c.reason for c in parse_scope_conflict(text)] == ["first"]


def test_should_return_empty_for_none():
    assert parse_scope_conflict(None) == []


def test_should_return_empty_for_empty_text():
    assert parse_scope_conflict("") == []


def test_should_return_empty_without_markers():
    assert parse_scope_conflict("nothing here\nSCOPE-CONFLICT: no separator") == []


def test_remedy_contains_marker_form_and_request_decision():
    assert f"{SCOPE_CONFLICT_PREFIX} <path> :: <why the brief needs it>" in SCOPE_GATE_REMEDY
    assert "request_decision" in SCOPE_GATE_REMEDY
    assert "do not revert" in SCOPE_GATE_REMEDY
