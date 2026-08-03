"""Acceptance: the deletion gate is wired into the REAL replace_lines tool
in BOTH agent scripts.

This fixture deliberately drives run_tool() rather than calling
edit_guards directly: the story's whole risk is that the classifier lands
but never gets called, and a unit-level assertion would pass with the
wiring missing. Both scripts are exercised because they are verbatim
copies and a fix to only one leaves the oracle path broken.
"""
import importlib.util
import os
from pathlib import Path

import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_ROOT = Path(__file__).resolve().parent.parent


def _load(name):
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


ORIGINAL = (
    "def f():\n"
    "    do_thing()\n"
    "    time.sleep(10)  # keep polling\n"
    "    return\n"
)


def test_true_deletion_is_rejected_and_file_untouched(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)

    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 3,
        "new_str": "    do_thing()\n",
    })

    assert "time.sleep(10)" in result
    assert "The edit was NOT applied." in result
    assert target.read_text() == ORIGINAL


def test_confirm_removals_lets_the_same_edit_through(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)

    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 3,
        "new_str": "    do_thing()\n", "confirm_removals": True,
    })

    assert "The edit was NOT applied." not in result
    assert "time.sleep(10)" not in target.read_text()


def test_confirm_removals_false_behaves_like_omitted(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 3,
        "new_str": "    do_thing()\n", "confirm_removals": False,
    })
    assert "The edit was NOT applied." in result
    assert target.read_text() == ORIGINAL


def test_rewrite_only_edit_is_not_blocked(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text('x = cfg.get("k", "")\n')
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 1, "end": 1,
        "new_str": 'x = cfg.get("k", "+")\n',
    })
    assert "The edit was NOT applied." not in result
    assert '"+"' in target.read_text()


def test_pure_insertion_is_not_blocked(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("def f():\n    return 1\n")
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2,
        "new_str": "    log.debug('entering')\n    return 1\n",
    })
    assert "The edit was NOT applied." not in result
    assert "log.debug" in target.read_text()


def test_str_replace_is_not_gated(agent, tmp_path):
    """str_replace's old_str already states the expectation and is verified
    before writing; gating it too would close the escape hatch that
    _str_replace_not_found_diag deliberately steers the model toward."""
    target = tmp_path / "mod.py"
    target.write_text("x = 1\n")
    result = agent.run_tool(
        "str_replace", {"path": "mod.py", "old_str": "x = 1\n", "new_str": ""}
    )
    assert "The edit was NOT applied." not in result


def test_confirm_removals_is_declared_in_the_tool_schema(agent):
    """The flag is useless if the model is never told it exists - assert the
    advertised schema, not just the handler."""
    entry = next(
        t for t in agent.TOOLS if t["function"]["name"] == "replace_lines"
    )
    props = entry["function"]["parameters"]["properties"]
    assert "confirm_removals" in props
    assert "confirm_removals" not in entry["function"]["parameters"].get("required", [])
