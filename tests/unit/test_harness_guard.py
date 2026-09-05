"""PIPELINE_AGENT_HARNESS dispatch-guard coverage.

ClaudeCliDriver.dispatch() and OllamaDriver.dispatch() must enforce
resolve_harness_name('claude') / resolve_harness_name('local') as a guard
before spawning anything: PIPELINE_AGENT_HARNESS unset or matching the
driver's own harness proceeds; a cross-harness (registered, but mismatched)
value must fail closed with NotImplementedError naming both the env var and
the offending value — before any side effect.

Every test uses the ``monkeypatch`` fixture (never a bare ``os.environ``
assignment) so the env var is restored at teardown and cannot leak into the
pre-existing env-unset dispatch tests that run after these.
"""

from __future__ import annotations

import pytest

from app import backend as b
from tests.unit._backend_helpers import _FakePopenResult

ENV_VAR = "PIPELINE_AGENT_HARNESS"


# ---------------------------------------------------------------------------
# ClaudeCliDriver (native harness: 'claude')
# ---------------------------------------------------------------------------


def test_claude_dispatch_proceeds_when_harness_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda cmd, cwd, env, stdout, stderr: _FakePopenResult(101),
    )

    handle = b.ClaudeCliDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert handle.pid == 101


def test_claude_dispatch_proceeds_when_harness_env_matches(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "claude")
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda cmd, cwd, env, stdout, stderr: _FakePopenResult(102),
    )

    handle = b.ClaudeCliDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert handle.pid == 102


def test_claude_dispatch_raises_on_cross_harness_value(tmp_path, monkeypatch):
    """A cross-harness value (registered, but not 'claude') must fail closed
    rather than silently dispatching via ClaudeCliDriver anyway — and the
    guard must re-read the env on every dispatch, so a follow-up call with
    the var unset proceeds normally (no cached decision from call 1)."""
    monkeypatch.setenv(ENV_VAR, "local")
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda cmd, cwd, env, stdout, stderr: _FakePopenResult(103),
    )

    with pytest.raises(NotImplementedError) as excinfo:
        b.ClaudeCliDriver().dispatch(
            "do it", system=None, model="opus", allowed_tools="Bash,Read",
            cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        )

    message = str(excinfo.value)
    assert ENV_VAR in message
    assert "local" in message

    # Follow-up call: the failed call left no residual state behind.
    monkeypatch.delenv(ENV_VAR, raising=False)
    handle = b.ClaudeCliDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )
    assert handle.pid == 103


# ---------------------------------------------------------------------------
# OllamaDriver (native harness: 'local')
# ---------------------------------------------------------------------------


def test_ollama_dispatch_proceeds_when_harness_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: _FakePopenResult(201),
    )

    handle = b.OllamaDriver().dispatch(
        "do it", system=None, model="sonnet", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert handle.pid == 201


def test_ollama_dispatch_proceeds_when_harness_env_matches(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "local")
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: _FakePopenResult(202),
    )

    handle = b.OllamaDriver().dispatch(
        "do it", system=None, model="sonnet", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert handle.pid == 202


def test_ollama_dispatch_raises_on_cross_harness_value(tmp_path, monkeypatch):
    """A cross-harness value (registered, but not 'local') must fail closed
    rather than silently dispatching via OllamaDriver anyway — and the
    guard must re-read the env on every dispatch, so a follow-up call with
    the var unset proceeds normally (no cached decision from call 1)."""
    monkeypatch.setenv(ENV_VAR, "claude")
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: _FakePopenResult(203),
    )

    with pytest.raises(NotImplementedError) as excinfo:
        b.OllamaDriver().dispatch(
            "do it", system=None, model="sonnet", allowed_tools="Bash,Edit,Write,Read",
            cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        )

    message = str(excinfo.value)
    assert ENV_VAR in message
    assert "claude" in message

    # Follow-up call: the failed call left no residual state behind.
    monkeypatch.delenv(ENV_VAR, raising=False)
    handle = b.OllamaDriver().dispatch(
        "do it", system=None, model="sonnet", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )
    assert handle.pid == 203