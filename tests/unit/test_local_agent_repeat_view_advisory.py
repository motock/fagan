"""A repeated identical view_file call must return an advisory, not the same bytes.

A truncated view re-issued verbatim returns byte-identical output, so a weak
executor can never escape by repeating and the repetition guard kills the run.
When the same (path, range) is viewed again and the file is unchanged, the tool
returns a short advisory instead. Both scripts/local_agent_tools.py and its
verbatim twin scripts/local_agent_oracle_tools.py implement it, so every test
runs against both.
"""

import pytest

from tests.unit._local_agent_oracle_test_helpers import lao
from tests.unit._local_agent_test_helpers import la

ADVISORY_MARKER = "already viewed"


@pytest.fixture(params=[la, lao], ids=["local_agent", "local_agent_oracle"])
def harness(request, tmp_path, monkeypatch):
    monkeypatch.setattr(request.param, "CWD", tmp_path)
    return request.param


def _write_lines(tmp_path, count):
    (tmp_path / "big.py").write_text("".join(f"x{k} = {k}\n" for k in range(1, count + 1)))


def test_second_identical_view_returns_advisory_without_rows(harness, tmp_path):
    _write_lines(tmp_path, 3000)
    harness.run_tool("view_file", {"path": "big.py"})
    out = harness.run_tool("view_file", {"path": "big.py"})
    assert ADVISORY_MARKER in out
    assert "x1 = 1" not in out


def test_second_identical_ranged_view_returns_advisory(harness, tmp_path):
    _write_lines(tmp_path, 50)
    args = {"path": "big.py", "line_start": 2, "line_end": 4}
    harness.run_tool("view_file", args)
    out = harness.run_tool("view_file", args)
    assert ADVISORY_MARKER in out
    assert "x3 = 3" not in out


def test_first_view_returns_rows_and_no_advisory(harness, tmp_path):
    _write_lines(tmp_path, 3000)
    out = harness.run_tool("view_file", {"path": "big.py"})
    assert out.startswith("   1| x1 = 1")
    assert ADVISORY_MARKER not in out


def test_different_range_is_not_a_repeat(harness, tmp_path):
    _write_lines(tmp_path, 50)
    harness.run_tool("view_file", {"path": "big.py", "line_start": 1, "line_end": 5})
    out = harness.run_tool("view_file", {"path": "big.py", "line_start": 6, "line_end": 10})
    assert ADVISORY_MARKER not in out
    assert "x6 = 6" in out


def test_view_after_successful_edit_returns_new_content(harness, tmp_path):
    _write_lines(tmp_path, 10)
    args = {"path": "big.py", "line_start": 1, "line_end": 3}
    harness.run_tool("view_file", args)
    edit = harness.run_tool(
        "str_replace", {"path": "big.py", "old_str": "x2 = 2", "new_str": "x2 = 'changed'"}
    )
    out = harness.run_tool("view_file", args)
    assert "Error" not in edit
    assert ADVISORY_MARKER not in out
    assert "x2 = 'changed'" in out


def test_small_untruncated_file_reread_returns_advisory(harness, tmp_path):
    (tmp_path / "small.py").write_text("x = 1\ny = 2\n")
    harness.run_tool("view_file", {"path": "small.py"})
    out = harness.run_tool("view_file", {"path": "small.py"})
    assert ADVISORY_MARKER in out
    assert "x = 1" not in out


def test_advisory_tells_the_model_to_use_a_new_range_or_edit(harness, tmp_path):
    (tmp_path / "small.py").write_text("x = 1\n")
    harness.run_tool("view_file", {"path": "small.py"})
    out = harness.run_tool("view_file", {"path": "small.py"})
    assert "line_start" in out
    assert "str_replace" in out
