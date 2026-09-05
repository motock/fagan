"""Tests for the dispatch agent's view_file output cap on the RANGED path.

Measured live (2026-09-04, 7 real .agent_transcript.json files): the
whole-file view path caps at 3000 chars, but a line_start/line_end view
returns the selected range UNcapped — one ranged call returned 21,060
chars, 20% of that transcript. Both scripts/local_agent_tools.py and its
verbatim twin scripts/local_agent_oracle_tools.py must cap the ranged
path the same way as the whole-file path.
"""
from tests.unit._local_agent_oracle_test_helpers import lao
from tests.unit._local_agent_test_helpers import la

CAP = 3000


def _write_big_file(tmp_path):
    path = tmp_path / "big.py"
    path.write_text("".join(f"x{i} = {i}\n" for i in range(3000)))
    return path


def test_ranged_view_over_cap_is_truncated_with_narrow_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    _write_big_file(tmp_path)
    out = la.run_tool("view_file", {"path": "big.py", "line_start": 1, "line_end": 3000})
    assert len(out) <= CAP + 400  # cap plus the truncation note
    assert "truncated" in out
    assert "line_start" in out  # tells the model how to narrow the range


def test_ranged_view_under_cap_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    _write_big_file(tmp_path)
    out = la.run_tool("view_file", {"path": "big.py", "line_start": 500, "line_end": 520})
    assert "truncated" not in out
    assert "x500 =" in out
    assert "x519 =" in out
    assert "x520 =" not in out


def test_ranged_view_cap_boundary_just_under_cap_not_truncated(tmp_path, monkeypatch):
    # 40-char lines x 65 = ~2.6K chars formatted: under cap, must pass through.
    monkeypatch.setattr(la, "CWD", tmp_path)
    path = tmp_path / "big.py"
    path.write_text("".join("a" * 39 + "\n" for _ in range(65)))
    out = la.run_tool("view_file", {"path": "big.py", "line_start": 1, "line_end": 65})
    assert "truncated" not in out
    assert out.count("a" * 39) == 65


def test_ranged_view_cap_boundary_just_over_cap_is_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    path = tmp_path / "big.py"
    path.write_text("".join("a" * 39 + "\n" for _ in range(95)))
    out = la.run_tool("view_file", {"path": "big.py", "line_start": 1, "line_end": 95})
    assert "truncated" in out


def test_oracle_ranged_view_over_cap_is_truncated_too(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    _write_big_file(tmp_path)
    out = lao.run_tool("view_file", {"path": "big.py", "line_start": 1, "line_end": 3000})
    assert len(out) <= CAP + 400
    assert "truncated" in out