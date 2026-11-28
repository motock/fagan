"""Characterisation tests pinning every behavioural difference between the
oracle tool runner (scripts/local_agent_oracle_tools.py) and the plain one
(scripts/local_agent_tools.py), plus the tools they must keep doing identically.
"""
from types import SimpleNamespace

import pytest

from pipeline import edit_guards
from scripts import local_agent_oracle_tools as oracle
from scripts import local_agent_tools as plain

MODULES = [pytest.param(plain, id="plain"), pytest.param(oracle, id="oracle")]


def _origin(tmp_path, **extra):
    origin = {
        "CWD": tmp_path,
        "edit_guards": edit_guards,
        "is_oracle_path": lambda p: p == "tests/acceptance.py",
        "_CREATED_THIS_RUN": set(),
        "_VIEWED_THIS_RUN": set(),
        "_SYNTAX_REJECT_COUNTS": {},
        "_python_syntax_error": lambda path, text: None,
        "_try_repair_indentation": lambda text: None,
        "_record_syntax_rejection": lambda *a: "REJECTED",
        "_dropped_top_level_defs": lambda old, new: [],
        "_dropped_top_level_vars": lambda old, new: [],
        "_lint_feedback_for": lambda path, cwd: "",
        "_str_replace_not_found_diag": lambda *a: "NOT FOUND",
        "_newly_undefined_names": lambda *a: [],
        "_var_drop_is_confirmed_loss": lambda *a: True,
        "destructive_git_op": lambda cmd: "git reset --hard" if "reset --hard" in cmd else None,
        "p": SimpleNamespace(_is_heavy=lambda argv: False),
        "BASH_TIMEOUT": 30,
        "ACCEPTANCE_PATHS": [],
        "_restore_tampered_oracle_files": lambda: "\n[RESTORED]",
        "run_tool": lambda fn, args: (_ for _ in ()).throw(KeyError("old_str")),
        "_TOOL_SCHEMAS": {"str_replace": {"required": ["path", "old_str", "new_str"]}},
    }
    origin.update(extra)
    return origin


ORACLE_MSG = ("ERROR: tests/acceptance.py is the read-only acceptance suite and "
              "must NOT be modified. Change the implementation file instead.")


def test_oracle_refuses_create_file_on_acceptance_path(tmp_path):
    result = oracle.run_tool_impl(
        _origin(tmp_path), "create_file", {"path": "tests/acceptance.py", "content": "x\n"})
    assert result == ORACLE_MSG
    assert not (tmp_path / "tests" / "acceptance.py").exists()


def test_oracle_refuses_str_replace_on_acceptance_path(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "acceptance.py").write_text("a\n")
    result = oracle.run_tool_impl(
        _origin(tmp_path), "str_replace",
        {"path": "tests/acceptance.py", "old_str": "a", "new_str": "b"})
    assert result == ORACLE_MSG
    assert (tmp_path / "tests" / "acceptance.py").read_text() == "a\n"


def test_plain_has_no_acceptance_path_guard(tmp_path):
    result = plain.run_tool_impl(
        _origin(tmp_path), "create_file", {"path": "tests/acceptance.py", "content": "x\n"})
    assert result == "created tests/acceptance.py"


def test_oracle_replace_lines_is_not_guarded_by_is_oracle_path(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "acceptance.py").write_text("a\n")
    result = oracle.run_tool_impl(
        _origin(tmp_path), "replace_lines",
        {"path": "tests/acceptance.py", "start": 1, "end": 1, "new_str": "b",
         "confirm_removals": True})
    assert result.startswith("edited tests/acceptance.py (lines 1-1)")


def test_oracle_guard_consults_is_oracle_path_only_for_create_and_str_replace(tmp_path):
    seen = []
    origin = _origin(tmp_path, is_oracle_path=lambda p: seen.append(p) or False)
    (tmp_path / "f.txt").write_text("a\n")
    oracle.run_tool_impl(origin, "view_file", {"path": "f.txt"})
    oracle.run_tool_impl(origin, "bash", {"command": "true"})
    oracle.run_tool_impl(origin, "replace_lines", {"path": "f.txt", "start": 1, "end": 1})
    assert seen == []
    oracle.run_tool_impl(origin, "create_file", {"path": "g.txt", "content": "g\n"})
    assert seen == ["g.txt"]


