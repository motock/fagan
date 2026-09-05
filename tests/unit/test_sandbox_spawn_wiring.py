"""Spec tests for the PIPELINE_SANDBOX wiring inside spawn_harness (b2 story).

Contract being specified, exercised through the public ``spawn_harness`` seam:

- ``PIPELINE_SANDBOX`` unset/empty/``none``: the spawn is BYTE-IDENTICAL to the
  pre-change behavior — same argv, same cwd/env/stdout/stderr Popen kwargs, no
  docker tokens anywhere. The secure default is provably unchanged.
- ``docker`` + docker binary present: the spawned argv is wrapped via
  ``build_docker_command(str(cwd), argv, allowlisted_env)``; cwd unchanged
  (same-path mount); ``-e`` flags carry ONLY ``LOCAL_AGENT_*``/``PIPELINE_*``
  keys from the dispatch env dict (deny-by-default; no host env leaks).
- ``docker`` + docker binary ABSENT: RuntimeError naming ``PIPELINE_SANDBOX``,
  saying docker is not installed and dispatch is REFUSED — and the Popen call
  site is never reached (a silent unsandboxed fallback is the exact bug class
  this story exists to prevent).
- Unknown ``PIPELINE_SANDBOX`` value: the resolver's ``ValueError`` propagates
  out of ``spawn_harness`` — no catching, no fallback.
- The ssh branch and the PIPELINE_EXEC_{ROLE} fail-closed gate are untouched.

Docker is never required: ``shutil.which`` is patched and Popen is faked at the
``pipeline.execution`` call site, so this suite runs in CI without docker.
"""

import shutil
import sys
from types import SimpleNamespace

import pytest

from pipeline import execution
from pipeline.execution import spawn_harness

FAKE_PID = 4242
IMAGE = "registry.example/agent:1.2.3"
DOCKER_TOKENS = ("docker", "-v", "--workdir", "-e")


@pytest.fixture
def popen_calls(monkeypatch):
    """Fake the Popen call site in pipeline.execution (seam-test shape)."""
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
def docker_present(monkeypatch):
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )


@pytest.fixture
def docker_absent(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)


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


def _assert_streams_into_log(call, log_path, append):
    """stdout/stderr must stream into the caller's log file, as today."""
    mode = "a" if append else "w"
    assert call["stdout"] is call["stderr"]
    assert call["stdout"].name == str(log_path) and call["stdout"].mode == mode
    assert call["stderr"].name == str(log_path) and call["stderr"].mode == mode


