"""Acceptance: duplicated_block_warning is actually CALLED by replace_lines
in both agent scripts.

Drives the real run_tool entrypoint - a unit-level assertion on
edit_guards would pass even with the wiring absent, which is the exact
failure this fixture exists to prevent.
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


DOC = (
    "## A3 item\n"
    "- [ ] Notify operator\n"
    "\n"
    "Shipped: PRs #214-217.\n"
    "See the retro for detail.\n"
)


def test_markdown_duplicate_paragraph_is_warned_about(agent, tmp_path):
    target = tmp_path / "doc.md"
    target.write_text(DOC)

    result = agent.run_tool("replace_lines", {
        "path": "doc.md", "start": 2, "end": 2,
        "new_str": (
            "- [x] Notify operator\n"
            "Shipped: PRs #214-217.\n"
            "See the retro for detail.\n"
        ),
        "confirm_removals": True,
    })

    assert "Shipped" in result
    assert "duplicat" in result.lower()


def test_duplicate_warning_never_blocks_the_write(agent, tmp_path):
    target = tmp_path / "doc.md"
    target.write_text(DOC)
    agent.run_tool("replace_lines", {
        "path": "doc.md", "start": 2, "end": 2,
        "new_str": (
            "- [x] Notify operator\n"
            "Shipped: PRs #214-217.\n"
            "See the retro for detail.\n"
        ),
        "confirm_removals": True,
    })
    assert "- [x] Notify operator" in target.read_text()


def test_no_warning_when_nothing_is_duplicated(agent, tmp_path):
    target = tmp_path / "doc.md"
    target.write_text(DOC)
    result = agent.run_tool("replace_lines", {
        "path": "doc.md", "start": 2, "end": 2,
        "new_str": "- [x] Notify operator\n",
    })
    assert "duplicat" not in result.lower()


def test_python_files_get_the_same_treatment(agent, tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
    result = agent.run_tool("replace_lines", {
        "path": "mod.py", "start": 1, "end": 1,
        "new_str": "a = 1\nc = 3\nd = 4\n",
    })
    assert "duplicat" in result.lower()


def test_replaced_range_itself_is_not_counted_as_the_duplicate_source(agent, tmp_path):
    """Re-emitting what the range already held is an ordinary edit, not a
    duplication - only content OUTSIDE the range counts."""
    target = tmp_path / "doc.md"
    target.write_text("head\nalpha\nbeta\ntail\n")
    result = agent.run_tool("replace_lines", {
        "path": "doc.md", "start": 2, "end": 3,
        "new_str": "alpha\nbeta\n",
    })
    assert "duplicat" not in result.lower()