def test_oracle_bash_appends_tamper_restore_when_acceptance_paths_set(tmp_path):
    origin = _origin(tmp_path, ACCEPTANCE_PATHS=["tests/acceptance.py"])
    assert oracle.run_tool_impl(origin, "bash", {"command": "echo hi"}) == "hi\n\n[RESTORED]"


def test_oracle_bash_skips_tamper_restore_without_acceptance_paths(tmp_path):
    assert oracle.run_tool_impl(_origin(tmp_path), "bash", {"command": "echo hi"}) == "hi\n"


def test_oracle_bash_blocked_command_does_not_restore(tmp_path):
    origin = _origin(tmp_path, ACCEPTANCE_PATHS=["tests/acceptance.py"])
    result = oracle.run_tool_impl(origin, "bash", {"command": "git reset --hard"})
    assert result.startswith("ERROR: 'git reset --hard' is blocked")
    assert "[RESTORED]" not in result


def test_oracle_bash_no_output_still_gets_restore_suffix(tmp_path):
    origin = _origin(tmp_path, ACCEPTANCE_PATHS=["x"])
    assert oracle.run_tool_impl(origin, "bash", {"command": "true"}) == "(no output)\n[RESTORED]"


def test_plain_bash_never_restores(tmp_path):
    origin = _origin(tmp_path, ACCEPTANCE_PATHS=["x"])
    assert plain.run_tool_impl(origin, "bash", {"command": "echo hi"}) == "hi\n"


def test_oracle_has_no_checkpoint_tool(tmp_path):
    assert oracle.run_tool_impl(_origin(tmp_path), "checkpoint", {"step": "s"}) == \
        "unknown tool checkpoint"


def test_plain_checkpoint_is_handled(tmp_path):
    origin = _origin(tmp_path, p=SimpleNamespace(
        _checkpoint_impl=lambda *a: {"step": a[2]}, _is_heavy=lambda a: False))
    args = {"plan_name": "p", "story_key": "k", "step": "s"}
    result = plain.run_tool_impl(origin, "checkpoint", args)
    assert result != "unknown tool checkpoint"


@pytest.mark.parametrize("module", MODULES)
def test_unknown_tool_message_is_shared(module, tmp_path):
    assert module.run_tool_impl(_origin(tmp_path), "nope", {}) == "unknown tool nope"


@pytest.mark.parametrize("module", MODULES)
def test_search_message_is_shared(module, tmp_path):
    result = module.run_tool_impl(_origin(tmp_path), "search", {})
    assert result.startswith("unknown tool search — there is no search tool.")


def test_safe_run_tool_oracle_omits_required_args_hint(tmp_path):
    result = oracle.safe_run_tool_impl(_origin(tmp_path), "str_replace", {"path": "a"})
    assert result == "ERROR running str_replace: KeyError: 'old_str'"


def test_safe_run_tool_plain_appends_required_args_hint(tmp_path):
    result = plain.safe_run_tool_impl(_origin(tmp_path), "str_replace", {"path": "a"})
    assert result.startswith("ERROR running str_replace: KeyError: 'old_str' -- ")
    assert "missing ['old_str', 'new_str']" in result


@pytest.mark.parametrize("module", MODULES)
def test_create_then_str_replace_then_view_round_trip(module, tmp_path):
    origin = _origin(tmp_path)
    assert module.run_tool_impl(
        origin, "create_file", {"path": "a.txt", "content": "one\ntwo\n"}) == "created a.txt"
    assert module.run_tool_impl(
        origin, "str_replace", {"path": "a.txt", "old_str": "one", "new_str": "1"}) == "edited a.txt"
    assert module.run_tool_impl(origin, "view_file", {"path": "a.txt"}) == "   1| 1\n   2| two\n"


@pytest.mark.parametrize("module", MODULES)
def test_view_file_missing_path_errors(module, tmp_path):
    assert module.run_tool_impl(_origin(tmp_path), "view_file", {"path": "zz"}) == \
        "ERROR: zz does not exist."


@pytest.mark.parametrize("module", MODULES)
def test_replace_lines_rejects_non_integer_range(module, tmp_path):
    (tmp_path / "a.txt").write_text("x\n")
    result = module.run_tool_impl(
        _origin(tmp_path), "replace_lines", {"path": "a.txt", "start": "1", "end": 1})
    assert result == "ERROR: replace_lines requires integer start and end (got start='1', end=1)."