@pytest.mark.parametrize("append", [True, False])
@pytest.mark.parametrize(
    ("sandbox_id", "sandbox_value"),
    [("unset", None), ("empty", ""), ("none", "none"), ("spaced-upper", "  NONE  ")],
)
def test_none_mode_spawn_is_byte_identical(
    tmp_path, popen_calls, monkeypatch, sandbox_id, sandbox_value, append
):
    if sandbox_value is None:
        monkeypatch.delenv("PIPELINE_SANDBOX", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_SANDBOX", sandbox_value)
    monkeypatch.delenv("PIPELINE_SANDBOX_IMAGE", raising=False)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    log_path, cmd = tmp_path / "agent.log", ["claude", "--print", "hello"]
    env = {"LOCAL_AGENT_MODEL": "claude-3", "PATH": "/usr/bin", "AWS_SECRET": "x"}

    handle = spawn_harness(
        cmd, cwd=worktree, log_path=log_path, append=append, env=env
    )

    assert len(popen_calls) == 1
    call = popen_calls[0]
    # Byte-identical argv: the harness argv itself, no docker wrapper tokens.
    assert call["cmd"] == cmd
    assert not any(tok in call["cmd"] for tok in DOCKER_TOKENS)
    # Byte-identical Popen kwargs: cwd/env untouched, log streaming preserved.
    assert call["cwd"] == worktree
    assert call["env"] == env
    assert call["extra"] == {}
    _assert_streams_into_log(call, log_path, append)
    # story_status-facing handle shape unchanged.
    assert handle.pid == FAKE_PID and handle.model == ""


@pytest.mark.parametrize("append", [True, False])
@pytest.mark.parametrize("sandbox_value", ["docker", "DOCKER"], ids=["lower", "upper"])
def test_docker_mode_wraps_argv_and_filters_env(
    tmp_path, popen_calls, monkeypatch, docker_present, sandbox_value, append
):
    monkeypatch.setenv("PIPELINE_SANDBOX", sandbox_value)
    monkeypatch.setenv("PIPELINE_SANDBOX_IMAGE", IMAGE)
    # Host env vars that must NOT leak into the container's -e flags.
    monkeypatch.setenv("HOST_ONLY_VAR", "host-leak")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    log_path, cmd = tmp_path / "agent.log", ["claude", "--print", "hello"]
    env = {
        "LOCAL_AGENT_MODEL": "claude-3",
        "PIPELINE_STEP_ID": "s7",
        "PATH": "/usr/sbin:/usr/bin",
        "AWS_SECRET_ACCESS_KEY": "leak-me-not",
    }

    handle = spawn_harness(
        cmd, cwd=worktree, log_path=log_path, append=append, env=env
    )

    assert len(popen_calls) == 1
    call = popen_calls[0]
    argv = call["cmd"]
    # docker run wrapper in front, original argv intact at the tail.
    assert argv[:2] == ["docker", "run"] and "--rm" in argv
    assert argv[-len(cmd):] == cmd and IMAGE in argv[: -len(cmd)]
    # Same-path volume mount and workdir at the host worktree path.
    assert argv[argv.index("-v") + 1] == f"{worktree}:{worktree}"
    assert argv[argv.index("--workdir") + 1] == str(worktree)
    # -e flags limited to LOCAL_AGENT_*/PIPELINE_* keys from the dispatch env.
    flags = _env_flags(argv)
    assert sorted(flags) == ["LOCAL_AGENT_MODEL=claude-3", "PIPELINE_STEP_ID=s7"]
    for flag in flags:
        assert flag.split("=", 1)[0].startswith(("LOCAL_AGENT_", "PIPELINE_"))
    for leaked in (
        "PATH=",
        "AWS_SECRET_ACCESS_KEY=",
        "HOST_ONLY_VAR=",
        "DATABASE_URL=",
    ):
        assert not any(f.startswith(leaked) for f in flags)
    # cwd unchanged (same-path mount), log streaming preserved, handle shape.
    assert call["cwd"] == worktree
    _assert_streams_into_log(call, log_path, append)
    assert handle.pid == FAKE_PID and handle.model == ""
    assert cmd == ["claude", "--print", "hello"]  # caller's argv not mutated


@pytest.mark.parametrize(
    "env",
    [None, {"PATH": "/usr/bin", "HOME": "/root", "LANG": "C"}],
    ids=["none-env", "disallowed-only"],
)
def test_docker_mode_deny_by_default_env(
    tmp_path, popen_calls, monkeypatch, docker_present, env
):
    monkeypatch.setenv("PIPELINE_SANDBOX", "docker")
    monkeypatch.setenv("PIPELINE_SANDBOX_IMAGE", IMAGE)
    monkeypatch.setenv("HOST_ONLY_VAR", "host-leak")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    spawn_harness(
        ["claude", "--print"],
        cwd=worktree,
        log_path=tmp_path / "agent.log",
        append=True,
        env=env,
    )

    argv = popen_calls[0]["cmd"]
    assert argv[:2] == ["docker", "run"]
    # Deny-by-default: no -e flags at all, never a host-environment fallback.
    assert _env_flags(argv) == []


def test_docker_absent_refuses_fail_closed(
    tmp_path, popen_calls, monkeypatch, docker_absent
):
    monkeypatch.setenv("PIPELINE_SANDBOX", "docker")
    monkeypatch.setenv("PIPELINE_SANDBOX_IMAGE", IMAGE)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    with pytest.raises(RuntimeError) as excinfo:
        spawn_harness(
            ["claude"],
            cwd=worktree,
            log_path=tmp_path / "agent.log",
            append=True,
            env=None,
        )

    msg = str(excinfo.value)
    assert "PIPELINE_SANDBOX" in msg
    assert "docker" in msg.lower()
    assert "not installed" in msg.lower()
    assert "refus" in msg.lower()  # REFUSED / refusing / refusal
    # Fail-closed core: the Popen call site is NEVER reached — never exec the
    # harness directly on the host when the sandbox cannot be honored.
    assert popen_calls == []


@pytest.mark.parametrize("docker_up", [True, False], ids=["docker-present", "docker-absent"])
def test_unknown_sandbox_value_raises_valueerror(
    tmp_path, popen_calls, monkeypatch, docker_up
):
    monkeypatch.setenv("PIPELINE_SANDBOX", "podman")
    monkeypatch.setenv("PIPELINE_SANDBOX_IMAGE", IMAGE)
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/docker" if docker_up else None
    )
    worktree = tmp_path / "wt"
    worktree.mkdir()

    with pytest.raises(ValueError) as excinfo:
        spawn_harness(
            ["claude"],
            cwd=worktree,
            log_path=tmp_path / "agent.log",
            append=True,
            env=None,
        )

    assert "PIPELINE_SANDBOX" in str(excinfo.value)
    assert "podman" in str(excinfo.value)
    assert popen_calls == []


