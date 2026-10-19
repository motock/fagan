"""Spec tests for SEC-2: pipeline secrets never reach agent subprocesses.

The defect: ``pipeline/__init__.py`` loads ``.pipeline.env`` into
``os.environ``, and agent launches passed that environment through unchanged.
Every dispatched agent has a bash tool, so it could read
``PIPELINE_NOTIFY_EMAIL_PASSWORD`` (the Gmail app password) and, for
MCP-spawned runs, ``PLANE_API_KEY`` — and a prompt-injected agent could emit
the credential into a tool result sent to a cloud model.

Contract pinned here, exercised through the public ``spawn_harness`` seam:

- local spawn with an explicit env: the child env is filtered — secret names
  are gone, ordinary keys survive.
- local spawn with ``env=None``: the child env is an explicit dict (the
  inherited ``os.environ`` materialized and filtered), so a marker variable
  set in ``os.environ`` is present and the secret is not.
- docker mode with ``env=None``: still no ``-e`` flags at all (deny by
  default; the sandbox-wiring suite remains the authority for that shape).
- docker mode with an explicit env: the generated ``-e`` flags never carry a
  secret name, even though the ``PIPELINE_*`` allowlist would otherwise keep
  ``PIPELINE_NOTIFY_EMAIL_PASSWORD``.

Docker is never required: ``shutil.which`` is patched and the Popen call site
in ``pipeline.execution`` is faked, mirroring
``tests/unit/test_sandbox_spawn_wiring.py``.
"""

import shutil
from types import SimpleNamespace

import pytest

from pipeline import execution
from pipeline.execution import spawn_harness

FAKE_PID = 4242
IMAGE = "registry.example/agent:1.2.3"
SECRET_KEYS = ("PIPELINE_NOTIFY_EMAIL_PASSWORD", "PLANE_API_KEY")


@pytest.fixture
def popen_calls(monkeypatch):
    """Fake the Popen call site in pipeline.execution (docker cases)."""
    calls = []

    def fake_popen(cmd, cwd=None, env=None, stdout=None, stderr=None, **extra):
        calls.append(
            {
                "cmd": list(cmd),
                "cwd": cwd,
                "env": env,
                "stdout": stdout,
                "stderr": stderr,
                "extra": extra,
            }
        )
        return SimpleNamespace(pid=FAKE_PID)

    monkeypatch.setattr(execution.subprocess, "Popen", fake_popen)
    return calls


@pytest.fixture
def local_spawn_calls(monkeypatch):
    """Fake ``execution.spawn_local`` so the local branch's env is observable."""
    calls = []

    def fake_spawn_local(
        cmd,
        *,
        cwd,
        log_path,
        append,
        env=None,
        line_filter=None,
        _fd_redirect=True,
    ):
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
        return SimpleNamespace(pid=FAKE_PID, model="")

    monkeypatch.setattr(execution, "spawn_local", fake_spawn_local)
    return calls


@pytest.fixture
def local_mode(monkeypatch):
    """Pin the local execution mode and no sandbox for the local cases."""
    monkeypatch.delenv("PIPELINE_EXEC_DISPATCH", raising=False)
    monkeypatch.delenv("PIPELINE_SANDBOX", raising=False)
    monkeypatch.delenv("PIPELINE_SANDBOX_IMAGE", raising=False)


@pytest.fixture
def docker_present(monkeypatch):
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )


def _env_flags(argv):
    """Return the values passed as ``-e`` flags, asserting well-formed pairs."""
    flags, i = [], 0
    while i < len(argv):
        if argv[i] == "-e":
            assert i + 1 < len(argv), "dangling -e flag at end of argv"
            flags.append(argv[i + 1])
            i += 2
        else:
            i += 1
    return flags


