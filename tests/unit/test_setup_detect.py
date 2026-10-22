"""Tests for the read-only guided-setup detection helpers.

``pipeline.setup.detect`` answers three questions for the guided installer:
where the model registry lives and what it says, whether the launchd/systemd
services are rendered and running, and a snapshot of the machine's install
state (checks, venv, MCP registration, personas, policy, registry, services).

Every external call is injected here: no test in this file starts a real
process, reads the real registry, or touches the real ``$HOME``. The module
must never raise and never write, so the tests assert exactly that.
"""

from __future__ import annotations

import importlib
import inspect
import json
import subprocess
import sys
from pathlib import Path

from pipeline.setup import detect

LABEL = "com.fagan.pipeline.advance-scheduler"
CHECKS = [{"name": "python", "ok": True}, {"name": "git", "ok": False}]


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class FakeRunner:
    """Records every ``(argv, timeout)`` and replays canned results.

    ``results``/``raises`` are keyed by a substring of the joined argv, so a
    test can say "the install_checks probe returns this" without pinning the
    interpreter path.
    """

    def __init__(self, results=None, raises=None):
        self.calls: list[tuple[list[str], object]] = []
        self.results = dict(results or {})
        self.raises = dict(raises or {})

    def __call__(self, argv, timeout=None):
        argv = list(argv)
        self.calls.append((argv, timeout))
        joined = " ".join(argv)
        for needle, exc in self.raises.items():
            if needle in joined:
                raise exc
        for needle, result in self.results.items():
            if needle in joined:
                return result
        return (0, "", "")

    def find(self, needle):
        return [call for call in self.calls if needle in " ".join(call[0])]


def _which(result):
    """A recording ``which`` stand-in."""
    seen: list[str] = []

    def which(name):
        seen.append(name)
        return result

    return which, seen


def _registry_payload(providers=("claude", "local"), roles=None):
    return {
        "providers": {name: {"models": {}} for name in providers},
        "roles": {"OVERLORD": {"provider": "claude"}} if roles is None else roles,
    }


