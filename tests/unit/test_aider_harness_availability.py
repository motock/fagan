"""aider binary availability gate (PIPELINE_AGENT_HARNESS=aider).

ClaudeCliDriver.dispatch() must accept the registered 'aider' harness ONLY
behind an availability precheck that fails closed BEFORE any subprocess
spawn, worktree write, or other side effect: an unavailable aider binary
must raise a clear error naming both PIPELINE_AGENT_HARNESS and the missing
binary — never silently fall back to the driver's native 'claude' harness
(that would run a completely different agent than the operator selected).

Every test stubs ``shutil.which`` and ``PIPELINE_AGENT_HARNESS`` with
``monkeypatch`` — no test depends on whether aider is really installed on
this host (.claude/rules/testing-config-gates.md). The Popen stubbing
follows tests/unit/test_harness_guard.py's _FakePopenResult pattern.

The aider-spawn tests also pin the CHILD ENVIRONMENT contract:
``HarnessCommand.env`` is additional-vars-only and ``subprocess.Popen`` REPLACES
the environment whenever ``env`` is given, so the env reaching Popen must be
the caller's ``os.environ`` MERGED with the harness's additions — an unmerged
``env={}`` would spawn aider with no PATH/HOME at all.
"""

from __future__ import annotations

import os
import shutil

import pytest

from app import backend as b
from app import harness
from app.harness import HarnessRequest, get_harness
from tests.unit._backend_helpers import _FakePopenResult

ENV_VAR = "PIPELINE_AGENT_HARNESS"
FAKE_AIDER = "/tmp/fake-aider/aider"


def _dispatch(driver, tmp_path):
    return driver.dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )


def _aider_request(tmp_path):
    """The exact HarnessRequest ClaudeCliDriver.dispatch builds for aider."""
    return HarnessRequest(
        prompt="do it", system=None, model="opus", cwd=str(tmp_path),
        options={"allowed_tools": "Bash,Read"},
    )


# ---------------------------------------------------------------------------
# app/harness.aider_binary_available()
# ---------------------------------------------------------------------------


def test_available_returns_true_when_which_resolves(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/aider")
    assert harness.aider_binary_available() == (True, "")


def test_unavailable_returns_false_reason_without_raising(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    ok, reason = harness.aider_binary_available()
    assert ok is False
    assert "aider" in reason


# ---------------------------------------------------------------------------
# ClaudeCliDriver.dispatch with PIPELINE_AGENT_HARNESS=aider
# ---------------------------------------------------------------------------


def test_dispatch_spawns_aider_when_available(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "aider")
    monkeypatch.setattr(shutil, "which", lambda name: FAKE_AIDER)
    captured = {}

    def fake_popen(cmd, cwd, env, stdout, stderr):
        captured["argv"] = cmd
        captured["env"] = env
        return _FakePopenResult(301)

    monkeypatch.setattr(b.subprocess, "Popen", fake_popen)

    handle = _dispatch(b.ClaudeCliDriver(), tmp_path)

    assert handle.pid == 301
    assert captured["argv"][0].endswith("aider")
    # The child environment must be the caller's environment MERGED with the
    # harness's additional vars — subprocess.Popen(env=...) REPLACES, so an
    # unmerged env={} would spawn aider with no PATH/HOME at all.
    assert "PATH" in captured["env"]
    assert captured["env"]["PATH"] == os.environ["PATH"]
    assert captured["env"]["HOME"] == os.environ["HOME"]
    # Full merge contract: inherited os.environ plus every HarnessCommand.env
    # addition (computed from the same AiderHarness path dispatch uses, so
    # provider keys supplied by a future caller are covered too).
    command = get_harness("aider").build_agent_command(_aider_request(tmp_path))
    assert captured["env"] == {**os.environ, **command.env}


def test_dispatch_fails_closed_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "aider")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []

    def fake_popen(cmd, cwd, env, stdout, stderr):
        calls.append({"argv": cmd, "env": env})
        return _FakePopenResult(302)

    monkeypatch.setattr(b.subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError) as excinfo:
        _dispatch(b.ClaudeCliDriver(), tmp_path)

    message = str(excinfo.value)
    assert ENV_VAR in message
    assert "aider" in message
    # Fail closed: the unavailable binary must never fall through to the
    # native 'claude' spawn — no side effect at all.
    assert calls == []

    # Follow-up call with the binary now available: the failed call left no
    # residual state behind, and the spawn goes to aider with the caller's
    # environment MERGED with the harness's additional vars (Popen(env=...)
    # replaces, so an unmerged env would strip PATH/HOME from the child).
    monkeypatch.setattr(shutil, "which", lambda name: FAKE_AIDER)
    handle = _dispatch(b.ClaudeCliDriver(), tmp_path)
    assert len(calls) == 1
    assert calls[0]["argv"][0].endswith("aider")
    assert "PATH" in calls[0]["env"]
    assert calls[0]["env"]["PATH"] == os.environ["PATH"]
    assert calls[0]["env"]["HOME"] == os.environ["HOME"]
    command = get_harness("aider").build_agent_command(_aider_request(tmp_path))
    assert calls[0]["env"] == {**os.environ, **command.env}
    assert handle.pid == 302


def test_unregistered_value_still_raises_valueerror(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "aidr")
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda cmd, cwd, env, stdout, stderr: _FakePopenResult(303),
    )

    with pytest.raises(ValueError) as excinfo:
        _dispatch(b.ClaudeCliDriver(), tmp_path)

    assert ENV_VAR in str(excinfo.value)


# ---------------------------------------------------------------------------
# Regression guards: native path + the OTHER driver's unchanged guard
# ---------------------------------------------------------------------------


def test_regression_native_and_other_driver_guard(tmp_path, monkeypatch):
    # (a) PIPELINE_AGENT_HARNESS unset -> ClaudeCliDriver still dispatches
    # through its own native 'claude' harness.
    monkeypatch.delenv(ENV_VAR, raising=False)
    captured = {}

    def fake_popen(cmd, cwd, env, stdout, stderr):
        captured["argv"] = cmd
        return _FakePopenResult(304)

    monkeypatch.setattr(b.subprocess, "Popen", fake_popen)
    handle = _dispatch(b.ClaudeCliDriver(), tmp_path)
    assert handle.pid == 304
    assert captured["argv"][0] == "claude"

    # (b) PIPELINE_AGENT_HARNESS=aider against OllamaDriver still raises the
    # UNCHANGED cross-harness NotImplementedError.
    monkeypatch.setenv(ENV_VAR, "aider")
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: _FakePopenResult(305),
    )
    with pytest.raises(NotImplementedError) as excinfo:
        b.OllamaDriver().dispatch(
            "do it", system=None, model="sonnet",
            allowed_tools="Bash,Edit,Write,Read",
            cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        )
    message = str(excinfo.value)
    assert ENV_VAR in message
    assert "aider" in message