def test_local_spawn_strips_secret_env(tmp_path, local_mode, local_spawn_calls):
    """An explicit env loses the pipeline secrets but keeps ordinary keys."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    env = {
        "PATH": "/usr/bin",
        "PIPELINE_LOCAL_MAX_STEPS": "40",
        "PIPELINE_NOTIFY_EMAIL_PASSWORD": "gmail-app-password",
        "PLANE_API_KEY": "plane-key",
    }

    spawn_harness(
        ["claude", "--print", "hello"],
        cwd=worktree,
        log_path=tmp_path / "agent.log",
        append=False,
        env=env,
    )

    assert len(local_spawn_calls) == 1
    child_env = local_spawn_calls[0]["env"]
    assert child_env is not None
    for key in SECRET_KEYS:
        assert key not in child_env, f"{key} leaked into the local agent env"
    assert child_env["PATH"] == "/usr/bin"
    assert child_env["PIPELINE_LOCAL_MAX_STEPS"] == "40"
    # The caller's dict is not mutated in place.
    assert env["PIPELINE_NOTIFY_EMAIL_PASSWORD"] == "gmail-app-password"


def test_local_spawn_none_env_materializes_filtered_copy(
    tmp_path, local_mode, local_spawn_calls, monkeypatch
):
    """``env=None`` inherits os.environ, but the inherited copy is filtered."""
    monkeypatch.setenv("SEC2_INHERITED_MARKER", "marker-value")
    monkeypatch.setenv("PIPELINE_NOTIFY_EMAIL_PASSWORD", "gmail-app-password")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    spawn_harness(
        ["claude", "--print", "hello"],
        cwd=worktree,
        log_path=tmp_path / "agent.log",
        append=False,
        env=None,
    )

    assert len(local_spawn_calls) == 1
    child_env = local_spawn_calls[0]["env"]
    assert isinstance(child_env, dict), "env=None must be materialized, not inherited"
    assert child_env["SEC2_INHERITED_MARKER"] == "marker-value"
    assert "PIPELINE_NOTIFY_EMAIL_PASSWORD" not in child_env


def test_docker_none_env_passes_no_e_flags(
    tmp_path, popen_calls, docker_present, monkeypatch
):
    """env=None keeps the deny-by-default docker shape: no -e flags at all."""
    monkeypatch.delenv("PIPELINE_EXEC_DISPATCH", raising=False)
    monkeypatch.setenv("PIPELINE_SANDBOX", "docker")
    monkeypatch.setenv("PIPELINE_SANDBOX_IMAGE", IMAGE)
    monkeypatch.setenv("PIPELINE_NOTIFY_EMAIL_PASSWORD", "gmail-app-password")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    spawn_harness(
        ["claude", "--print", "hello"],
        cwd=worktree,
        log_path=tmp_path / "agent.log",
        append=False,
        env=None,
    )

    assert len(popen_calls) == 1
    assert _env_flags(popen_calls[0]["cmd"]) == []


def test_docker_explicit_env_e_flags_exclude_secrets(
    tmp_path, popen_calls, docker_present, monkeypatch
):
    """The PIPELINE_* allowlist must not carry the secret names into -e flags."""
    monkeypatch.delenv("PIPELINE_EXEC_DISPATCH", raising=False)
    monkeypatch.setenv("PIPELINE_SANDBOX", "docker")
    monkeypatch.setenv("PIPELINE_SANDBOX_IMAGE", IMAGE)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    env = {
        "PIPELINE_LOCAL_MAX_STEPS": "40",
        "PIPELINE_NOTIFY_EMAIL_PASSWORD": "gmail-app-password",
        "PLANE_API_KEY": "plane-key",
        "PATH": "/usr/bin",
    }

    spawn_harness(
        ["claude", "--print", "hello"],
        cwd=worktree,
        log_path=tmp_path / "agent.log",
        append=False,
        env=env,
    )

    assert len(popen_calls) == 1
    flags = _env_flags(popen_calls[0]["cmd"])
    flag_names = [flag.split("=", 1)[0] for flag in flags]
    for key in SECRET_KEYS:
        assert key not in flag_names, f"{key} leaked into docker -e flags"
    assert "PIPELINE_LOCAL_MAX_STEPS" in flag_names
