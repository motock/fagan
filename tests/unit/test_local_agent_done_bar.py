"""Tests for the FULL_SUITE_DONE_BAR flag (scripts/local_agent.py).

Arms the existing out-of-band full-suite done-bar (`_full_suite_result`,
originally gated on REWORK_FULL_SUITE for CI-fail-rework rounds only) on
EVERY `done` call when LOCAL_AGENT_FULL_SUITE_DONE_BAR=1 is set — including
fresh (non-rework) dispatch. Default OFF (secure defaults / opt-in).

Imported the same way as test_local_agent.py: as a standalone module, since
it only requires LOCAL_AGENT_MODEL in the environment at import time.
"""
import importlib.util
import os
import subprocess
from pathlib import Path

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")

_spec = importlib.util.spec_from_file_location(
    "local_agent", str(Path(__file__).parent.parent.parent / "scripts" / "local_agent.py")
)
la = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(la)


def _init_git_repo(path):
    subprocess.run(["git", "init"], check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], check=False, cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], check=False, cwd=path, capture_output=True, text=True)


def _sequence_chat(responses):
    """Same helper as test_local_agent.py's _sequence_chat: returns a fake
    chat() that yields scripted tool calls in order, falling back to `done`
    once the script runs out."""
    calls = []

    def _fake(messages):
        calls.append(messages)
        idx = len(calls) - 1
        if idx >= len(responses):
            fn, args = "done", {"summary": "out of scripted responses"}
        else:
            fn, args = responses[idx]
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": fn, "arguments": args}}]}

    return _fake, calls


def test_done_rejected_on_fresh_dispatch_when_flag_set_and_suite_fails(
    tmp_path, monkeypatch, capsys):
    """Fresh dispatch (REWORK_FULL_SUITE unset), FULL_SUITE_DONE_BAR=1, clean
    worktree, failing suite: `done` must be rejected and the failing excerpt
    fed back — the done-bar fires on fresh dispatch, not just rework."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", False)
    monkeypatch.setattr(la, "FULL_SUITE_DONE_BAR", True)
    monkeypatch.setattr(la, "MAX_STEPS", 3)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)
    excerpt = "FAILED test_widget.py::test_frobnicate - assert 1 == 2"
    monkeypatch.setattr(la, "_full_suite_result", lambda: (False, excerpt))

    fake, calls = _sequence_chat([("done", {"summary": "first attempt"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc != 0, f"done must not be accepted while the suite fails; rc={rc}\n{out!r}"
    assert "done rejected — full test suite still fails" in out, out
    assert len(calls) >= 2
    last_user = [m for m in calls[1] if m["role"] == "user"][-1]
    assert excerpt in last_user["content"], last_user["content"]


def test_done_accepted_on_fresh_dispatch_when_flag_set_and_suite_green(
    tmp_path, monkeypatch):
    """Fresh dispatch, FULL_SUITE_DONE_BAR=1, clean worktree, green suite:
    `done` proceeds (rc=0)."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", False)
    monkeypatch.setattr(la, "FULL_SUITE_DONE_BAR", True)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)
    monkeypatch.setattr(la, "_full_suite_result", lambda: (True, ""))

    fake, _ = _sequence_chat([("done", {"summary": "all green"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    assert rc == 0


def test_done_accepted_on_fresh_dispatch_when_flag_unset_without_consulting_suite(
    tmp_path, monkeypatch):
    """Boundary: fresh dispatch with FULL_SUITE_DONE_BAR unset (default OFF)
    behaves exactly as today — done accepted, suite never run."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", False)
    monkeypatch.setattr(la, "FULL_SUITE_DONE_BAR", False)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)

    suite_calls: list = []

    def _suite_spy():
        suite_calls.append(True)
        return (False, "would-fail-but-uncalled")

    monkeypatch.setattr(la, "_full_suite_result", _suite_spy)

    fake, _ = _sequence_chat([("done", {"summary": "done"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    assert rc == 0
    assert suite_calls == []


def test_dirty_tree_auto_accept_checks_suite_on_fresh_dispatch_when_flag_set(
    tmp_path, monkeypatch, capsys):
    """The dirty-tree auto-accept-at-2 escape must also consult the suite on
    a fresh dispatch when FULL_SUITE_DONE_BAR is set — mirrors the existing
    REWORK_FULL_SUITE guard at the same call site, so the flag can't be
    dodged by leaving the tree dirty."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", False)
    monkeypatch.setattr(la, "FULL_SUITE_DONE_BAR", True)
    monkeypatch.setattr(la, "MAX_STEPS", 4)
    monkeypatch.setattr(la, "worktree_dirty", lambda: True)
    monkeypatch.setattr(la, "auto_wip_commit", lambda reason: None)
    monkeypatch.setattr(la, "_full_suite_result",
                        lambda: (False, "assert 9.0 == 3.0 - test_x.py:12"))

    fake, _ = _sequence_chat([("done", {"summary": "bypass attempt"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc != 0, f"auto-accept-at-2 bypassed the done-bar; rc={rc}\n{out!r}"
    assert "DONE with auto-WIP-commit" not in out, out
    assert "done rejected — full test suite still fails" in out, out


def test_fresh_dispatch_parks_after_suite_reject_cap_when_flag_set(
    tmp_path, monkeypatch, capsys):
    """Fresh dispatch, flag on, suite fails REWORK_SUITE_REJECT_CAP times in
    a row: parks with rc=2, same as the rework path."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", False)
    monkeypatch.setattr(la, "FULL_SUITE_DONE_BAR", True)
    monkeypatch.setattr(la, "REWORK_SUITE_REJECT_CAP", 2)
    monkeypatch.setattr(la, "MAX_STEPS", 30)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)

    suite_calls: list = []

    def _suite_spy():
        suite_calls.append(True)
        return (False, "FAILED test_x.py::test_y")

    monkeypatch.setattr(la, "_full_suite_result", _suite_spy)

    fake, _ = _sequence_chat([("done", {"summary": "attempt"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 2, f"expected park (rc=2); got rc={rc}\n{out!r}"
    assert "rework suite-reject cap (2) reached" in out, out
    assert len(suite_calls) == 2, f"suite consulted {len(suite_calls)}x, expected 2"


def test_both_flags_set_runs_suite_once_not_twice(tmp_path, monkeypatch):
    """When both REWORK_FULL_SUITE and FULL_SUITE_DONE_BAR are set, the suite
    must be consulted exactly once per done call, not double-run."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(la, "FULL_SUITE_DONE_BAR", True)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)

    suite_calls: list = []

    def _suite_spy():
        suite_calls.append(True)
        return (True, "")

    monkeypatch.setattr(la, "_full_suite_result", _suite_spy)

    fake, _ = _sequence_chat([("done", {"summary": "all green"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    assert rc == 0
    assert len(suite_calls) == 1, f"suite consulted {len(suite_calls)}x, expected exactly 1"
