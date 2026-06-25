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

    assert captured["options"]["num_ctx"] == 8192


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


# ---------- complete() review mode (Bash + cwd -> read-only tool loop) ----------
def test_complete_runs_readonly_review_loop_when_bash_and_cwd(tmp_path, monkeypatch):
    """A review call (allowed_tools includes Bash + a worktree cwd) must run a
    tool loop — run tests / read files — then return text with a VERDICT, not
    a single-shot hallucinated verdict."""
    driver = b.OllamaDriver()
    responses = [
        {"tool_calls": [{"function": {"name": "bash", "arguments": {"command": "echo ran-tests"}}}]},
        {"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "APPROVE", "summary": "clean"}}}]},
    ]
    tools_each_call = []
    monkeypatch.setattr(
        driver, "_chat",
        lambda messages, model, tools=None: tools_each_call.append(tools) or responses.pop(0),
    )

    out = driver.complete("review the branch", system="reviewer body",
                          model="sonnet", allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" in out
    assert tools_each_call[0] is not None          # the loop offered tools
    assert responses == []                          # consumed tool turn + submit_review


def test_complete_review_loop_recovers_unnamed_tool_call(tmp_path, monkeypatch):
    """devstral sometimes emits a bare args object with no tool name; the loop
    must infer the tool from its keys (here: verdict -> submit_review)."""
    driver = b.OllamaDriver()
    responses = [{"content": 'Looks good.\n{"verdict": "APPROVE", "summary": "ok"}'}]
    monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" in out


def test_complete_stays_single_shot_for_overlord_style_call(monkeypatch):
    """Overlord-style complete() (allowed_tools='Read', no cwd) must NOT enter
    the tool loop — one plain completion, no tools offered."""
    driver = b.OllamaDriver()
    seen_tools = []
    monkeypatch.setattr(
        driver, "_chat",
        lambda messages, model, tools=None: seen_tools.append(tools) or {"content": "RULING: ROUTINE"},
    )

    out = driver.complete("adjudicate", system="overlord", model="opus", allowed_tools="Read")

    assert out == "RULING: ROUTINE"
    assert seen_tools == [None]                      # single call, no tools


# ---------- resource_status() (per-backend gate, Step 5) ----------
def test_ollama_resource_status_ok_when_endpoint_reachable(monkeypatch):
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    status = b.OllamaDriver().resource_status()
    assert status["ok"] is True


def test_ollama_resource_status_not_ok_when_endpoint_unreachable(monkeypatch):
    def _boom(url, timeout):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "get", _boom)
    status = b.OllamaDriver().resource_status()
    assert status["ok"] is False
    assert "unreachable" in status["reason"]


def test_claude_resource_status_reflects_usage_paused_flag(monkeypatch):
    import pipeline_mcp_server as p
    monkeypatch.setattr(p, "_read_usage_state", lambda: {"paused": True})
    assert b.ClaudeCliDriver().resource_status()["ok"] is False
    monkeypatch.setattr(p, "_read_usage_state", lambda: {"paused": False})
    assert b.ClaudeCliDriver().resource_status()["ok"] is True


def test_claude_resource_status_fails_open_when_no_usage_state(monkeypatch):
    import pipeline_mcp_server as p
    monkeypatch.setattr(p, "_read_usage_state", lambda: {})
    assert b.ClaudeCliDriver().resource_status()["ok"] is True


# ---------- OllamaDriver.dispatch() ----------
def test_dispatch_refuses_read_only_allowed_tools():
    """The local agent loop is a writing/coding harness - routing a read-only
    role (e.g. review) here would silently let the agent edit files anyway."""
    with pytest.raises(NotImplementedError, match="read-only"):
        b.OllamaDriver().dispatch(
            "p", model="opus", allowed_tools="Bash,Read",
            cwd=b.Path("."), log_path=b.Path("x.log"), append=False,
        )


def test_dispatch_launches_local_agent_subprocess(tmp_path, monkeypatch):
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
    # Runs the standalone agent loop with this project's venv python.
    assert captured["argv"][0].endswith(".venv/bin/python3")
    assert captured["argv"][1].endswith("scripts/local_agent.py")
    assert captured["cwd"] == tmp_path
    # Config goes through the environment; system and prompt stay separate.
    assert captured["env"]["LOCAL_AGENT_MODEL"] == "devstral:24b"
    assert captured["env"]["LOCAL_AGENT_SYSTEM"] == "be careful"
    assert captured["env"]["LOCAL_AGENT_TASK"] == "fix the bug"
    assert captured["env"]["LOCAL_AGENT_ENDPOINT"] == "http://localhost:11434"
    assert (tmp_path / "agent.log").exists()


def test_dispatch_resolves_model_tier_and_passes_runtime_knobs(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env, argv=argv)
        or _FakePopenResult(7),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_SONNET", "qwen2.5-coder:14b")
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "8192")
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_STEPS", "12")

    b.OllamaDriver().dispatch(
        "do it", system=None, model="sonnet", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    # Logical tier resolves to a concrete local model, knobs pass through.
    assert captured["env"]["LOCAL_AGENT_MODEL"] == "qwen2.5-coder:14b"
    assert captured["env"]["LOCAL_AGENT_SYSTEM"] == ""
    assert captured["env"]["LOCAL_AGENT_NUM_CTX"] == "8192"
    assert captured["env"]["LOCAL_AGENT_MAX_STEPS"] == "12"


# ---------- ClaudeCliDriver.dispatch() ----------
def test_dispatch_streams_claude_cli_output_so_log_size_is_a_reliable_signal(
    tmp_path, monkeypatch,
):
    """check_story_status treats a 0-byte agent.log (after the process exits)
    as a failed launch. claude -p's default text output format only writes
    once, at the very end, so a long-running-but-legitimate agent looks
    identical to a launch that never produced anything. --output-format
    stream-json --verbose makes the CLI emit an event immediately on
    startup, so 0 bytes after exit reliably means it never even started."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda cmd, cwd, stdout, stderr: captured.update(cmd=cmd) or _FakePopenResult(99),
    )

    b.ClaudeCliDriver().dispatch(
        "implement the story", system="be careful", model="sonnet",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    cmd = captured["cmd"]
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd


class _FakePopenResult:
    def __init__(self, pid):
        self.pid = pid
