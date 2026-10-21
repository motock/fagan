"""Regression: every write path must reject a trailing omission marker.

Mode 32 defect: a local model that runs out of context sometimes ends a written
file with a placeholder such as ``# ... (rest of file remains same)`` instead of
the real content. The marker is a valid Python comment, so the syntax check
passes, the truncated file lands silently, and the next edit deletes functions
that are still imported elsewhere.

The guard must cover create_file, str_replace and replace_lines, and it must
only look at the LAST non-blank line so that a mid-file mention of the artifact
(documentation about it) stays writable.

Both agent variants are exercised because they are near-verbatim copies and a
fix to only one leaves the oracle path broken.
"""
import importlib.util
import os
from pathlib import Path

import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_ROOT = Path(__file__).resolve().parents[2]

MARKER_ERROR = "ERROR: mod.py content ends with an omission marker"


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
    monkeypatch.setenv("LOCAL_AGENT_MODEL", "test-model")
    mod = _load(request.param)
    monkeypatch.setattr(mod, "CWD", tmp_path)
    mod._CREATED_THIS_RUN.clear()
    mod._VIEWED_THIS_RUN.clear()
    return mod


def test_create_file_rejects_content_ending_with_an_omission_marker(
        agent, tmp_path):
    # arrange: the exact incident shape - a truncated file whose last line is
    # a placeholder that is valid Python
    content = "def f():\n    return 1\n\n# ... (rest of file remains same)\n"

    # act
    result = agent.run_tool("create_file", {"path": "mod.py", "content": content})

    # assert: rejected with the omission-marker error and nothing written
    assert result.startswith(MARKER_ERROR)
    assert "rest of file remains same" in result
    assert result.endswith("The file was NOT written.")
    assert not (tmp_path / "mod.py").exists()


def test_create_file_writes_normal_content(agent, tmp_path):
    # arrange: ordinary content that merely ends in a comment
    content = "def f():\n    return 1\n\n# done\n"

    # act
    result = agent.run_tool("create_file", {"path": "mod.py", "content": content})

    # assert: negative control - the guard must not block normal writes
    assert "created mod.py" in result
    assert (tmp_path / "mod.py").read_text() == content


def test_create_file_allows_a_mid_file_mention_of_the_marker(agent, tmp_path):
    # arrange: a note that documents the artifact mid-file, ending in prose
    content = (
        "# Notes on truncation\n"
        "\n"
        "# ... (rest of file remains same)\n"
        "\n"
        "This note is complete.\n"
    )

    # act
    result = agent.run_tool(
        "create_file", {"path": "notes.md", "content": content})

    # assert: only the LAST non-blank line is examined, so this is writable
    assert "created notes.md" in result
    assert (tmp_path / "notes.md").read_text() == content


def test_str_replace_rejects_a_result_ending_with_the_marker(agent, tmp_path):
    # arrange: an existing file the model has viewed this run
    target = tmp_path / "mod.py"
    original = "def f():\n    return 1\n"
    target.write_text(original)
    agent.run_tool("view_file", {"path": "mod.py"})

    # act: a unique replacement whose result ends in a truncation marker
    result = agent.run_tool("str_replace", {
        "path": "mod.py",
        "old_str": "    return 1\n",
        "new_str": "    return 2\n\n# ... existing code ...\n",
    })

    # assert: rejected and the file is untouched
    assert result.startswith(MARKER_ERROR)
    assert "existing code" in result
    assert result.endswith("The file was NOT written.")
    assert target.read_text() == original


def test_replace_lines_rejects_a_result_ending_with_the_marker(agent, tmp_path):
    # arrange
    target = tmp_path / "mod.py"
    original = "def f():\n    return 1\n"
    target.write_text(original)
    agent.run_tool("view_file", {"path": "mod.py"})

    # act: replace the whole file with truncated content
    result = agent.run_tool("replace_lines", {
        "path": "mod.py",
        "start": 1,
        "end": 2,
        "new_str": "def f():\n    return 2\n\n# ... (rest of file remains same)\n",
    })

    # assert: rejected and the file is untouched
    assert result.startswith(MARKER_ERROR)
    assert "rest of file remains same" in result
    assert result.endswith("The file was NOT written.")
    assert target.read_text() == original
