"""The reviewer's bash tool must be bounded.

An unbounded reviewer command (`find / ... | xargs grep`) blocked forever on
2026-09-22: the daemon held the plan lock for 34 minutes and every tick
returned "skipped: locked".
"""
import subprocess

from app import ollama_prompt_utils as _opu


def test_bash_tool_has_a_finite_default_ceiling():
    assert isinstance(_opu._BASH_TIMEOUT_S, (int, float))
    assert _opu._BASH_TIMEOUT_S > 0


def test_command_past_the_ceiling_returns_an_error_instead_of_blocking(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(_opu, "_BASH_TIMEOUT_S", 1)
    result = _opu._run_readonly_tool("bash", {"command": "sleep 30"}, tmp_path)
    assert result.startswith("ERROR")
    assert "timed out" in result


def test_timeout_expired_is_handled_internally(tmp_path, monkeypatch):
    monkeypatch.setattr(_opu, "_BASH_TIMEOUT_S", 1)

    def _raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr(_opu.subprocess, "run", _raise_timeout)
    result = _opu._run_readonly_tool("bash", {"command": "echo hi"}, tmp_path)
    assert "timed out" in result


def test_command_within_the_ceiling_still_returns_its_output(tmp_path):
    result = _opu._run_readonly_tool("bash", {"command": "echo hello"}, tmp_path)
    assert result.strip() == "hello"
