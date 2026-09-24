"""view_file's truncation marker must not make an intact file look corrupt.

A capped view used to cut at a raw character offset, which could land in the
middle of a line (live: mid-import), and its marker claimed to show the whole
REQUESTED range. A weak executor read the ragged tail as file corruption and
"repaired" it. The cut must fall on a line boundary, and the marker must state
the last line actually shown, that the file is intact, and where to resume.

Both scripts/local_agent_tools.py and its verbatim twin
scripts/local_agent_oracle_tools.py implement the cap, so every test runs
against both.
"""

import re

import pytest

from tests.unit._local_agent_oracle_test_helpers import lao
from tests.unit._local_agent_test_helpers import la

CAP = 3000
_ROW = re.compile(r"^\s*(\d+)\| (.*)$")
_CLAIM = re.compile(r"truncated after line (\d+)")


@pytest.fixture(params=[la, lao], ids=["local_agent", "local_agent_oracle"])
def harness(request, tmp_path, monkeypatch):
    monkeypatch.setattr(request.param, "CWD", tmp_path)
    return request.param


def _write_lines(tmp_path, count, width=0):
    lines = [f"x{k} = {k}".ljust(width) + "\n" for k in range(1, count + 1)]
    (tmp_path / "big.py").write_text("".join(lines))


def _split(out):
    body, _, marker = out.partition("... [truncated")
    return body, "... [truncated" + marker


def _rows(body):
    matches = [_ROW.match(line) for line in body.splitlines()]
    assert all(matches)
    return [(int(m.group(1)), m.group(2)) for m in matches]


def _claimed_last_line(marker):
    return int(_CLAIM.search(marker).group(1))


def test_ranged_cut_ends_on_a_whole_line(harness, tmp_path):
    _write_lines(tmp_path, 3000)
    out = harness.run_tool("view_file", {"path": "big.py", "line_start": 1, "line_end": 3000})
    body, _ = _split(out)
    number, content = _rows(body)[-1]
    assert body.endswith("\n")
    assert content == f"x{number} = {number}"


def test_ranged_marker_names_the_last_line_actually_shown(harness, tmp_path):
    _write_lines(tmp_path, 3000)
    out = harness.run_tool("view_file", {"path": "big.py", "line_start": 1, "line_end": 3000})
    body, marker = _split(out)
    assert _claimed_last_line(marker) == _rows(body)[-1][0]


def test_ranged_marker_says_the_file_is_intact_and_where_to_resume(harness, tmp_path):
    _write_lines(tmp_path, 3000)
    out = harness.run_tool("view_file", {"path": "big.py", "line_start": 1, "line_end": 3000})
    body, marker = _split(out)
    last = _rows(body)[-1][0]
    assert "the file itself is intact" in marker
    assert f"line_start={last + 1}" in marker


def test_ranged_marker_does_not_claim_the_whole_requested_range_was_shown(harness, tmp_path):
    _write_lines(tmp_path, 3000)
    out = harness.run_tool("view_file", {"path": "big.py", "line_start": 1, "line_end": 3000})
    _, marker = _split(out)
    assert "showing" not in marker
    assert " lines 1-3000" not in marker


def test_resuming_at_the_reported_line_skips_nothing(harness, tmp_path):
    _write_lines(tmp_path, 3000)
    first = harness.run_tool("view_file", {"path": "big.py", "line_start": 1, "line_end": 3000})
    last = _rows(_split(first)[0])[-1][0]
    second = harness.run_tool(
        "view_file", {"path": "big.py", "line_start": last + 1, "line_end": last + 3}
    )
    assert _rows(second)[0][0] == last + 1


def test_ranged_view_stays_within_the_cap_plus_the_marker(harness, tmp_path):
    _write_lines(tmp_path, 3000)
    out = harness.run_tool("view_file", {"path": "big.py", "line_start": 1, "line_end": 3000})
    assert len(out) <= CAP + 400


def test_whole_file_cut_ends_on_a_whole_line_and_names_it(harness, tmp_path):
    _write_lines(tmp_path, 3000)
    out = harness.run_tool("view_file", {"path": "big.py"})
    body, marker = _split(out)
    number, content = _rows(body)[-1]
    assert out.startswith("   1| x1 = 1")
    assert content == f"x{number} = {number}"
    assert _claimed_last_line(marker) == number
    assert "of 3000" in marker
    assert "the file itself is intact" in marker
    assert f"line_start={number + 1}" in marker


def test_view_exactly_at_the_cap_is_not_truncated(harness, tmp_path):
    # 50 lines x 60 formatted chars = exactly 3000.
    _write_lines(tmp_path, 50, width=53)
    out = harness.run_tool("view_file", {"path": "big.py"})
    assert len(out) == CAP
    assert "truncated" not in out


def test_view_one_line_past_the_cap_keeps_every_whole_line_that_fits(harness, tmp_path):
    _write_lines(tmp_path, 51, width=53)
    out = harness.run_tool("view_file", {"path": "big.py"})
    body, marker = _split(out)
    assert len(_rows(body)) == 50
    assert _claimed_last_line(marker) == 50
    assert "of 51" in marker


def test_small_file_view_is_unchanged(harness, tmp_path):
    (tmp_path / "small.py").write_text("x = 1\ny = 2\n")
    out = harness.run_tool("view_file", {"path": "small.py"})
    assert out == "   1| x = 1\n   2| y = 2\n"


def test_a_first_line_longer_than_the_cap_is_hard_cut_and_says_so(harness, tmp_path):
    (tmp_path / "big.py").write_text("a" * 5000 + "\nsecond\n")
    out = harness.run_tool("view_file", {"path": "big.py"})
    assert out.startswith("   1| " + "a" * 100)
    assert "truncated inside line 1" in out
    assert "the file itself is intact" in out
    assert len(out) <= CAP + 400


def test_a_ranged_line_longer_than_the_cap_names_that_line(harness, tmp_path):
    (tmp_path / "big.py").write_text("short\n" + "b" * 5000 + "\nlast\n")
    out = harness.run_tool("view_file", {"path": "big.py", "line_start": 2, "line_end": 3})
    assert "truncated inside line 2" in out
    assert "the file itself is intact" in out
