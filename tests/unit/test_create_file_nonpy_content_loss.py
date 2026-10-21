"""Regression: create_file must reject a non-.py overwrite that drops content.

Mode 22 defect: ``create_file`` on a pre-existing file only checked (a) the
prior-view requirement, (b) Python syntax, (c) dropped top-level def/class via
``_dropped_top_level_defs`` and (d) dropped top-level vars (``.py`` only).
``_dropped_top_level_defs`` is AST-based and returns ``[]`` whenever
``ast.parse`` fails, which is always true for markdown/text, so a create_file
rewrite of a markdown persona file could silently drop whole sections.

The fix adds a generic line-loss guard for non-``.py`` files, reusing
``pipeline.edit_guards.classify_removed_lines`` so "survives" keeps its single
definition (verbatim multiset match or >= 0.9 similarity to some new line).

Both agent variants are exercised because they are near-verbatim copies and a
fix to only one leaves the oracle path broken.
"""
import importlib.util
import os
from pathlib import Path

import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_ROOT = Path(__file__).resolve().parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, str(_ROOT / "scripts" / f"{name}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=["local_agent", "local_agent_oracle"])
def agent(request, tmp_path, monkeypatch):
    # The env-isolation conftest fixture clears LOCAL_AGENT_MODEL per test, and
    # both scripts read it at import time, so set it before loading.
    monkeypatch.setenv("LOCAL_AGENT_MODEL", "ollama/deepseek-v4.1-flash")
    mod = _load(request.param)
    monkeypatch.setattr(mod, "CWD", tmp_path)
    mod._CREATED_THIS_RUN = set()
    mod._VIEWED_THIS_RUN = set()
    return mod


# 12 total lines, 8 non-blank. Dropping the middle section removes two
# non-blank lines, which is exactly the threshold max(2, round(8 * 0.10)) = 2.
OLD_DOC = (
    "# Persona\n"
    "\n"
    "You are a helpful engineering assistant.\n"
    "\n"
    "## Overview\n"
    "Be concise.\n"
    "\n"
    "## Details\n"
    "Always explain your reasoning.\n"
    "\n"
    "## Examples\n"
    "Say hello.\n"
)

# Same document with the whole "## Details" section removed.
DROPPED_SECTION_DOC = (
    "# Persona\n"
    "\n"
    "You are a helpful engineering assistant.\n"
    "\n"
    "## Overview\n"
    "Be concise.\n"
    "\n"
    "## Examples\n"
    "Say hello.\n"
)


def test_markdown_overwrite_that_drops_a_section_is_rejected(agent, tmp_path):
    # arrange: a pre-existing markdown doc the agent has viewed this run
    target = tmp_path / "persona.md"
    target.write_text(OLD_DOC)
    agent.run_tool("view_file", {"path": "persona.md"})

    # act: a full rewrite that silently drops the middle section
    result = agent.run_tool("create_file", {
        "path": "persona.md", "content": DROPPED_SECTION_DOC,
    })

    # assert: rejected, names the dropped content, offers the escape hatch
    assert result.startswith("ERROR: this create_file overwrite of persona.md would drop")
    assert "confirm_removals" in result
    assert "## Details" in result or "Always explain your reasoning." in result
    assert target.read_text() == OLD_DOC

    # assert: the follow-up call is still gated (no silent second-chance write)
    agent.run_tool("view_file", {"path": "persona.md"})
    again = agent.run_tool("create_file", {
        "path": "persona.md", "content": DROPPED_SECTION_DOC,
    })
    assert again.startswith("ERROR: this create_file overwrite of persona.md would drop")
    assert target.read_text() == OLD_DOC


def test_single_line_typo_fix_is_allowed(agent, tmp_path):
    # arrange
    target = tmp_path / "persona.md"
    target.write_text(OLD_DOC)
    agent.run_tool("view_file", {"path": "persona.md"})

    # act: one changed line, everything else verbatim
    fixed = OLD_DOC.replace("Be concise.", "Be clear.")
    result = agent.run_tool("create_file", {"path": "persona.md", "content": fixed})

    # assert
    assert "created persona.md" in result
    assert target.read_text() == fixed


def test_adding_a_section_is_allowed(agent, tmp_path):
    # arrange
    target = tmp_path / "persona.md"
    target.write_text(OLD_DOC)
    agent.run_tool("view_file", {"path": "persona.md"})

    # act: old content plus an appended section
    grown = OLD_DOC + "\n## Notes\nKeep the diff small.\n"
    result = agent.run_tool("create_file", {"path": "persona.md", "content": grown})

    # assert
    assert "created persona.md" in result
    assert target.read_text() == grown


def test_confirm_removals_allows_the_section_drop(agent, tmp_path):
    # arrange
    target = tmp_path / "persona.md"
    target.write_text(OLD_DOC)
    agent.run_tool("view_file", {"path": "persona.md"})

    # act: same drop, explicitly confirmed
    result = agent.run_tool("create_file", {
        "path": "persona.md",
        "content": DROPPED_SECTION_DOC,
        "confirm_removals": True,
    })

    # assert
    assert "created persona.md" in result
    assert target.read_text() == DROPPED_SECTION_DOC


def test_brand_new_non_python_file_is_not_checked(agent, tmp_path):
    # arrange: no pre-existing file, no prior view
    target = tmp_path / "notes.md"
    assert not target.exists()
    content = "".join(f"- line {i}\n" for i in range(1, 31))

    # act
    result = agent.run_tool("create_file", {"path": "notes.md", "content": content})

    # assert: nothing to lose, so the guard is skipped entirely
    assert "created notes.md" in result
    assert target.read_text() == content


def test_python_file_is_not_subject_to_the_generic_line_check(agent, tmp_path):
    # arrange: two top-level defs, both retained by the rewrite
    target = tmp_path / "persona.py"
    target.write_text(
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
    )
    agent.run_tool("view_file", {"path": "persona.py"})

    # act: every body changes, both top-level names survive, no confirmation
    rewritten = (
        "def alpha():\n"
        "    value = 10\n"
        "    return value\n"
        "\n"
        "\n"
        "def beta():\n"
        "    value = 20\n"
        "    return value\n"
    )
    result = agent.run_tool("create_file", {"path": "persona.py", "content": rewritten})

    # assert: .py keeps only its top-level-symbol guard
    assert "created persona.py" in result
    assert target.read_text() == rewritten
