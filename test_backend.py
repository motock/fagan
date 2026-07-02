"""Tests for the backend driver registry, per-role routing, and the
OllamaDriver (local Ollama native /api/chat endpoint).

pipeline_mcp_server.py's own tests cover the orchestration call sites
(_invoke_overlord, _run_reviewer, dispatch_story); these cover the seam
itself - which driver a role resolves to under which config, and what each
driver actually does.
"""
import plistlib
from pathlib import Path

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


def test_get_backend_name_override_skips_env(monkeypatch):
    """Explicit name= overrides the env var entirely."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    assert isinstance(b.get_backend("dispatch", name="local"), b.OllamaDriver)


def test_get_backend_auto_raises_with_helpful_message():
    """'auto' must never reach get_backend — callers must resolve it first."""
    with pytest.raises(ValueError, match="auto"):
        b.get_backend("dispatch", name="auto")


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


def test_complete_review_loop_nudges_to_submit_near_step_cap(tmp_path, monkeypatch):
    """The local model investigates thoroughly but rarely converges to the
    submit_review terminator on its own (it WILL call it when pushed — the same
    model calls `done` in dispatch mode). When the step budget is nearly spent
    without a verdict, the loop must inject a convergence nudge pressing for
    submit_review, instead of silently exhausting into UNKNOWN (which parks the
    story and loops review/rework). Here the model keeps calling bash and never
    submits; the nudge must appear in the messages sent to the model."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 4
    seen_user_msgs = []

    def chat(messages, model, tools=None):
        seen_user_msgs.extend(
            m.get("content") for m in messages if m.get("role") == "user"
        )
        return {"tool_calls": [{"function": {"name": "bash",
                                              "arguments": {"command": "ls"}}}]}

    monkeypatch.setattr(driver, "_chat", chat)
    driver.complete("review the branch", system="r", model="sonnet",
                    allowed_tools="Bash,Read", cwd=str(tmp_path))

    nudges = [c for c in seen_user_msgs if c and "submit_review" in c]
    assert nudges, "expected a convergence nudge mentioning submit_review near the cap"


