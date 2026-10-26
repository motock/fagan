import sys

import pytest
from pipeline.runtime_check import assert_interpreter, interpreter_problem


def test_interpreter_problem_healthy(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path))
    assert interpreter_problem() is None


def test_interpreter_problem_missing(monkeypatch, tmp_path):
    missing = tmp_path / "gone"
    monkeypatch.setattr(sys, "base_prefix", str(missing))
    assert str(missing) in interpreter_problem()


def test_assert_interpreter_healthy(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path))
    assert assert_interpreter() is None


def test_assert_interpreter_missing(monkeypatch, tmp_path, capsys):
    missing = tmp_path / "gone"
    monkeypatch.setattr(sys, "base_prefix", str(missing))
    with pytest.raises(SystemExit) as exc_info:
        assert_interpreter()
    assert exc_info.value.code == 1 and str(missing) in capsys.readouterr().err


def test_message_does_not_leak_environment(monkeypatch, tmp_path):
    sentinel = "SENTINEL_ENV_VALUE_9f3c"
    monkeypatch.setenv("RUNTIME_CHECK_SENTINEL", sentinel)
    missing = tmp_path / "gone"
    monkeypatch.setattr(sys, "base_prefix", str(missing))
    message = interpreter_problem()
    assert not any(
        word in message for word in ("KEY", "TOKEN", "SECRET", sentinel)
    )
