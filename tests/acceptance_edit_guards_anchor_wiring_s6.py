"""Acceptance: optional expect_first/expect_last anchors are wired into the
real replace_lines tool in BOTH agent scripts.

Drives run_tool rather than edit_guards directly, so the fixture fails if
the validator lands but is never called.
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


ORIGINAL = "def f():\n    a = 1\n    b = 2\n    return a\n"


def test_omitting_anchors_leaves_behaviour_unchanged(agent, tmp_path):
    """Backward compatibility: every pre-existing caller passes no anchors."""
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2,
        # Insertion-style edit preserves "a = 1" verbatim so the s5
        # confirm_removals deletion gate isn't tripped; this isolates the
        # backward-compat behaviour (omitting anchors must not block the edit).
        "new_str": "    a = 1\n    a = 42\n",
    })
    assert "The edit was NOT applied." not in result
    assert "a = 42" in target.read_text()


def test_correct_anchor_allows_the_edit(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2,
        # Same non-deleting new_str as above: the anchor check is what's under
        # test, not the deletion gate.
        "new_str": "    a = 1\n    a = 42\n",
        "expect_first": "    a = 1",
    })
    assert "The edit was NOT applied." not in result
    assert "a = 42" in target.read_text()


def test_wrong_anchor_blocks_and_file_is_untouched(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 2, "new_str": "    a = 42\n",
        "expect_first": "    b = 2",
    })
    assert "The edit was NOT applied." in result
    assert target.read_text() == ORIGINAL


def test_wrong_anchor_is_reported_before_the_deletion_gate(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 3, "new_str": "",
        "expect_first": "    totally wrong",
    })
    assert "The edit was NOT applied." in result
    assert "totally wrong" in result
    assert target.read_text() == ORIGINAL


def test_expect_last_is_validated_too(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 2, "end": 3,
        "new_str": "    a = 1\n    b = 2\n",
        "expect_last": "    nope",
    })
    assert "The edit was NOT applied." in result


def test_anchors_are_declared_optional_in_the_tool_schema(agent):
    entry = next(
        t for t in agent.TOOLS if t["function"]["name"] == "replace_lines"
    )
    params = entry["function"]["parameters"]
    assert "expect_first" in params["properties"]
    assert "expect_last" in params["properties"]
    required = params.get("required", [])
    assert "expect_first" not in required
    assert "expect_last" not in required