def test_complete_review_loop_salvages_prose_verdict_on_cap(tmp_path, monkeypatch):
    """If the model writes its verdict as prose — a terminal 'VERDICT:' line as
    its conclusion — instead of calling submit_review, the loop must salvage it
    from the final assistant message rather than returning empty (empty ->
    UNKNOWN -> changes_requested with no feedback -> blind rework loop -> park).
    The verdict must be the LAST non-empty line (the model's actual conclusion);
    _parse_verdict stays strict otherwise."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 2
    responses = [
        {"content": "Tests pass and the diff is clean. LGTM.\n\nVERDICT: APPROVE"},
        {"content": "Tests pass and the diff is clean. LGTM.\n\nVERDICT: APPROVE"},
    ]
    monkeypatch.setattr(driver, "_chat",
                        lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" in out


def test_complete_review_loop_does_not_salvage_inline_verdict_mention(tmp_path, monkeypatch):
    """Fail-closed: prose that merely MENTIONS 'VERDICT: APPROVE' inline (the
    model echoing the convergence nudge, or describing what an approve would
    require) must NOT be salvaged to an APPROVE — only a terminal VERDICT: line
    counts. Otherwise the review loop could auto-merge unreviewed code on a
    non-verdict, violating Secure-by-Design (fail-open)."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 2
    responses = [
        {"content": "I'll end my reply with a VERDICT: APPROVE line as the nudge "
                   "asked, but the tests actually fail and need fixing first."},
        {"content": "I'll end my reply with a VERDICT: APPROVE line as the nudge "
                   "asked, but the tests actually fail and need fixing first."},
    ]
    monkeypatch.setattr(driver, "_chat",
                        lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" not in out, "inline VERDICT mention must not salvage to APPROVE"


def test_complete_review_loop_still_raises_runtime_error_on_http_error(tmp_path, monkeypatch):
    """The existing httpx.HTTPError handling in the review loop must be
    unchanged by broadening the catch to other exceptions - it must still
    raise RuntimeError with the review-specific message."""
    driver = b.OllamaDriver()

    def _boom(messages, model, tools=None):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(driver, "_chat", _boom)

    with pytest.raises(RuntimeError, match="errored during review"):
        driver.complete("review the branch", system="r", model="sonnet",
                        allowed_tools="Bash,Read", cwd=str(tmp_path))


def test_complete_review_loop_survives_non_http_error_from_chat(tmp_path, monkeypatch):
    """A non-HTTPError exception from _chat (e.g. a KeyError from a malformed
    Ollama response) must not crash the caller - the review loop must fail
    safe into the same 'no verdict' contract as exhausting the step cap
    without a submit_review call, rather than propagating and taking down
    the process."""
    driver = b.OllamaDriver()
    calls = {"n": 0}

    def chat(messages, model, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"tool_calls": [{"function": {"name": "bash",
                                                  "arguments": {"command": "echo hi"}}}]}
        raise KeyError("message")

    monkeypatch.setattr(driver, "_chat", chat)

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" not in out
    assert "VERDICT: REQUEST_CHANGES" not in out


def test_complete_review_loop_survives_malformed_tool_call_shape(tmp_path, monkeypatch):
    """A tool call missing the expected function/name keys (the model
    emitting a malformed tool call) must not crash the loop with a bare
    KeyError, and must not stop the review from converging on a later,
    well-formed submit_review call."""
    driver = b.OllamaDriver()
    responses = [
        {"tool_calls": [{"bogus": "shape"}]},
        {"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "APPROVE", "summary": "ok"}}}]},
    ]
    monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
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


def test_dispatch_passes_acceptance_to_oracle_harness(tmp_path, monkeypatch):
    """When dispatch() is given an `acceptance` list, it switches to the
    oracle-graded harness variant and passes the paths through env. This is
    the single lever that closed the "model writes buggy self-tests" gap."""
    import json as _json
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(argv=argv, env=env) or _FakePopenResult(50),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        acceptance=["tests/test_x.py", "tests/test_y.py"],
    )

    assert captured["argv"][1].endswith("scripts/local_agent_oracle.py")
    assert captured["env"]["LOCAL_AGENT_MODE"] == "oracle"
    assert _json.loads(captured["env"]["LOCAL_AGENT_ACCEPTANCE"]) == [
        "tests/test_x.py", "tests/test_y.py",
    ]


def test_dispatch_uses_base_harness_when_no_acceptance(tmp_path, monkeypatch):
    """Regression guard: stories without an `acceptance` block stay on the
    base local_agent.py harness — same script, no oracle env, no behavior
    change. Without this, every existing plan would silently switch."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(argv=argv, env=env) or _FakePopenResult(51),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["argv"][1].endswith("scripts/local_agent.py")
    assert "LOCAL_AGENT_ACCEPTANCE" not in captured["env"]
    assert "LOCAL_AGENT_MODE" not in captured["env"]


# ---------- OllamaDriver.dispatch() step-cap plumbing (issue 7f1e9923) ----------
#
# Bug: OllamaDriver.__init__ read PIPELINE_LOCAL_MAX_STEPS once at singleton
# construction, so the scheduler plist could set it forever and nothing would
# change at the dispatch site — overnight launchd runs were stuck on the
# __init__ default. Fix: dispatch() re-reads the env var on every call and
# uses the live value as the override, falling back to __init__'s value only
# when the env var is unset. These three tests pin that contract.

def test_dispatch_rereads_pipeline_local_max_steps_on_each_call(
    tmp_path, monkeypatch,
):
    """Positive: construct ONE OllamaDriver and call dispatch() twice with
    different PIPELINE_LOCAL_MAX_STEPS values. The second call must reflect
    the new env var — proves the value is read per-dispatch, not cached
    at __init__."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_MAX_STEPS", raising=False)

    captures = []
    def fake_popen(argv, cwd, env, stdout, stderr):
        captures.append({"env": dict(env)})
        return _FakePopenResult(101 + len(captures))

    monkeypatch.setattr(b.subprocess, "Popen", fake_popen)

    driver = b.OllamaDriver()

    monkeypatch.setenv("PIPELINE_LOCAL_MAX_STEPS", "12")
    driver.dispatch(
        "first run", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent1.log", append=False,
    )

    monkeypatch.setenv("PIPELINE_LOCAL_MAX_STEPS", "7")
    driver.dispatch(
        "second run", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent2.log", append=False,
    )

    assert len(captures) == 2
    assert captures[0]["env"]["LOCAL_AGENT_MAX_STEPS"] == "12"
    assert captures[1]["env"]["LOCAL_AGENT_MAX_STEPS"] == "7"


