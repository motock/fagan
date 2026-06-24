"""Tests for the backend driver registry, per-role routing, and the
OllamaDriver (local Ollama native /api/chat endpoint).

pipeline_mcp_server.py's own tests cover the orchestration call sites
(_invoke_overlord, _run_reviewer, dispatch_story); these cover the seam
itself - which driver a role resolves to under which config, and what each
driver actually does.
"""
import pytest

import backend as b


# ---------- Role routing ----------
def test_get_backend_no_role_returns_claude_driver():
    assert isinstance(b.get_backend(), b.ClaudeCliDriver)


@pytest.mark.parametrize("role", ["dispatch", "review", "overlord"])
def test_get_backend_defaults_each_role_to_claude(role, monkeypatch):
    monkeypatch.delenv(f"PIPELINE_BACKEND_{role.upper()}", raising=False)
    assert isinstance(b.get_backend(role), b.ClaudeCliDriver)


@pytest.mark.parametrize("role", ["dispatch", "review", "overlord"])
def test_get_backend_honors_explicit_claude_selection(role, monkeypatch):
    monkeypatch.setenv(f"PIPELINE_BACKEND_{role.upper()}", "claude")
    assert isinstance(b.get_backend(role), b.ClaudeCliDriver)


def test_get_backend_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "CLAUDE")
    assert isinstance(b.get_backend("dispatch"), b.ClaudeCliDriver)


def test_get_backend_roles_are_independently_routable(monkeypatch):
    """Selecting a different driver for one role must not affect the others -
    this is the whole point of per-role routing."""
    monkeypatch.setenv("PIPELINE_BACKEND_OVERLORD", "local")
    assert isinstance(b.get_backend("review"), b.ClaudeCliDriver)
    assert isinstance(b.get_backend("dispatch"), b.ClaudeCliDriver)
    assert isinstance(b.get_backend("overlord"), b.OllamaDriver)


def test_get_backend_unknown_driver_name_raises_not_implemented(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "some-typo'd-name")
    with pytest.raises(NotImplementedError, match="PIPELINE_BACKEND_REVIEW"):
        b.get_backend("review")


# ---------- OllamaDriver.complete() ----------
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_complete_posts_to_native_chat_endpoint_and_returns_content(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    captured = {}

    def _fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"message": {"content": "RULING: x"}})

    monkeypatch.setattr(b.httpx, "post", _fake_post)

    driver = b.OllamaDriver()
    result = driver.complete("do the thing", system="be careful", model="opus")

    assert result == "RULING: x"
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["json"]["messages"] == [
        {"role": "system", "content": "be careful"},
        {"role": "user", "content": "do the thing"},
    ]


def test_complete_pins_num_ctx_to_avoid_cpu_gpu_split(monkeypatch):
    """Ollama's default context (131072) made devstral:24b's KV cache blow
    past 24GB unified memory, forcing a CPU/GPU split that timed out a single
    completion at 600s. num_ctx must always be set explicitly."""
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "8192")
    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    b.OllamaDriver().complete("p", model="opus")

    assert captured["options"] == {"num_ctx": 8192}


def test_complete_resolves_tier_to_configured_local_model(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_OPUS", "qwen2.5-coder:32b")
    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    b.OllamaDriver().complete("p", model="opus")

    assert captured["model"] == "qwen2.5-coder:32b"


def test_complete_falls_back_to_devstral_default_for_unmapped_tier(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_SONNET", raising=False)
    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    b.OllamaDriver().complete("p", model="sonnet")

    assert captured["model"] == "devstral:24b"


def test_complete_raises_clear_runtime_error_when_endpoint_unreachable(monkeypatch):
    def _boom(url, json, timeout):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "post", _boom)

    with pytest.raises(RuntimeError, match="unreachable"):
        b.OllamaDriver().complete("p", model="opus")


def test_usage_probe_text_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        b.OllamaDriver().usage_probe_text()


# ---------- OllamaDriver.dispatch() ----------
def test_dispatch_refuses_read_only_allowed_tools():
    """OpenHands has no read-only mode - routing a read-only role (e.g.
    review) here would silently let the agent edit files anyway."""
    with pytest.raises(NotImplementedError, match="read-only"):
        b.OllamaDriver().dispatch(
            "p", model="opus", allowed_tools="Bash,Read",
            cwd=b.Path("."), log_path=b.Path("x.log"), append=False,
        )


def test_dispatch_raises_clear_error_when_settings_not_set_up(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_OPENHANDS_PERSISTENCE_DIR", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="setup_openhands_local"):
        b.OllamaDriver().dispatch(
            "p", model="opus", allowed_tools="Bash,Edit,Write,Read",
            cwd=b.Path("."), log_path=b.Path("x.log"), append=False,
        )


def test_dispatch_allows_default_tools_with_edit_and_write(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_OPENHANDS_PERSISTENCE_DIR", str(tmp_path))
    (tmp_path / "agent_settings.json").write_text("{}")
    captured = {}

    def _fake_popen(argv, cwd, env, stdout, stderr):
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["env"] = env
        return _FakePopenResult(4242)

    monkeypatch.setattr(b.subprocess, "Popen", _fake_popen)
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    handle = b.OllamaDriver().dispatch(
        "fix the bug", system="be careful", model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert handle.pid == 4242
    assert captured["argv"][0] == "openhands"
    assert "--headless" in captured["argv"]
    assert "--override-with-envs" in captured["argv"]
    assert captured["argv"][-2:] == ["-t", "be careful\n\nfix the bug"]
    assert captured["env"]["LLM_MODEL"] == "ollama/devstral:24b"
    assert captured["env"]["LLM_BASE_URL"] == "http://localhost:11434"
    assert captured["env"]["OPENHANDS_PERSISTENCE_DIR"] == str(tmp_path)
    assert (tmp_path / "agent.log").exists()


def test_dispatch_registers_minimal_checkpoint_only_mcp_server(tmp_path, monkeypatch):
    """Without this, an agent told (via the dispatch prompt) to call the
    checkpoint tool finds no such tool, gets confused, and gives up without
    doing any work. It must be the minimal dedicated server, not the full
    pipeline server - the full server's ~18 other tools (approve_merge,
    advance_pipeline, ...) are a privilege-escalation risk for a local model
    and, found via real end-to-end testing, also degraded tool selection."""
    monkeypatch.setenv("PIPELINE_OPENHANDS_PERSISTENCE_DIR", str(tmp_path))
    monkeypatch.setenv("PLAN_DIR", "/some/plan/dir")
    (tmp_path / "agent_settings.json").write_text("{}")
    monkeypatch.setattr(
        b.subprocess, "Popen", lambda *a, **k: _FakePopenResult(1),
    )

    b.OllamaDriver().dispatch(
        "p", model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    config = b.json.loads((tmp_path / "mcp.json").read_text())
    server = config["mcpServers"]["pipeline-checkpoint"]
    assert server["command"].endswith(".venv/bin/python3")
    assert server["args"][0].endswith("scripts/checkpoint_mcp_server.py")
    assert server["env"] == {"PLAN_DIR": "/some/plan/dir"}


class _FakePopenResult:
    def __init__(self, pid):
        self.pid = pid
