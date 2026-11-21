"""The command-wrap seam wired into ``spawn_harness``.

With ``PIPELINE_COMMAND_WRAP_PLUGIN`` set, the argv handed to ``spawn_local`` is
the plugin's answer, with kind ``review`` for the review role and ``agent``
otherwise. The seam cannot be combined with ssh execution or with
``PIPELINE_SANDBOX=docker``: either combination raises before anything spawns.
With the variable unset the argv is passed through unchanged.
"""

from __future__ import annotations

import shutil

import pytest

from pipeline import execution, tenant_exec
from pipeline.actor import bind_namespace

WRAP_VAR = "PIPELINE_COMMAND_WRAP_PLUGIN"
ALLOWLIST_VAR = "PIPELINE_PLUGIN_MODULES"
EXEC_DISPATCH_VAR = "PIPELINE_EXEC_DISPATCH"
EXEC_REVIEW_VAR = "PIPELINE_EXEC_REVIEW"
SANDBOX_VAR = "PIPELINE_SANDBOX"

_STATE: dict = {}


class _StubWrapper:
    def wrap(self, argv, *, cwd, kind, namespace):
        _STATE.setdefault("calls", []).append(
            {"argv": list(argv), "cwd": cwd, "kind": kind, "namespace": namespace}
        )
        return ["wrapped", *argv]


def build():
    """Factory the seam loads through this module's allowlist entry."""
    return _StubWrapper()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    tenant_exec.reset_for_tests()
    _STATE.clear()
    for var in (
        WRAP_VAR,
        ALLOWLIST_VAR,
        EXEC_DISPATCH_VAR,
        EXEC_REVIEW_VAR,
        SANDBOX_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    tenant_exec.reset_for_tests()
    _STATE.clear()


@pytest.fixture
def spawned(monkeypatch):
    """Capture the call that ``spawn_harness`` makes to ``spawn_local``."""
    calls = []

    def fake_spawn_local(cmd, **kwargs):
        calls.append({"cmd": list(cmd), **kwargs})
        return "handle"

    monkeypatch.setattr(execution, "spawn_local", fake_spawn_local)
    return calls


@pytest.fixture
def no_ssh(monkeypatch):
    """Fail the test if the ssh spawn path is reached."""

    def refuse(*args, **kwargs):
        raise AssertionError("ssh spawn must not be reached")

    monkeypatch.setattr(execution, "_spawn_ssh", refuse)


@pytest.fixture
def wrap_plugin(monkeypatch):
    monkeypatch.setenv(WRAP_VAR, f"{__name__}:build")
    monkeypatch.setenv(ALLOWLIST_VAR, __name__)


def _spawn(tmp_path, role="dispatch", cmd=None):
    return execution.spawn_harness(
        cmd or ["python", "-m", "agent"],
        cwd=tmp_path,
        log_path=tmp_path / "agent.log",
        append=False,
        role=role,
    )


class TestUnconfigured:
    def test_argv_passes_through_unchanged(self, tmp_path, spawned):
        argv = ["python", "-m", "agent"]

        _spawn(tmp_path, cmd=argv)

        assert spawned[0]["cmd"] == argv
        assert "wrapped" not in spawned[0]["cmd"]


class TestConfigured:
    def test_spawn_local_receives_the_wrapped_argv(
        self, tmp_path, wrap_plugin, spawned
    ):
        _spawn(tmp_path, cmd=["python", "-m", "agent"])

        assert spawned[0]["cmd"] == ["wrapped", "python", "-m", "agent"]

    def test_dispatch_role_passes_kind_agent_and_the_worktree_cwd(
        self, tmp_path, wrap_plugin, spawned
    ):
        with bind_namespace("acme"):
            _spawn(tmp_path, role="dispatch")

        call = _STATE["calls"][0]
        assert call["kind"] == "agent"
        assert call["cwd"] == str(tmp_path)
        assert call["namespace"] == "acme"

    def test_review_role_passes_kind_review(self, tmp_path, wrap_plugin, spawned):
        _spawn(tmp_path, role="review")

        assert _STATE["calls"][0]["kind"] == "review"
        assert spawned[0]["cmd"][0] == "wrapped"

    def test_ssh_mode_with_plugin_raises_before_spawning(
        self, tmp_path, wrap_plugin, spawned, no_ssh, monkeypatch
    ):
        monkeypatch.setenv(EXEC_DISPATCH_VAR, "ssh")

        with pytest.raises(ValueError, match="cannot be combined with ssh execution"):
            _spawn(tmp_path)

        assert spawned == []
        assert "calls" not in _STATE

    def test_docker_sandbox_with_plugin_raises_before_spawning(
        self, tmp_path, wrap_plugin, spawned, monkeypatch
    ):
        monkeypatch.setenv(SANDBOX_VAR, "docker")
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )

        with pytest.raises(
            ValueError, match="cannot be combined with PIPELINE_SANDBOX=docker"
        ):
            _spawn(tmp_path)

        assert spawned == []
        assert "calls" not in _STATE

    def test_plugin_without_allowlist_refuses_the_spawn(
        self, tmp_path, monkeypatch, spawned
    ):
        monkeypatch.setenv(WRAP_VAR, f"{__name__}:build")

        with pytest.raises(tenant_exec.CommandWrapError, match="command wrap refused"):
            _spawn(tmp_path)

        assert spawned == []