def _write_registry(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _make_repo(
    tmp_path,
    *,
    scripts=True,
    venv=True,
    agents=("overlord", "software-engineer"),
    launchd=True,
    systemd=True,
    registry=True,
):
    """A throwaway repo + home pair; nothing here is the real host."""
    repo = tmp_path / "repo"
    repo.mkdir()
    if scripts:
        (repo / "scripts").mkdir()
        (repo / "scripts" / "install_checks.py").write_text("print('[]')\n")
    if venv:
        bin_dir = repo / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        python = bin_dir / "python3"
        python.write_text("#!/bin/sh\n")
        python.chmod(0o755)
    if agents:
        (repo / "agents").mkdir()
        for name in agents:
            (repo / "agents" / f"{name}.md").write_text(f"# {name}\n")
    if launchd:
        (repo / "launchd").mkdir()
        (repo / "launchd" / f"{LABEL}.plist").write_text("<plist/>")
        (repo / "launchd" / "com.fagan.pipeline.usage-poller.plist").write_text("<plist/>")
        (repo / "launchd" / "com.fagan.pipeline.usage-poller.plist.template").write_text("<plist/>")
        (repo / "launchd" / "com.fagan.ollama-num-parallel.plist").write_text("<plist/>")
    if systemd:
        (repo / "systemd").mkdir()
        (repo / "systemd" / f"{LABEL}.service").write_text("[Unit]\n")
        (repo / "systemd" / "com.fagan.pipeline.usage-poller.service.template").write_text("[Unit]\n")
    if registry:
        _write_registry(repo / "model_registry.json", _registry_payload())
    home = tmp_path / "home"
    (home / ".claude" / "agents").mkdir(parents=True)
    return repo, home


def _basenames(paths):
    return sorted(Path(p).name for p in paths)


# --------------------------------------------------------------------------
# public surface
# --------------------------------------------------------------------------


def test_module_exposes_exactly_three_public_functions():
    public = sorted(
        name
        for name, obj in inspect.getmembers(detect, inspect.isfunction)
        if not name.startswith("_") and obj.__module__ == detect.__name__
    )
    assert public == ["collect_state", "detect_registry", "detect_services"]


# --------------------------------------------------------------------------
# detect_registry
# --------------------------------------------------------------------------


def test_registry_env_override_wins(tmp_path):
    repo, _ = _make_repo(tmp_path)
    _write_registry(repo / "model_registry.local.json", _registry_payload(("claude", "local")))
    payload = _registry_payload(("claude", "local", "litellm"))
    override = _write_registry(tmp_path / "elsewhere.json", payload)

    result = detect.detect_registry(
        repo, environ={"PIPELINE_MODEL_REGISTRY_PATH": str(override)}
    )

    assert Path(result["path"]).resolve() == override.resolve()
    assert result["exists"] is True
    assert result["data"] == payload
    assert result["roles"] == payload["roles"]
    assert result["provider_count"] == 3
    assert result["error"] is None


def test_registry_local_file_beats_committed(tmp_path):
    repo, _ = _make_repo(tmp_path)
    local = _write_registry(
        repo / "model_registry.local.json", _registry_payload(("claude", "local"))
    )

    result = detect.detect_registry(repo, environ={})

    assert Path(result["path"]).resolve() == local.resolve()
    assert result["exists"] is True
    assert result["provider_count"] == 2
    assert result["error"] is None


def test_registry_falls_back_to_committed_file(tmp_path):
    repo, _ = _make_repo(tmp_path)

    result = detect.detect_registry(repo, environ={})

    assert Path(result["path"]).resolve() == (repo / "model_registry.json").resolve()
    assert result["exists"] is True
    assert result["provider_count"] == 2
    assert result["roles"] == {"OVERLORD": {"provider": "claude"}}


def test_registry_empty_env_var_is_ignored(tmp_path):
    repo, _ = _make_repo(tmp_path)
    local = _write_registry(
        repo / "model_registry.local.json", _registry_payload(("claude", "local"))
    )

    result = detect.detect_registry(
        repo, environ={"PIPELINE_MODEL_REGISTRY_PATH": ""}
    )

    assert Path(result["path"]).resolve() == local.resolve()


def test_registry_defaults_to_os_environ(tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    override = _write_registry(tmp_path / "from-env.json", _registry_payload(("claude",)))
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(override))

    result = detect.detect_registry(repo)

    assert Path(result["path"]).resolve() == override.resolve()


def test_registry_missing_file_is_not_an_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    result = detect.detect_registry(repo, environ={})

    assert Path(result["path"]).resolve() == (repo / "model_registry.json").resolve()
    assert result["exists"] is False
    assert result["roles"] == {}
    assert result["data"] == {}
    assert result["provider_count"] == 0
    assert result["error"] is None


def test_registry_malformed_json_reports_path_without_raising(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "model_registry.json").write_text("{not: json,,,")

    result = detect.detect_registry(repo, environ={})

    assert result["roles"] == {}
    assert result["data"] == {}
    assert result["provider_count"] == 0
    assert isinstance(result["error"], str) and result["error"]
    assert "model_registry.json" in result["error"]


# --------------------------------------------------------------------------
# detect_services
# --------------------------------------------------------------------------


def test_services_darwin_rendered_and_running(tmp_path):
    repo, _ = _make_repo(tmp_path)
    runner = FakeRunner(results={"launchctl": (0, f"1234\t0\t{LABEL}\n", "")})

    result = detect.detect_services(repo, platform="darwin", runner=runner)

    assert result["platform"] == "darwin"
    assert _basenames(result["rendered"]) == [
        f"{LABEL}.plist",
        "com.fagan.pipeline.usage-poller.plist",
    ]
    assert all(Path(p).exists() for p in result["rendered"])
    assert result["running"] is True
    calls = runner.find("launchctl")
    assert len(calls) == 1
    assert calls[0][0] == ["launchctl", "list"]
    assert calls[0][1] == 10


def test_services_darwin_not_running_when_label_absent(tmp_path):
    repo, _ = _make_repo(tmp_path)
    runner = FakeRunner(results={"launchctl": (0, "-\t0\tcom.fagan.pipeline.usage-poller\n", "")})

    result = detect.detect_services(repo, platform="darwin", runner=runner)

    assert result["running"] is False


def test_services_darwin_label_as_substring_of_another_line(tmp_path):
    repo, _ = _make_repo(tmp_path)
    runner = FakeRunner(results={"launchctl": (0, f"-\t0\t{LABEL}-backup\n", "")})

    result = detect.detect_services(repo, platform="darwin", runner=runner)

    # A sibling label that merely contains ours is a different service: the
    # match is on the whole label field, never a substring of the line.
    assert result["running"] is False


def test_services_darwin_probe_failure_is_none(tmp_path):
    repo, _ = _make_repo(tmp_path)
    runner = FakeRunner(raises={"launchctl": OSError("launchctl exploded")})

    result = detect.detect_services(repo, platform="darwin", runner=runner)

    assert result["running"] is None
    assert _basenames(result["rendered"]) == [
        f"{LABEL}.plist",
        "com.fagan.pipeline.usage-poller.plist",
    ]


def test_services_darwin_timeout_is_none(tmp_path):
    repo, _ = _make_repo(tmp_path)
    runner = FakeRunner(
        raises={"launchctl": subprocess.TimeoutExpired(["launchctl", "list"], 10)}
    )

    result = detect.detect_services(repo, platform="darwin", runner=runner)

    assert result["running"] is None


def test_services_linux_rendered_and_running(tmp_path):
    repo, _ = _make_repo(tmp_path)
    runner = FakeRunner(results={"is-active": (0, "active\n", "")})

    result = detect.detect_services(repo, platform="linux", runner=runner)

    assert result["platform"] == "linux"
    assert _basenames(result["rendered"]) == [f"{LABEL}.service"]
    assert result["running"] is True
    calls = runner.find("is-active")
    assert len(calls) == 1
    assert calls[0][0] == [
        "systemctl",
        "--user",
        "is-active",
        f"{LABEL}.service",
    ]
    assert calls[0][1] == 10


def test_services_linux_nonzero_exit_is_false(tmp_path):
    repo, _ = _make_repo(tmp_path)
    runner = FakeRunner(results={"is-active": (3, "inactive\n", "")})

    result = detect.detect_services(repo, platform="linux", runner=runner)

    assert result["running"] is False


def test_services_linux_probe_failure_is_none(tmp_path):
    repo, _ = _make_repo(tmp_path)
    runner = FakeRunner(raises={"systemctl": OSError("no systemd here")})

    result = detect.detect_services(repo, platform="linux", runner=runner)

    assert result["running"] is None


def test_services_unsupported_platform_is_empty(tmp_path):
    repo, _ = _make_repo(tmp_path)
    runner = FakeRunner()

    result = detect.detect_services(repo, platform="win32", runner=runner)

    assert result == {"platform": "win32", "rendered": [], "running": None}
    assert runner.calls == []


def test_services_platform_defaults_to_sys_platform(tmp_path, monkeypatch):
    repo, _ = _make_repo(tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    runner = FakeRunner()

    result = detect.detect_services(repo, runner=runner)

    assert result == {"platform": "win32", "rendered": [], "running": None}
    assert runner.calls == []


def test_services_missing_directories_report_empty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = FakeRunner(results={"launchctl": (0, "", "")})

    result = detect.detect_services(repo, platform="darwin", runner=runner)

    assert result["rendered"] == []
    assert result["running"] is False


# --------------------------------------------------------------------------
# collect_state - happy path
# --------------------------------------------------------------------------


def test_collect_state_fully_installed(tmp_path):
    repo, home = _make_repo(tmp_path, agents=("overlord", "software-engineer", "qa-test-engineer"))
    for name in ("overlord", "qa-test-engineer"):
        (home / ".claude" / "agents" / f"{name}.md").write_text(f"# {name}\n")
    (home / ".claude" / "overlord-policy.md").write_text("policy\n")
    runner = FakeRunner(
        results={
            "install_checks.py": (0, json.dumps(CHECKS), ""),
            "import mcp": (0, "", ""),
            "mcp get": (0, "pipeline: connected\n", ""),
            "launchctl": (0, f"1234\t0\t{LABEL}\n", ""),
            "is-active": (0, "active\n", ""),
        }
    )
    which, seen = _which("/usr/local/bin/claude")

    state = detect.collect_state(
        repo, home=home, runner=runner, which=which, environ={}
    )

    assert state["checks"] == CHECKS
    assert state.get("checks_error") is None
    assert Path(state["venv_python"]).resolve() == (repo / ".venv" / "bin" / "python3").resolve()
    assert Path(state["venv_python"]).is_absolute()
    assert state["venv_imports_ok"] is True
    assert state["mcp_registered"] is True
    assert state["personas"] == {
        "installed": ["overlord", "qa-test-engineer"],
        "missing": ["software-engineer"],
    }
    assert state["overlord_policy"] is True
    assert Path(state["registry"]["path"]).resolve() == (repo / "model_registry.json").resolve()
    assert state["registry"]["exists"] is True
    assert state["services"]["platform"] == sys.platform
    assert set(state["services"]) == {"platform", "rendered", "running"}
    assert seen == ["claude"]


def test_collect_state_exact_probe_argvs_and_timeouts(tmp_path):
    repo, home = _make_repo(tmp_path)
    runner = FakeRunner(
        results={
            "install_checks.py": (0, json.dumps(CHECKS), ""),
            "import mcp": (0, "", ""),
            "mcp get": (0, "", ""),
            "launchctl": (0, "", ""),
            "is-active": (0, "", ""),
        }
    )
    which, _ = _which("/usr/local/bin/claude")

    detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    checks_call = runner.find("install_checks.py")[0]
    assert checks_call[0][0] == sys.executable
    assert Path(checks_call[0][1]).resolve() == (repo / "scripts" / "install_checks.py").resolve()
    assert Path(checks_call[0][1]).is_absolute()
    assert checks_call[0][2] == "--json"
    assert len(checks_call[0]) == 3
    assert checks_call[1] == 60

    import_call = runner.find("import mcp")[0]
    assert Path(import_call[0][0]).resolve() == (repo / ".venv" / "bin" / "python3").resolve()
    assert import_call[0][1] == "-c"
    assert import_call[0][2] == "import mcp, httpx, fastapi, uvicorn"
    assert import_call[1] == 30

    mcp_call = runner.find("mcp get")[0]
    assert mcp_call[0][1:] == ["mcp", "get", "pipeline"]
    assert mcp_call[0][0] in ("claude", "/usr/local/bin/claude")

    # No probe may be unbounded.
    assert runner.calls
    for argv, timeout in runner.calls:
        assert isinstance(timeout, (int, float)) and timeout > 0, argv


def test_collect_state_registry_honours_environ(tmp_path):
    repo, home = _make_repo(tmp_path)
    override = _write_registry(tmp_path / "override.json", _registry_payload(("claude", "local", "litellm")))
    runner = FakeRunner(results={"launchctl": (0, "", ""), "is-active": (3, "", "")})
    which, _ = _which(None)

    state = detect.collect_state(
        repo,
        home=home,
        runner=runner,
        which=which,
        environ={"PIPELINE_MODEL_REGISTRY_PATH": str(override)},
    )

    assert Path(state["registry"]["path"]).resolve() == override.resolve()
    assert state["registry"]["provider_count"] == 3


def test_collect_state_venv_imports_failure_is_false(tmp_path):
    repo, home = _make_repo(tmp_path)
    runner = FakeRunner(
        results={"import mcp": (1, "", "ModuleNotFoundError: fastapi"), "launchctl": (0, "", "")}
    )
    which, _ = _which(None)

    state = detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    assert state["venv_imports_ok"] is False


def test_collect_state_mcp_nonzero_is_false(tmp_path):
    repo, home = _make_repo(tmp_path)
    runner = FakeRunner(results={"mcp get": (1, "", "not registered"), "launchctl": (0, "", "")})
    which, _ = _which("/usr/local/bin/claude")

    state = detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    assert state["mcp_registered"] is False


def test_collect_state_mcp_absent_never_invokes_claude(tmp_path):
    repo, home = _make_repo(tmp_path)
    runner = FakeRunner(results={"launchctl": (0, "", "")})
    which, seen = _which(None)

    state = detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    assert state["mcp_registered"] is None
    assert seen == ["claude"]
    assert runner.find("mcp get") == []


def test_collect_state_venv_absent_skips_import_probe(tmp_path):
    repo, home = _make_repo(tmp_path, venv=False)
    runner = FakeRunner(results={"launchctl": (0, "", "")})
    which, _ = _which(None)

    state = detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    assert state["venv_python"] is None
    assert state["venv_imports_ok"] is None
    assert runner.find("import mcp") == []


def test_collect_state_non_executable_venv_python_is_none(tmp_path):
    repo, home = _make_repo(tmp_path)
    (repo / ".venv" / "bin" / "python3").chmod(0o644)
    runner = FakeRunner(results={"launchctl": (0, "", "")})
    which, _ = _which(None)

    state = detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    assert state["venv_python"] is None
    assert state["venv_imports_ok"] is None


def test_collect_state_home_defaults_to_path_home(tmp_path, monkeypatch):
    repo, home = _make_repo(tmp_path)
    (home / ".claude" / "agents" / "overlord.md").write_text("# overlord\n")
    (home / ".claude" / "overlord-policy.md").write_text("policy\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    runner = FakeRunner(results={"launchctl": (0, "", "")})
    which, _ = _which(None)

    state = detect.collect_state(repo, runner=runner, which=which, environ={})

    assert state["personas"]["installed"] == ["overlord"]
    assert state["overlord_policy"] is True


# --------------------------------------------------------------------------
# collect_state - negative / boundary
# --------------------------------------------------------------------------


def test_collect_state_empty_repo_returns_empty_values(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    runner = FakeRunner(results={"launchctl": (0, "", ""), "is-active": (3, "", "")})
    which, _ = _which(None)

    state = detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    assert state["checks"] == []
    assert isinstance(state["checks_error"], str) and state["checks_error"]
    assert state["venv_python"] is None
    assert state["venv_imports_ok"] is None
    assert state["mcp_registered"] is None
    assert state["personas"] == {"installed": [], "missing": []}
    assert state["overlord_policy"] is False
    assert state["registry"]["exists"] is False
    assert state["registry"]["data"] == {}
    assert state["services"]["rendered"] == []
    assert runner.find("import mcp") == []


def test_collect_state_empty_checks_list_is_not_an_error(tmp_path):
    repo, home = _make_repo(tmp_path)
    runner = FakeRunner(results={"install_checks.py": (0, "[]", ""), "launchctl": (0, "", "")})
    which, _ = _which(None)

    state = detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    assert state["checks"] == []
    assert state.get("checks_error") is None


def test_collect_state_zero_personas(tmp_path):
    repo, home = _make_repo(tmp_path, agents=())
    runner = FakeRunner(results={"launchctl": (0, "", "")})
    which, _ = _which(None)

    state = detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    assert state["personas"] == {"installed": [], "missing": []}


def test_collect_state_checks_non_json_reports_json_error(tmp_path):
    repo, home = _make_repo(tmp_path)
    runner = FakeRunner(
        results={"install_checks.py": (0, "Traceback (most recent call last): ...", ""), "launchctl": (0, "", "")}
    )
    which, _ = _which(None)

    state = detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    assert state["checks"] == []
    assert "json" in state["checks_error"].lower()


def test_collect_state_checks_nonzero_exit(tmp_path):
    repo, home = _make_repo(tmp_path)
    runner = FakeRunner(
        results={"install_checks.py": (2, "", "boom"), "launchctl": (0, "", "")}
    )
    which, _ = _which(None)

    state = detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    assert state["checks"] == []
    assert isinstance(state["checks_error"], str) and state["checks_error"]


def test_collect_state_probe_failures_never_raise(tmp_path):
    repo, home = _make_repo(tmp_path)
    runner = FakeRunner(
        raises={
            "install_checks.py": OSError("no interpreter"),
            "import mcp": subprocess.TimeoutExpired("python3", 30),
            "mcp get": OSError("claude vanished"),
            "launchctl": subprocess.TimeoutExpired("launchctl", 10),
            "systemctl": OSError("no systemd"),
        }
    )
    which, _ = _which("/usr/local/bin/claude")

    state = detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    assert state["checks"] == []
    assert isinstance(state["checks_error"], str) and state["checks_error"]
    assert state["venv_imports_ok"] is None
    assert state["mcp_registered"] is None
    assert state["services"]["running"] is None
    # The rest of the snapshot still resolves.
    assert state["venv_python"] is not None
    assert state["registry"]["exists"] is True


def test_collect_state_returns_every_documented_key(tmp_path):
    repo, home = _make_repo(tmp_path)
    runner = FakeRunner(results={"launchctl": (0, "", "")})
    which, _ = _which(None)

    state = detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    for key in (
        "checks",
        "venv_python",
        "venv_imports_ok",
        "mcp_registered",
        "personas",
        "overlord_policy",
        "registry",
        "services",
    ):
        assert key in state, key
    assert set(state["registry"]) >= {"path", "exists", "roles", "provider_count", "data", "error"}


# --------------------------------------------------------------------------
# isolation: the module writes nothing
# --------------------------------------------------------------------------


def _snapshot(root):
    entries = {}
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        entries[str(path)] = (stat.st_mtime_ns, stat.st_size)
    return entries


def test_collect_state_writes_nothing(tmp_path):
    repo, home = _make_repo(tmp_path)
    runner = FakeRunner(
        results={
            "install_checks.py": (0, json.dumps(CHECKS), ""),
            "import mcp": (0, "", ""),
            "mcp get": (0, "", ""),
            "launchctl": (0, f"1234\t0\t{LABEL}\n", ""),
            "is-active": (0, "active\n", ""),
        }
    )
    which, _ = _which("/usr/local/bin/claude")
    before_repo, before_home = _snapshot(repo), _snapshot(home)

    detect.collect_state(repo, home=home, runner=runner, which=which, environ={})

    assert _snapshot(repo) == before_repo
    assert _snapshot(home) == before_home


def test_import_starts_no_process(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("detect.py started a process at import time")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "check_output", _boom)

    importlib.reload(detect)

    assert callable(detect.collect_state)


def test_detect_registry_and_services_write_nothing(tmp_path):
    repo, _ = _make_repo(tmp_path)
    runner = FakeRunner(results={"launchctl": (0, "", "")})
    before = _snapshot(repo)

    detect.detect_registry(repo, environ={})
    detect.detect_services(repo, platform="darwin", runner=runner)

    assert _snapshot(repo) == before