@pytest.mark.parametrize("image_value", [None, ""], ids=["unset", "empty"])
def test_docker_mode_without_image_fails_closed(
    tmp_path, popen_calls, monkeypatch, docker_present, image_value
):
    monkeypatch.setenv("PIPELINE_SANDBOX", "docker")
    if image_value is None:
        monkeypatch.delenv("PIPELINE_SANDBOX_IMAGE", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_SANDBOX_IMAGE", image_value)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    with pytest.raises(ValueError) as excinfo:
        spawn_harness(
            ["claude"],
            cwd=worktree,
            log_path=tmp_path / "agent.log",
            append=True,
            env=None,
        )

    assert "PIPELINE_SANDBOX_IMAGE" in str(excinfo.value)
    assert popen_calls == []


@pytest.mark.parametrize("sandbox", [None, "docker"], ids=["unset", "docker"])
def test_ssh_branch_untouched(tmp_path, popen_calls, monkeypatch, docker_present, sandbox):
    # B1 remote-SSH-execution driver is now implemented; the old
    # NotImplementedError pin is obsolete (review Blocking 1). The ssh branch
    # must dispatch via _spawn_ssh -> `python -m pipeline.remote_exec`, and it
    # bypasses local docker-sandbox resolution entirely.
    monkeypatch.setenv("PIPELINE_EXEC_DISPATCH", "ssh")
    monkeypatch.setenv("PIPELINE_REMOTE_EXEC_HOST", "gpu-host")
    monkeypatch.setenv("PIPELINE_REMOTE_SYNC_ROOT", "/srv/sync")
    if sandbox is None:
        monkeypatch.delenv("PIPELINE_SANDBOX", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_SANDBOX", sandbox)
        monkeypatch.setenv("PIPELINE_SANDBOX_IMAGE", IMAGE)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    # _spawn_ssh derives the branch via git; stub it out (external boundary).
    monkeypatch.setattr(
        execution.subprocess,
        "check_output",
        lambda *a, **kw: "main\n",
    )

    handle = spawn_harness(
        ["claude"],
        cwd=worktree,
        log_path=tmp_path / "agent.log",
        append=True,
        env=None,
    )

    assert len(popen_calls) == 1
    call = popen_calls[0]
    # Expect remote_exec invocation
    assert call["cmd"][0] == sys.executable
    assert call["cmd"][1] == "-m"
    assert call["cmd"][2] == "pipeline.remote_exec"
    # remote exec flags
    assert "--worktree" in call["cmd"]
    assert "--remote-url" in call["cmd"]
    assert "--host" in call["cmd"]
    assert "--spec-file" in call["cmd"]
    assert handle.pid == FAKE_PID


def test_unknown_exec_mode_still_fails_closed(tmp_path, popen_calls, monkeypatch):
    monkeypatch.setenv("PIPELINE_EXEC_DISPATCH", "teleport")
    monkeypatch.delenv("PIPELINE_SANDBOX", raising=False)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    with pytest.raises(ValueError) as excinfo:
        spawn_harness(
            ["claude"],
            cwd=worktree,
            log_path=tmp_path / "agent.log",
            append=True,
            env=None,
        )

    assert "PIPELINE_EXEC_DISPATCH" in str(excinfo.value)
    assert popen_calls == []