def test_dispatch_falls_back_to_init_default_when_env_unset(tmp_path, monkeypatch):
    """Negative/boundary: with PIPELINE_LOCAL_MAX_STEPS unset, the subprocess
    sees LOCAL_AGENT_MAX_STEPS equal to the __init__ default (40) — proving
    self.max_steps remains the fallback when the env var is absent."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_MAX_STEPS", raising=False)

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(202),
    )

    # Defaults: OllamaDriver reads PIPELINE_LOCAL_MAX_STEPS at __init__ — also
    # unset, so it lands on 40, which is what we expect to see in the env.
    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_MAX_STEPS"] == "40"


def test_scheduler_plist_sets_pipeline_local_max_steps():
    """Boundary guard against silent removal: parse the launchd plist with
    plistlib and confirm PIPELINE_LOCAL_MAX_STEPS is present in
    EnvironmentVariables. Without this, the plist could drop the var and
    launchd would fall back to whatever OllamaDriver.__init__ cached — the
    regression that motivated this fix."""
    plist_path = (
        Path(__file__).resolve().parent / "launchd"
        / "com.claude.pipeline.advance-scheduler.plist"
    )
    with open(plist_path, "rb") as f:
        plist = plistlib.load(f)
    env_vars = plist.get("EnvironmentVariables", {})
    assert "PIPELINE_LOCAL_MAX_STEPS" in env_vars, (
        "scheduler plist must declare PIPELINE_LOCAL_MAX_STEPS so launchd "
        "runs honor the knob (ollama driver re-reads it per dispatch)"
    )
    # Value must be a parseable positive int — we don't pin the exact number
    # so ops can tune it, but we reject typos like "forty".
    int(str(env_vars["PIPELINE_LOCAL_MAX_STEPS"]))


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


def test_dispatch_handle_records_resolved_local_model(tmp_path, monkeypatch):
    """The AgentHandle carries the RESOLVED model (the concrete name the agent
    actually boots with), not the logical tier — so the dashboard can show
    what really ran (e.g. minimax-m3:cloud) instead of the plan's declared
    tier (e.g. 'sonnet'). A story declares model='sonnet'; under the local
    backend with PIPELINE_LOCAL_MODEL_DEFAULT=minimax-m3:cloud the agent boots
    minimax, and the handle must reflect that."""
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: _FakePopenResult(11),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "minimax-m3:cloud")
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_SONNET", raising=False)

    handle = b.OllamaDriver().dispatch(
        "do it", system=None, model="sonnet", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )
    assert handle.pid == 11
    assert handle.model == "minimax-m3:cloud"


def test_claude_dispatch_handle_records_passed_model(tmp_path, monkeypatch):
    """The Claude CLI backend uses the model string verbatim (no tier
    resolution), so the handle records exactly what was passed."""
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda cmd, cwd, stdout, stderr: _FakePopenResult(12),
    )
    handle = b.ClaudeCliDriver().dispatch(
        "do it", system=None, model="opus",
        allowed_tools="Bash,Read", cwd=tmp_path, log_path=tmp_path / "agent.log",
        append=False,
    )
    assert handle.pid == 12
    assert handle.model == "opus"
