"""Behaviour of the reviewer's explicit ``/bin/sh -c`` runner."""

import subprocess

import pytest

from app import ollama_prompt_utils as opu


@pytest.fixture(autouse=True)
def _no_tenant_exec(monkeypatch):
    from pipeline import tenant_exec

    monkeypatch.setattr(tenant_exec, "is_configured", lambda: False)


def test_compound_command_returns_both_lines(tmp_path):
    result = opu._bounded_bash_run("echo hi && echo there", tmp_path)
    assert result.stdout.split() == ["hi", "there"]
    assert result.returncode == 0


def test_failing_command_returns_nonzero_without_raising(tmp_path):
    result = opu._bounded_bash_run("exit 3", tmp_path)
    assert result.returncode == 3


def test_timeout_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(opu, "_BASH_TIMEOUT_S", 0.2)
    with pytest.raises(subprocess.TimeoutExpired):
        opu._bounded_bash_run("sleep 5", tmp_path)
