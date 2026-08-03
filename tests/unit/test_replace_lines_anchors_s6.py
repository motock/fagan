"""Unit tests for the optional expect_first/expect_last anchor wiring in the
replace_lines tool of BOTH agent scripts (scripts/local_agent.py and
scripts/local_agent_oracle.py).

These tests drive run_tool (not edit_guards directly) so they fail until the
implementation actually calls edit_guards.verify_range_anchors inside the
replace_lines branch.

The acceptance oracle tests/acceptance_edit_guards_anchor_wiring_s6.py is
read-only (sha256-checked by the merge gate); this file is the separate,
extensible unit suite.
"""
import importlib.util
import os
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent

# Test fixture file. Lines are long enough that a small numeric tweak is a
# rewrite (difflib ratio >= 0.9) rather than a deletion, so the pre-existing
# deletion gate does NOT fire and we isolate the anchor behaviour.
LINE1 = "def f():\n"
LINE2 = "    a = 1  # initialize the counter variable here\n"
LINE3 = "    b = 2  # initialize the second counter here\n"
LINE4 = "    return a\n"
ORIGINAL = LINE1 + LINE2 + LINE3 + LINE4

# Replacements that are clear rewrites of the originals (ratio >= 0.9).
REPL_LINE2 = "    a = 42  # initialize the counter variable here\n"
REPL_LINE2_3 = LINE2 + "    b = 22  # initialize the second counter here\n"
REPL_LINE1 = "def g():\n"
REPL_LINE4 = "    return a + 1\n"

# The bare line text (no trailing newline) as a model would supply it.
L1 = LINE1.rstrip("\n")
L2 = LINE2.rstrip("\n")
L3 = LINE3.rstrip("\n")
L4 = LINE4.rstrip("\n")


def _load(name):
    # The repo-root conftest clears all LOCAL_AGENT_* env vars (autouse
    # _isolate_environ), and the scripts read LOCAL_AGENT_MODEL at import
    # time, so (re)set it here right before loading each script.
    os.environ["LOCAL_AGENT_MODEL"] = "test-model"
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / "scripts" / f"{name}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=["local_agent", "local_agent_oracle"])
def agent(request, tmp_path, monkeypatch):
    mod = _load(request.param)
    monkeypatch.setattr(mod, "CWD", tmp_path)
    return mod


def _script_path(agent):
    name = "local_agent_oracle.py" if agent.__name__.endswith("_oracle") else "local_agent.py"
    return _ROOT / "scripts" / name


# ---------------------------------------------------------------------------
# Backward compatibility: omitting both anchors must be a complete no-op.
# Every pre-existing replace_lines caller passes no anchors.
# ---------------------------------------------------------------------------
def test_omitting_both_anchors_applies_the_edit(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
    })
    assert "The edit was NOT applied." not in result
    assert "a = 42" in target.read_text()


def test_omitting_both_anchors_preserves_full_file(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
    })
    assert target.read_text() == LINE1 + REPL_LINE2 + LINE3 + LINE4


# ---------------------------------------------------------------------------
# Happy path: a correct anchor allows the edit.
# ---------------------------------------------------------------------------
def test_correct_expect_first_allows_edit(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
        "expect_first": L2,
    })
    assert "The edit was NOT applied." not in result
    assert "a = 42" in target.read_text()


def test_correct_expect_last_allows_edit(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 3, "new_str": REPL_LINE2_3,
        "expect_last": L3,
    })
    assert "The edit was NOT applied." not in result
    assert "b = 22" in target.read_text()


def test_both_correct_anchors_allow_edit(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 3, "new_str": REPL_LINE2_3,
        "expect_first": L2,
        "expect_last": L3,
    })
    assert "The edit was NOT applied." not in result
    assert "b = 22" in target.read_text()


def test_anchor_with_trailing_newline_still_matches(agent, tmp_path):
    """verify_range_anchors should normalize trailing whitespace/newlines."""
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
        "expect_first": LINE2,  # includes trailing newline
    })
    assert "The edit was NOT applied." not in result
    assert "a = 42" in target.read_text()


# ---------------------------------------------------------------------------
# Negative: a wrong anchor blocks the edit and leaves the file byte-identical.
# ---------------------------------------------------------------------------
def test_wrong_expect_first_blocks_and_file_untouched(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
        "expect_first": L3,
    })
    assert "The edit was NOT applied." in result
    assert result.rstrip().endswith("The edit was NOT applied.")
    assert target.read_text() == ORIGINAL


