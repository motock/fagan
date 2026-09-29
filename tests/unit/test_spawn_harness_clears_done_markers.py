"""Spec tests for clearing a previous attempt's done markers before dispatch.

Contract, exercised through the public ``spawn_harness`` seam:

- A dispatch spawn removes ``<cwd>/.agent_done`` and
  ``<cwd>/.agent_done.consumed`` from the reused worktree BEFORE the local
  spawn happens, so a marker always describes the CURRENT attempt.
- The removal happens before ``spawn_local`` is called (ordering is graded
  from inside the recorder).
- Non-dispatch roles leave both markers untouched.
- Unrelated worktree files (``agent.log``, ``.agent_done.tmp``) survive.

``pipeline.execution.spawn_local`` is patched to a recorder so no process
starts, and ``resolve_sandbox`` is pinned to ``"none"`` so docker is never
required.
"""

from types import SimpleNamespace

import pytest

from pipeline import execution
from pipeline.execution import spawn_harness

CONSUMED = ".agent_done.consumed"
UNCONSUMED = ".agent_done"
TMP = ".agent_done.tmp"


@pytest.fixture
def spawn_recorder(monkeypatch):
    """Record spawn_local calls; return a fake AgentHandle."""
    calls = []

    def fake_spawn_local(cmd, *, cwd, log_path, append, env=None, line_filter=None,
                         _fd_redirect=False):
        calls.append(
            {
                "cmd": list(cmd),
                "cwd": cwd,
                "log_path": log_path,
                "append": append,
                "env": env,
                "line_filter": line_filter,
                "_fd_redirect": _fd_redirect,
            }
        )
        return SimpleNamespace(pid=4242, model="")

    monkeypatch.setattr(execution, "spawn_local", fake_spawn_local)
    monkeypatch.setattr(execution, "resolve_sandbox", lambda: "none")
    return calls


def _spawn(cwd, *, role="dispatch"):
    return spawn_harness(
        ["agent", "run"],
        cwd=cwd,
        log_path=cwd / "agent.log",
        append=True,
        role=role,
    )


def _recorder_asserting_marker_gone(monkeypatch, calls, marker):
    """Patch spawn_local so it records whether ``marker`` is gone at spawn time."""
    observed = {}

    def recorder(cmd, *, cwd, log_path, append, env=None, line_filter=None,
                 _fd_redirect=False):
        observed["gone_at_spawn"] = not marker.exists()
        calls.append({"cwd": cwd})
        return SimpleNamespace(pid=4242, model="")

    monkeypatch.setattr(execution, "spawn_local", recorder)
    return observed


def test_should_remove_consumed_marker_before_dispatch_spawn(
    tmp_path, spawn_recorder, monkeypatch
):
    marker = tmp_path / CONSUMED
    marker.write_text("done")

    observed = _recorder_asserting_marker_gone(monkeypatch, spawn_recorder, marker)

    _spawn(tmp_path)

    assert observed["gone_at_spawn"] is True
    assert not marker.exists()


def test_should_remove_unconsumed_marker_before_dispatch_spawn(
    tmp_path, spawn_recorder, monkeypatch
):
    marker = tmp_path / UNCONSUMED
    marker.write_text("done")

    observed = _recorder_asserting_marker_gone(monkeypatch, spawn_recorder, marker)

    _spawn(tmp_path)

    assert observed["gone_at_spawn"] is True
    assert not marker.exists()


def test_should_spawn_normally_when_no_markers_exist(tmp_path, spawn_recorder):
    _spawn(tmp_path)

    assert len(spawn_recorder) == 1
    assert not (tmp_path / CONSUMED).exists()
    assert not (tmp_path / UNCONSUMED).exists()


def test_should_leave_markers_for_non_dispatch_roles(tmp_path, spawn_recorder):
    consumed = tmp_path / CONSUMED
    unconsumed = tmp_path / UNCONSUMED
    consumed.write_text("done")
    unconsumed.write_text("done")

    _spawn(tmp_path, role="review")

    assert consumed.exists()
    assert unconsumed.exists()
    assert len(spawn_recorder) == 1


def test_should_not_touch_other_worktree_files(tmp_path, spawn_recorder):
    log = tmp_path / "agent.log"
    tmp_marker = tmp_path / TMP
    log.write_text("previous output")
    tmp_marker.write_text("in flight")
    (tmp_path / CONSUMED).write_text("done")

    _spawn(tmp_path)

    assert log.exists()
    assert log.read_text() == "previous output"
    assert tmp_marker.exists()
    assert tmp_marker.read_text() == "in flight"
    assert not (tmp_path / CONSUMED).exists()


def test_should_propagate_non_filenotfound_oserror(tmp_path, spawn_recorder):
    """A worktree we cannot write to is a real fault, not a silent skip.

    A directory in the marker's place makes the removal raise a non-
    FileNotFoundError OSError, which must propagate out of spawn_harness
    instead of being swallowed.
    """
    (tmp_path / UNCONSUMED).mkdir()

    with pytest.raises(OSError) as excinfo:
        _spawn(tmp_path)

    assert not isinstance(excinfo.value, FileNotFoundError)
    assert spawn_recorder == []


def test_should_not_clear_markers_on_ssh_dispatch(tmp_path, spawn_recorder, monkeypatch):
    """The ssh early-return happens before the clear, so remote is untouched."""
    monkeypatch.setenv("PIPELINE_EXEC_DISPATCH", "ssh")
    consumed = tmp_path / CONSUMED
    unconsumed = tmp_path / UNCONSUMED
    consumed.write_text("done")
    unconsumed.write_text("done")

    ssh_calls = []

    def fake_ssh(cmd, *, cwd, log_path, append, env=None):
        ssh_calls.append(cwd)
        return SimpleNamespace(pid=4242, model="")

    monkeypatch.setattr(execution, "_spawn_ssh", fake_ssh)

    _spawn(tmp_path)

    assert len(ssh_calls) == 1
    assert consumed.exists()
    assert unconsumed.exists()
