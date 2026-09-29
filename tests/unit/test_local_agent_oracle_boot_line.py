"""The oracle harness (scripts/local_agent_oracle.py) must print the same
`[boot]` startup line scripts/local_agent.py prints, as the FIRST output of
every run.

pipeline/rebrief.py `_current_attempt_log` isolates the CURRENT attempt with
`text.rfind("[boot]")`. agent.log is appended to across dispatch attempts, so
without a boot line every oracle-mode story's step-cap rebrief reads all prior
attempts' output as one attempt (the exact failure its docstring warns about),
and the watchdog's `last_log_line` evidence cannot be tied to an attempt.

Shared fixtures/helpers (including the loaded `lao` module) live in
tests.unit._local_agent_oracle_test_helpers.
"""
import os

from pipeline import rebrief
from tests.unit._local_agent_oracle_test_helpers import (  # noqa: F401
    _init_git_repo,
    _isolate_environ,
    _sequence_chat,
    lao,
)


def _wire_run(tmp_path, monkeypatch, responses):
    """Deterministic wiring for one lao.main() run (same shape as
    test_local_agent_oracle_persistence_and_finish's rework-round test).
    Returns the fake chat's recorded call list."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["tests/test_acceptance.py"])
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", False)
    monkeypatch.setattr(lao, "MAX_STEPS", 1)
    monkeypatch.setattr(lao, "oracle_result", lambda: (True, "(oracle green)"))
    monkeypatch.setattr(lao, "worktree_dirty", lambda: False)
    monkeypatch.setattr(lao, "auto_commit", lambda reason: None)
    fake, calls = _sequence_chat(responses)
    monkeypatch.setattr(lao, "chat", fake)
    return calls


def _expected_boot_line():
    """The documented shape, built from the module's own config globals (so a
    monkeypatched MAX_STEPS is reflected, exactly as the implementation's
    f-string reads it at print time)."""
    return (
        f"[boot] pid={os.getpid()} model={lao.MODEL} endpoint={lao.ENDPOINT} "
        f"provider={lao.PROVIDER} steps={lao.MAX_STEPS} timeout={lao.TIMEOUT}s"
    )


def test_should_print_boot_line_first(tmp_path, monkeypatch, capsys):
    """The boot line is the first non-empty stdout line of the run, carries
    this process's pid, and has the full documented field set."""
    _wire_run(tmp_path, monkeypatch, [("done", {"summary": "all done"})])

    lao.main()
    out = capsys.readouterr().out

    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, f"run produced no stdout at all:\n{out!r}"
    first = lines[0]
    assert first.startswith("[boot] pid="), f"first stdout line was {first!r}\n{out!r}"
    assert f"pid={os.getpid()}" in first, first
    assert first == _expected_boot_line(), first


def test_should_print_boot_line_once_per_run(tmp_path, monkeypatch, capsys):
    """Exactly one boot line per run - a duplicate would make rfind() pick the
    wrong attempt boundary."""
    _wire_run(tmp_path, monkeypatch, [("bash", {"command": "echo hi"})])

    lao.main()
    out = capsys.readouterr().out

    assert out.count("[boot]") == 1, out


def test_should_let_rebrief_isolate_the_latest_attempt(tmp_path, monkeypatch, capsys):
    """Integration: agent.log holding two consecutive runs' stdout must let
    pipeline.rebrief._current_attempt_log return ONLY the second attempt - the
    first attempt's marker text must be gone, and the slice must start at the
    boot line."""
    marker = "FIRST-ATTEMPT-MARKER"

    _wire_run(tmp_path, monkeypatch, [("bash", {"command": f"echo {marker}"})])
    lao.main()
    first_out = capsys.readouterr().out
    assert marker in first_out, f"test precondition: marker never echoed\n{first_out!r}"

    _wire_run(tmp_path, monkeypatch, [("done", {"summary": "second attempt"})])
    lao.main()
    second_out = capsys.readouterr().out

    (tmp_path / "agent.log").write_text(first_out + second_out, encoding="utf-8")

    current = rebrief._current_attempt_log(tmp_path)
    assert current is not None
    assert marker not in current, (
        "rebrief read the FIRST attempt's output as part of the current "
        f"attempt:\n{current!r}"
    )
    assert current.startswith("[boot]"), current
