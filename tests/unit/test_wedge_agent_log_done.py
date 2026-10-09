"""A trailing DONE line in agent.log counts as agent_done in the wedge scan."""

from __future__ import annotations

from pipeline import wedge_io
from pipeline.wedge import wedge_verdict


def _signals(tmp_path, monkeypatch, log_text=None, marker=None, with_worktree=True):
    monkeypatch.setattr(wedge_io, "_pid_is_alive", lambda pid: False)
    story = {"pid": 424242}
    if with_worktree:
        story["worktree"] = str(tmp_path)
    if log_text is not None:
        (tmp_path / "agent.log").write_text(log_text)
    if marker:
        (tmp_path / marker).write_text("")
    return wedge_io.collect_story_wedge_signals("plan", "S-1", story)


def test_should_treat_trailing_done_line_as_agent_done(tmp_path, monkeypatch):
    signals = _signals(tmp_path, monkeypatch, "[step 1] x\n[step 4] DONE: all good\n")

    assert signals["agent_done"] is True
    assert wedge_verdict(False, 10.0, 1800, agent_done=True)["wedged"] is False


def test_should_report_dead_pid_when_log_has_no_done_line(tmp_path, monkeypatch):
    signals = _signals(tmp_path, monkeypatch, "[step 1] working\n[step 2] more\n")

    assert signals["agent_done"] is False
    verdict = wedge_verdict(
        signals["pid_alive"],
        10.0,
        1800,
        agent_done=signals["agent_done"],
    )
    assert "dead_pid" in verdict["reasons"]


def test_should_ignore_old_done_line_followed_by_resumed_steps(tmp_path, monkeypatch):
    log = "[step 4] DONE: first run\n[step 5] resumed rework\n"

    assert _signals(tmp_path, monkeypatch, log)["agent_done"] is False


def test_should_ignore_trailing_blank_lines_after_done(tmp_path, monkeypatch):
    log = "[step 4] DONE: all good\n\n  \n\n"

    assert _signals(tmp_path, monkeypatch, log)["agent_done"] is True


def test_should_not_raise_when_agent_log_missing(tmp_path, monkeypatch):
    assert _signals(tmp_path, monkeypatch)["agent_done"] is False


def test_should_not_raise_when_story_has_no_worktree(tmp_path, monkeypatch):
    signals = _signals(tmp_path, monkeypatch, with_worktree=False)

    assert signals["agent_done"] is False


def test_should_detect_done_line_from_tail_of_large_log(tmp_path, monkeypatch):
    log = ("[step 1] filler line\n" * 2000) + "[step 9] DONE: big\n"
    assert len(log) > 8192

    assert _signals(tmp_path, monkeypatch, log)["agent_done"] is True


def test_should_still_honor_agent_done_marker_without_log(tmp_path, monkeypatch):
    assert _signals(tmp_path, monkeypatch, marker=".agent_done")["agent_done"] is True