def test_wrong_expect_last_blocks_and_file_untouched(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 3, "new_str": REPL_LINE2_3,
        "expect_last": "    nope",
    })
    assert "The edit was NOT applied." in result
    assert result.rstrip().endswith("The edit was NOT applied.")
    assert target.read_text() == ORIGINAL


def test_wrong_anchor_message_mentions_expected_text(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
        "expect_first": "WRONG_TEXT",
    })
    assert "The edit was NOT applied." in result
    assert "WRONG_TEXT" in result


# ---------------------------------------------------------------------------
# Ordering: the anchor check runs BEFORE the deletion gate.
# A call with both a wrong anchor AND a deletion reports the anchor problem,
# not the deletion problem.
# ---------------------------------------------------------------------------
def test_wrong_anchor_reported_before_deletion_gate(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 3, "new_str": "",
        "expect_first": "    totally wrong",
    })
    assert "The edit was NOT applied." in result
    assert "totally wrong" in result
    # The deletion gate message talks about deleting lines / confirm_removals;
    # it must NOT be the reported problem when the anchor is wrong.
    assert "confirm_removals" not in result
    assert target.read_text() == ORIGINAL


def test_wrong_anchor_reported_before_syntax_gate(agent, tmp_path):
    """Anchor check precedes new_text computation / syntax validation."""
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2, "new_str": "    a = (unbalanced",
        "expect_first": "    totally wrong",
    })
    assert "The edit was NOT applied." in result
    assert "totally wrong" in result
    assert target.read_text() == ORIGINAL


# ---------------------------------------------------------------------------
# Boundary values for the anchors themselves.
# ---------------------------------------------------------------------------
def test_empty_string_anchor_is_treated_as_supplied(agent, tmp_path):
    """An empty-string anchor is not None, so it is validated (and a non-empty
    line will mismatch)."""
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
        "expect_first": "",
    })
    assert "The edit was NOT applied." in result
    assert target.read_text() == ORIGINAL


def test_anchor_on_single_line_range_start_equals_end(agent, tmp_path):
    """start == end boundary: expect_first and expect_last refer to the same
    line and both must match it."""
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
        "expect_first": L2,
        "expect_last": L2,
    })
    assert "The edit was NOT applied." not in result
    assert "a = 42" in target.read_text()


def test_anchor_at_first_line_of_file(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 1, "end": 1, "new_str": REPL_LINE1,
        "expect_first": L1,
    })
    assert "The edit was NOT applied." not in result
    assert "def g():" in target.read_text()


def test_anchor_at_last_line_of_file(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 4, "end": 4, "new_str": REPL_LINE4,
        "expect_last": L4,
    })
    assert "The edit was NOT applied." not in result
    assert "return a + 1" in target.read_text()


# ---------------------------------------------------------------------------
# Tool schema: anchors are declared and NOT required.
# ---------------------------------------------------------------------------
def _replace_lines_entry(agent):
    return next(
        t for t in agent.TOOLS if t["function"]["name"] == "replace_lines"
    )


def test_schema_declares_expect_first(agent):
    params = _replace_lines_entry(agent)["function"]["parameters"]
    assert "expect_first" in params["properties"]
    assert params["properties"]["expect_first"]["type"] == "string"


def test_schema_declares_expect_last(agent):
    params = _replace_lines_entry(agent)["function"]["parameters"]
    assert "expect_last" in params["properties"]
    assert params["properties"]["expect_last"]["type"] == "string"


def test_schema_does_not_require_anchors(agent):
    params = _replace_lines_entry(agent)["function"]["parameters"]
    required = params.get("required", [])
    assert "expect_first" not in required
    assert "expect_last" not in required


def test_schema_anchor_descriptions_mention_optional_and_stale_lines(agent):
    """The schema descriptions should note the anchors are optional and
    recommended on files over 1000 lines because line numbers go stale."""
    props = _replace_lines_entry(agent)["function"]["parameters"]["properties"]
    for key in ("expect_first", "expect_last"):
        desc = props[key].get("description", "")
        assert "optional" in desc.lower() or "recommended" in desc.lower(), (
            f"{key} description should mention optional/recommended: {desc!r}"
        )
        assert "1000" in desc, f"{key} description should mention 1000 lines: {desc!r}"


def test_schema_description_mentions_view_file_recommendation(agent):
    """CHANGE 3: the replace_lines description should recommend supplying
    expect_first/expect_last when line numbers came from an earlier view_file."""
    desc = _replace_lines_entry(agent)["function"]["description"]
    assert "expect_first" in desc
    assert "expect_last" in desc
    assert "view_file" in desc


# ---------------------------------------------------------------------------
# Call-site: edit_guards.verify_range_anchors is actually invoked, with a
# comment noting the anchors must stay optional.
# ---------------------------------------------------------------------------
def test_call_site_invokes_verify_range_anchors(agent):
    """The replace_lines branch must call edit_guards.verify_range_anchors."""
    src = _script_path(agent).read_text()
    assert "verify_range_anchors" in src


def test_call_site_has_optional_anchors_comment(agent):
    """A comment at the call site must note the anchors must stay optional."""
    src = _script_path(agent).read_text()
    assert "verify_range_anchors" in src
    idx = src.index("verify_range_anchors")
    window = src[max(0, idx - 400): idx + 200]
    assert "optional" in window.lower(), (
        "call site should have a comment noting anchors must stay optional"
    )


# ---------------------------------------------------------------------------
# Both scripts behave identically: run the same scenario through each and
# compare results.
# ---------------------------------------------------------------------------
def _both_agents(tmp_path, monkeypatch):
    return [_load("local_agent"), _load("local_agent_oracle")]


def test_both_scripts_identical_on_wrong_anchor(tmp_path, monkeypatch):
    a, b = _both_agents(tmp_path, monkeypatch)
    for mod in (a, b):
        monkeypatch.setattr(mod, "CWD", tmp_path)
    ta = tmp_path / "a.py"
    tb = tmp_path / "b.py"
    ta.write_text(ORIGINAL)
    tb.write_text(ORIGINAL)
    ra = a.run_tool("replace_lines", {
        "path": "a.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
        "expect_first": "WRONG",
    })
    rb = b.run_tool("replace_lines", {
        "path": "b.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
        "expect_first": "WRONG",
    })
    assert ("The edit was NOT applied." in ra) == ("The edit was NOT applied." in rb)
    assert ta.read_text() == ORIGINAL
    assert tb.read_text() == ORIGINAL


def test_both_scripts_identical_on_correct_anchor(tmp_path, monkeypatch):
    a, b = _both_agents(tmp_path, monkeypatch)
    for mod in (a, b):
        monkeypatch.setattr(mod, "CWD", tmp_path)
    ta = tmp_path / "a.py"
    tb = tmp_path / "b.py"
    ta.write_text(ORIGINAL)
    tb.write_text(ORIGINAL)
    ra = a.run_tool("replace_lines", {
        "path": "a.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
        "expect_first": L2,
    })
    rb = b.run_tool("replace_lines", {
        "path": "b.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
        "expect_first": L2,
    })
    assert ("The edit was NOT applied." not in ra)
    assert ("The edit was NOT applied." not in rb)
    assert ta.read_text() == tb.read_text()


def test_both_scripts_identical_on_omitted_anchors(tmp_path, monkeypatch):
    a, b = _both_agents(tmp_path, monkeypatch)
    for mod in (a, b):
        monkeypatch.setattr(mod, "CWD", tmp_path)
    ta = tmp_path / "a.py"
    tb = tmp_path / "b.py"
    ta.write_text(ORIGINAL)
    tb.write_text(ORIGINAL)
    ra = a.run_tool("replace_lines", {
        "path": "a.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
    })
    rb = b.run_tool("replace_lines", {
        "path": "b.py", "start": 2, "end": 2, "new_str": REPL_LINE2,
    })
    assert ("The edit was NOT applied." not in ra)
    assert ("The edit was NOT applied." not in rb)
    assert ta.read_text() == tb.read_text()


def test_both_scripts_identical_schema():
    a = _load("local_agent")
    b = _load("local_agent_oracle")
    ea = _replace_lines_entry(a)["function"]["parameters"]
    eb = _replace_lines_entry(b)["function"]["parameters"]
    assert ea["properties"].get("expect_first") == eb["properties"].get("expect_first")
    assert ea["properties"].get("expect_last") == eb["properties"].get("expect_last")
    assert ea.get("required", []) == eb.get("required", [])
    assert _replace_lines_entry(a)["function"]["description"] == \
        _replace_lines_entry(b)["function"]["description"]