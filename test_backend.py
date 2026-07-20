"""Tests for the backend driver registry, per-role routing, and the
OllamaDriver (local Ollama native /api/chat endpoint).

pipeline_mcp_server.py's own tests cover the orchestration call sites
(_invoke_overlord, _run_reviewer, dispatch_story); these cover the seam
itself - which driver a role resolves to under which config, and what each
driver actually does.
"""
import json
import plistlib
from pathlib import Path

import pytest

import backend as b


# _chat was widened to return the full /api/chat envelope (not just the
# message body) so the review loop can pull prompt_eval_count /
# eval_count / total_duration for the per-call token-cost sidecar. Tests
# that mock _chat below still want to express "here is the message the
# model emits next" — _envelope wraps such a message in the minimal
# envelope shape OllamaDriver._review_loop reads, so test bodies don't
# have to spell out `{"message": {"content": ...}}` at every call site.
def _envelope(message_body: dict) -> dict:
    return {"message": message_body, "model": "test"}


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


# ---------- T16: explicit provider names as top-level backend names ----------
@pytest.mark.parametrize("name,provider_cls", [
    ("ollama", "OllamaProvider"),
    ("lmstudio", "LMStudioProvider"),
    ("mlx", "MLXProvider"),
])
def test_get_backend_explicit_provider_name_pins_that_provider(name, provider_cls, monkeypatch):
    # Regardless of PIPELINE_LOCAL_PROVIDER, naming the provider directly as
    # the backend must resolve to exactly that provider - this is what lets
    # PIPELINE_BACKEND_DISPATCH=lmstudio work without also setting
    # PIPELINE_LOCAL_PROVIDER=lmstudio.
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "ollama")
    driver = b.get_backend("dispatch", name=name)
    assert isinstance(driver, b.OllamaDriver)
    assert isinstance(driver.provider, getattr(b.inference_providers, provider_cls))


def test_get_backend_local_alias_still_resolves_via_env(monkeypatch):
    # "local" stays a permanent back-compat alias - it must keep resolving via
    # PIPELINE_LOCAL_PROVIDER exactly as before these explicit names existed
    # (manifests persist "backend": "local" for already-dispatched stories).
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "mlx")
    driver = b.get_backend("dispatch", name="local")
    assert isinstance(driver, b.OllamaDriver)
    assert isinstance(driver.provider, b.inference_providers.MLXProvider)


# ---------- OllamaDriver.complete() ----------
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        # Default to a healthy status; _FakeStatusResponse overrides for
        # the 429 short-circuit path that _chat now checks before
        # raise_for_status().
        self.status_code = 200

    def raise_for_status(self):
        if self.status_code >= 400:
            raise b.httpx.HTTPStatusError(
                f"{self.status_code} simulated", request=None, response=self
            )

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


def test_resolve_local_model_passes_through_a_concrete_tag_unchanged(monkeypatch):
    """A value containing ':' (Ollama's tag separator, e.g. 'devstral:24b')
    is already a concrete model tag, not a tier name ('sonnet'/'opus'/
    'haiku') - it must be returned as-is rather than looked up in
    _LOCAL_TIER_ENV (where it would never match and silently fall back to
    PIPELINE_LOCAL_MODEL_DEFAULT, discarding the caller's explicit choice).
    This is what lets _run_reviewer route review to a different concrete
    model than dispatch's tier resolution would give it."""
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "gpt-oss:20b")
    assert b._resolve_local_model("devstral:24b") == "devstral:24b"


def test_resolve_local_model_provider_scoped_env_var_wins_over_generic(monkeypatch):
    """Two roles on different local providers must not silently share one
    tier->model mapping: PIPELINE_LOCAL_MODEL_MLX_SONNET (provider-scoped)
    must be checked before the provider-agnostic PIPELINE_LOCAL_MODEL_SONNET,
    so an mlx-routed role and an ollama-routed role can independently resolve
    the same tier name to two different concrete models."""
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_SONNET", "devstral:24b")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_MLX_SONNET", "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit")
    assert b._resolve_local_model("sonnet", provider="mlx") == (
        "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"
    )
    assert b._resolve_local_model("sonnet", provider="ollama") == "devstral:24b"


def test_resolve_local_model_falls_back_to_generic_tier_env_when_provider_scoped_unset(
    monkeypatch,
):
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_MLX_SONNET", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_SONNET", "devstral:24b")
    assert b._resolve_local_model("sonnet", provider="mlx") == "devstral:24b"


def test_resolve_local_model_default_provider_arg_is_ollama():
    """Existing callers that don't pass provider= (pre-dating this change)
    must resolve identically to before - the parameter defaults to 'ollama',
    the historical implicit assumption."""
    import inspect
    assert inspect.signature(b._resolve_local_model).parameters["provider"].default == "ollama"


def test_complete_threads_provider_name_into_local_model_resolution(monkeypatch):
    """OllamaDriver(provider_name='mlx').complete() must resolve tiers via
    the mlx-scoped env var, not the ollama-scoped/generic one - regression
    test for the tier-collision bug (both providers sharing one global
    PIPELINE_LOCAL_MODEL_OPUS)."""
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_OPUS", "devstral:24b")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_MLX_OPUS", "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit")
    captured = {}

    class _FakeProvider:
        name = "mlx"

        def chat(self, messages, *, model, **kwargs):
            captured["model"] = model
            return {"message": {"content": "ok"}}

    driver = b.OllamaDriver(provider_name="mlx")
    monkeypatch.setattr(driver, "provider", _FakeProvider())

    driver.complete("p", model="opus")

    assert captured["model"] == "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"


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
        _envelope({"tool_calls": [{"function": {"name": "bash", "arguments": {"command": "echo ran-tests"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "APPROVE", "summary": "clean"}}}]}),
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
    responses = [_envelope({"content": 'Looks good.\n{"verdict": "APPROVE", "summary": "ok"}'})]
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
        return _envelope({"tool_calls": [{"function": {"name": "bash",
                                              "arguments": {"command": "ls"}}}]})

    monkeypatch.setattr(driver, "_chat", chat)
    driver.complete("review the branch", system="r", model="sonnet",
                    allowed_tools="Bash,Read", cwd=str(tmp_path))

    nudges = [c for c in seen_user_msgs if c and "submit_review" in c]
    assert nudges, "expected a convergence nudge mentioning submit_review near the cap"


def test_complete_review_loop_repeats_nudge_every_step_near_cap(tmp_path, monkeypatch):
    """A model that ignores the first convergence nudge and keeps calling
    bash must be re-nudged on EVERY subsequent step within the final window,
    not just once — a single nudge deep in a long agentic transcript is easy
    for a weak local model to lose track of. This is what let gpt-oss exhaust
    the full 20-step review cap in 3 of 5 observed review cycles on
    2026-07-04 (ratelimiter_inspect benchmark) despite the nudge firing once
    at 5-steps-remaining: the model just kept calling view_file/bash past it,
    with nothing reinforcing the instruction for the remaining steps."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 6
    last_msg_had_nudge = []

    def chat(messages, model, tools=None):
        last_msg_had_nudge.append(
            bool(messages) and messages[-1].get("role") == "user"
            and "submit_review" in (messages[-1].get("content") or "")
        )
        return _envelope({"tool_calls": [{"function": {"name": "bash",
                                              "arguments": {"command": "ls"}}}]})

    monkeypatch.setattr(driver, "_chat", chat)
    driver.complete("review the branch", system="r", model="sonnet",
                    allowed_tools="Bash,Read", cwd=str(tmp_path))

    # review_max_steps=6 -> the "few steps left" window covers 5 calls
    # (5 down to 1 remaining). A model that never converges should be
    # re-nudged before most of those, not just the first one.
    nudge_count = sum(last_msg_had_nudge)
    assert nudge_count >= 3, (
        f"expected the nudge to repeat across multiple steps near the cap, "
        f"got {nudge_count} nudged calls: {last_msg_had_nudge}"
    )


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
        _envelope({"content": "Tests pass and the diff is clean. LGTM.\n\nVERDICT: APPROVE"}),
        _envelope({"content": "Tests pass and the diff is clean. LGTM.\n\nVERDICT: APPROVE"}),
    ]
    monkeypatch.setattr(driver, "_chat",
                        lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" in out


def test_review_loop_reads_step_cap_live_from_env(tmp_path, monkeypatch):
    """PIPELINE_LOCAL_REVIEW_MAX_STEPS must take effect on a driver instance
    that already exists, mirroring how PIPELINE_LOCAL_MAX_STEPS is re-read
    live for dispatch (see the dispatch() num_ctx/max_steps re-read comment)
    rather than only being captured once at __init__ time. Without this, a
    plist/env edit to the review cap silently has no effect on a long-lived
    MCP server process (2026-07-07 web-client-epic retro §7 - this was the
    root cause of the observed config-propagation asymmetry)."""
    driver = b.OllamaDriver()
    assert driver.review_max_steps == 20  # constructor default, env unset
    monkeypatch.setenv("PIPELINE_LOCAL_REVIEW_MAX_STEPS", "2")
    call_count = 0

    def chat(messages, model, tools=None):
        nonlocal call_count
        call_count += 1
        return _envelope({"tool_calls": [{"function": {"name": "bash",
                                                        "arguments": {"command": "echo x"}}}]})
    monkeypatch.setattr(driver, "_chat", chat)

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    # A never-ending supply of tool-call responses is available (25+ would be
    # trivial for the mock to serve), so a call_count of 2 can only mean the
    # loop honored the live env override rather than the constructor's
    # captured default of 20.
    assert call_count == 2
    assert out == ""        # step cap exhausted with no verdict to salvage


def test_complete_review_loop_does_not_salvage_inline_verdict_mention(tmp_path, monkeypatch):
    """Fail-closed: prose that merely MENTIONS 'VERDICT: APPROVE' inline (the
    model echoing the convergence nudge, or describing what an approve would
    require) must NOT be salvaged to an APPROVE — only a terminal VERDICT: line
    counts. Otherwise the review loop could auto-merge unreviewed code on a
    non-verdict, violating Secure-by-Design (fail-open)."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 2
    responses = [
        _envelope({"content": "I'll end my reply with a VERDICT: APPROVE line as the nudge "
                   "asked, but the tests actually fail and need fixing first."}),
        _envelope({"content": "I'll end my reply with a VERDICT: APPROVE line as the nudge "
                   "asked, but the tests actually fail and need fixing first."}),
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
            return _envelope({"tool_calls": [{"function": {"name": "bash",
                                                  "arguments": {"command": "echo hi"}}}]})
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
        _envelope({"tool_calls": [{"bogus": "shape"}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "APPROVE", "summary": "ok"}}}]}),
    ]
    monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" in out


# ---------- REQUEST_CHANGES must carry findings (REVIEW-FINDINGS) ----------
def test_complete_review_loop_rejects_findings_less_request_changes_once(tmp_path, monkeypatch):
    """A REQUEST_CHANGES with no summary/pr_body gives the redispatched agent
    nothing to act on. The loop must not accept it the first time - it should
    push back once with a corrective tool-role message, then accept the
    model's follow-up once it actually states the problems."""
    driver = b.OllamaDriver()
    responses = [
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "REQUEST_CHANGES"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "REQUEST_CHANGES",
                                                    "summary": "auth.py:42 missing null check"}}}]}),
    ]
    seen_tool_msgs = []

    def chat(messages, model, tools=None):
        seen_tool_msgs.extend(m.get("content") for m in messages if m.get("role") == "tool")
        return responses.pop(0)

    monkeypatch.setattr(driver, "_chat", chat)

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: REQUEST_CHANGES" in out
    assert "auth.py:42 missing null check" in out
    corrective = [m for m in seen_tool_msgs if m and "file" in m.lower()]
    assert len(corrective) == 1, "expected exactly one corrective nudge for the findings-less verdict"


def test_complete_review_loop_fails_closed_after_two_findings_less_request_changes(tmp_path, monkeypatch):
    """If the model still submits no findings after the corrective nudge, the
    loop must fail closed: return REQUEST_CHANGES with whatever text exists,
    never silently upgrade to APPROVE, and never raise."""
    driver = b.OllamaDriver()
    responses = [
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "REQUEST_CHANGES"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "REQUEST_CHANGES"}}}]}),
    ]
    monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: REQUEST_CHANGES" in out
    assert "APPROVE" not in out
    assert responses == []


def test_complete_review_loop_accepts_request_changes_with_summary_immediately(tmp_path, monkeypatch):
    """REQUEST_CHANGES with a non-empty summary on the first call must pass
    through untouched - no corrective nudge, no extra chat round-trip."""
    driver = b.OllamaDriver()
    calls = {"n": 0}

    def chat(messages, model, tools=None):
        calls["n"] += 1
        return _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                             "arguments": {"verdict": "REQUEST_CHANGES",
                                                           "summary": "tests fail in test_foo.py"}}}]})

    monkeypatch.setattr(driver, "_chat", chat)

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: REQUEST_CHANGES" in out
    assert "tests fail in test_foo.py" in out
    assert calls["n"] == 1


def test_complete_review_loop_accepts_approve_without_summary(tmp_path, monkeypatch):
    """APPROVE needs no findings - an empty summary must not trigger the
    corrective nudge that findings-less REQUEST_CHANGES gets."""
    driver = b.OllamaDriver()
    calls = {"n": 0}

    def chat(messages, model, tools=None):
        calls["n"] += 1
        return _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                             "arguments": {"verdict": "APPROVE"}}}]})

    monkeypatch.setattr(driver, "_chat", chat)

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" in out
    assert calls["n"] == 1


def test_complete_review_loop_findings_nudge_still_returns_verdict_near_step_cap(tmp_path, monkeypatch):
    """The findings nudge must not consume the step budget in a way that
    turns a conclusive review into UNKNOWN: even when it fires on the last
    available steps, a verdict must still come back once the model complies."""
    driver = b.OllamaDriver()
    driver.review_max_steps = 2
    responses = [
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "REQUEST_CHANGES"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "REQUEST_CHANGES",
                                                    "summary": "foo.py:10 off by one"}}}]}),
    ]
    monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: REQUEST_CHANGES" in out
    assert "foo.py:10 off by one" in out


# ---------- review.log transcript persistence (REVIEW-LOG) ----------
def test_review_loop_writes_transcript_with_header_tool_and_verdict(tmp_path, monkeypatch):
    """The review loop must persist a plain-text transcript to review.log in
    the worktree cwd - a cycle header (timestamp + resolved model), each tool
    call, and the terminal verdict - so an inconclusive review is debuggable
    (dispatch's agent.log has no review counterpart today)."""
    driver = b.OllamaDriver()
    responses = [
        _envelope({"tool_calls": [{"function": {"name": "bash", "arguments": {"command": "echo ran-tests"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "APPROVE", "summary": "clean"}}}]}),
    ]
    monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    log_path = tmp_path / "review.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert "review cycle" in content
    assert "model=" in content
    assert "bash" in content
    assert "VERDICT: APPROVE" in content
    assert "VERDICT: APPROVE" in out


def test_review_loop_appends_across_multiple_review_cycles(tmp_path, monkeypatch):
    """Multiple review cycles in the same worktree cwd (a rework loop) must
    accumulate in one review.log, matching agent.log's append idiom - not
    truncate the previous cycle's transcript."""
    driver = b.OllamaDriver()

    def make_responses():
        return [
            _envelope({"tool_calls": [{"function": {"name": "bash", "arguments": {"command": "echo hi"}}}]}),
            _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                          "arguments": {"verdict": "APPROVE", "summary": "clean"}}}]}),
        ]

    for _ in range(2):
        responses = make_responses()
        monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))
        driver.complete("review the branch", system="r", model="sonnet",
                        allowed_tools="Bash,Read", cwd=str(tmp_path))

    content = (tmp_path / "review.log").read_text()
    assert content.count("review cycle") == 2


def test_review_loop_survives_review_log_path_being_a_directory(tmp_path, monkeypatch):
    """Logging must never break the review: if review.log cannot be written
    (here, forced by pre-creating it as a directory so open() raises
    IsADirectoryError, an OSError subclass), the loop must still return its
    verdict and must not raise."""
    (tmp_path / "review.log").mkdir()
    driver = b.OllamaDriver()
    responses = [
        _envelope({"tool_calls": [{"function": {"name": "bash", "arguments": {"command": "echo hi"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "APPROVE", "summary": "clean"}}}]}),
    ]
    monkeypatch.setattr(driver, "_chat", lambda messages, model, tools=None: responses.pop(0))

    out = driver.complete("review the branch", system="r", model="sonnet",
                          allowed_tools="Bash,Read", cwd=str(tmp_path))

    assert "VERDICT: APPROVE" in out


def test_review_loop_truncates_tool_result_in_log_but_not_in_conversation(tmp_path, monkeypatch):
    """Tool results must be truncated to ~2000 chars in review.log only - the
    full text must still reach the model conversation unmodified."""
    driver = b.OllamaDriver()
    long_result = "X" * 3000
    monkeypatch.setattr(b, "_run_readonly_tool", lambda fn, args, cwd: long_result)

    seen_messages = []
    responses = [
        _envelope({"tool_calls": [{"function": {"name": "bash", "arguments": {"command": "echo hi"}}}]}),
        _envelope({"tool_calls": [{"function": {"name": "submit_review",
                                      "arguments": {"verdict": "APPROVE", "summary": "clean"}}}]}),
    ]

    def chat(messages, model, tools=None):
        seen_messages.append(list(messages))
        return responses.pop(0)

    monkeypatch.setattr(driver, "_chat", chat)

    driver.complete("review the branch", system="r", model="sonnet",
                    allowed_tools="Bash,Read", cwd=str(tmp_path))

    tool_messages = [m["content"] for call in seen_messages for m in call
                     if m.get("role") == "tool"]
    assert long_result in tool_messages, "full tool result must reach the model conversation"

    content = (tmp_path / "review.log").read_text()
    assert long_result not in content, "full tool result must not appear unabridged in the log"
    assert "X" * 2000 in content, "truncated tool result must still appear in the log"


def test_complete_stays_single_shot_for_overlord_style_call(monkeypatch):
    """Overlord-style complete() (allowed_tools='Read', no cwd) must NOT enter
    the tool loop — one plain completion, no tools offered."""
    driver = b.OllamaDriver()
    seen_tools = []
    monkeypatch.setattr(
        driver, "_chat",
        lambda messages, model, tools=None: seen_tools.append(tools) or _envelope({"content": "RULING: ROUTINE"}),
    )

    out = driver.complete("adjudicate", system="overlord", model="opus", allowed_tools="Read")

    assert out == "RULING: ROUTINE"
    assert seen_tools == [None]                      # single call, no tools


# ---------- resource_status() (per-backend gate, Step 5) ----------
def test_ollama_resource_status_ok_when_endpoint_reachable(monkeypatch):
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver()
    # T13 (2026-07-12): isolate from live host memory state, same as the
    # httpx mock above isolates from live network state - this test asserts
    # reachability only, not the machine's actual free memory at test time.
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 4096)
    status = driver.resource_status()
    assert status["ok"] is True


def test_ollama_resource_status_not_ok_when_endpoint_unreachable(monkeypatch):
    def _boom(url, timeout):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "get", _boom)
    status = b.OllamaDriver().resource_status()
    assert status["ok"] is False
    assert "unreachable" in status["reason"]


# ---------- T13: free-memory floor gate on resource_status() ----------
def test_ollama_resource_status_not_ok_when_free_memory_below_floor(monkeypatch):
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 512)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")

    status = driver.resource_status()

    assert status["ok"] is False
    assert "insufficient free memory" in status["reason"]


def test_ollama_resource_status_ok_when_free_memory_above_floor(monkeypatch):
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 4096)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")

    status = driver.resource_status()

    assert status["ok"] is True


def test_ollama_resource_status_memory_floor_defaults_to_2048mb(monkeypatch):
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    monkeypatch.delenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", raising=False)
    driver = b.OllamaDriver()

    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 2047)
    assert driver.resource_status()["ok"] is False

    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 2048)
    assert driver.resource_status()["ok"] is True


def test_resource_status_memory_floor_uses_provider_specific_override(monkeypatch):
    # MLX pins one model's full footprint in memory for the server's entire
    # lifetime (no VRAM-swap eviction like Ollama), so free memory settles at
    # a permanently lower steady state once a model is loaded - the generic
    # 2048mb floor (calibrated for Ollama's evictable footprint) never clears
    # again for the life of the server, livelocking dispatch forever. A
    # provider-scoped override lets MLX use a floor suited to its own memory
    # model without weakening Ollama's.
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver(provider_name="mlx")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 1024)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_MLX", "512")

    status = driver.resource_status()

    assert status["ok"] is True


def test_resource_status_memory_floor_falls_back_to_generic_without_override(monkeypatch):
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver(provider_name="mlx")
    monkeypatch.delenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_MLX", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 1024)

    status = driver.resource_status()

    assert status["ok"] is False
    assert "insufficient free memory" in status["reason"]


def test_resource_status_memory_floor_override_is_provider_scoped(monkeypatch):
    # An override set for mlx must not loosen ollama's own floor - each
    # provider's override is read by its own provider name, never globally.
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver(provider_name="ollama")
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_MLX", "128")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 1024)

    status = driver.resource_status()

    assert status["ok"] is False
    assert "insufficient free memory" in status["reason"]


def test_ollama_resource_status_fails_open_when_memory_read_fails(monkeypatch):
    # A vm_stat parse failure (or a platform without vm_stat) must not block
    # dispatch - "can't determine memory" is not the same as "low memory".
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: None)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "999999")  # would fail if checked

    status = driver.resource_status()

    assert status["ok"] is True


def test_ollama_resource_status_reachability_failure_takes_priority_over_memory(monkeypatch):
    # When both the endpoint is unreachable AND memory is below floor, the
    # reason must reflect the (actionable) reachability failure - an
    # unreachable server can't dispatch regardless of memory, so the memory
    # check should never even run.
    def _boom(url, timeout):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "get", _boom)
    driver = b.OllamaDriver()

    def _boom_memory():
        raise AssertionError("memory should not be checked when unreachable")
    monkeypatch.setattr(driver, "_free_memory_mb", _boom_memory)

    status = driver.resource_status()

    assert status["ok"] is False
    assert "unreachable" in status["reason"]


# ---------- T12 (2026-07-13 review): _free_memory_mb counts reclaimable pages ----------
def _vm_stat_output(free=0, inactive=0, purgeable=0, page_size=16384, include_inactive=True, include_purgeable=True):
    lines = [f"Mach Virtual Memory Statistics: (page size of {page_size} bytes)"]
    lines.append(f"Pages free:                                    {free}.")
    lines.append("Pages active:                                 100.")
    if include_inactive:
        lines.append(f"Pages inactive:                               {inactive}.")
    if include_purgeable:
        lines.append(f"Pages purgeable:                               {purgeable}.")
    return "\n".join(lines) + "\n"


class _FakeCompletedProcess:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _fake_vm_stat_run(stdout):
    def _run(cmd, capture_output, text, timeout):
        return _FakeCompletedProcess(stdout)
    return _run


def test_free_memory_mb_sums_free_inactive_and_purgeable_pages(monkeypatch):
    # 64 pages each of free/inactive/purgeable at a 16384-byte page size is
    # 1MB per bucket - strict free-only would read 1MB here; the fix must
    # report 3MB, matching how macOS's own memory-pressure tooling treats
    # inactive/purgeable pages as reclaimable, not scarce.
    stdout = _vm_stat_output(free=64, inactive=64, purgeable=64, page_size=16384)
    monkeypatch.setattr(b.subprocess, "run", _fake_vm_stat_run(stdout))

    assert b.OllamaDriver()._free_memory_mb() == 3


def test_free_memory_mb_defaults_missing_inactive_or_purgeable_to_zero(monkeypatch):
    # An unexpected vm_stat output shape missing one of the optional fields
    # should degrade to treating that term as 0, not abort the whole read -
    # free alone is still a valid (if less generous) answer.
    stdout = _vm_stat_output(free=64, page_size=16384, include_inactive=False, include_purgeable=False)
    monkeypatch.setattr(b.subprocess, "run", _fake_vm_stat_run(stdout))

    assert b.OllamaDriver()._free_memory_mb() == 1


def test_free_memory_mb_still_returns_none_when_free_pages_missing(monkeypatch):
    # "Pages free" is the one field that must be present - without it there's
    # no baseline to report, so the read still fails open as before.
    stdout = "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages active: 100.\n"
    monkeypatch.setattr(b.subprocess, "run", _fake_vm_stat_run(stdout))

    assert b.OllamaDriver()._free_memory_mb() is None


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


# ---------- T1: Claude backend provider-redirect env isolation ----------
# A `claude` subprocess launched with no `env=` kwarg inherits the calling
# process's full environment. If that shell has a 3rd-party-provider
# redirect exported (ANTHROPIC_BASE_URL et al — a real, documented `claude`
# CLI feature), every review/dispatch/overlord call silently rides it while
# the audit trail still claims "backend": "claude". These vars must be
# stripped before every `claude` subprocess call unless explicitly
# re-enabled via PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV.
_PROVIDER_REDIRECT_ENV_SAMPLE = {
    "ANTHROPIC_BASE_URL": "https://evil.example.com",
    "ANTHROPIC_AUTH_TOKEN": "not-a-real-token",
    "ANTHROPIC_API_KEY": "not-a-real-key",
    "ANTHROPIC_MODEL": "some-other-vendor-model",
    "ANTHROPIC_SMALL_FAST_MODEL": "some-other-vendor-model-fast",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
}


def _set_provider_redirect_env(monkeypatch):
    for var, value in _PROVIDER_REDIRECT_ENV_SAMPLE.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("MY_HARMLESS_TEST_VAR", "keep-me")


def test_complete_strips_provider_redirect_env_vars(monkeypatch):
    _set_provider_redirect_env(monkeypatch)
    monkeypatch.delenv("PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV", raising=False)
    captured = {}

    def _fake_run(cmd, cwd, capture_output, text, env=None):
        captured["env"] = env
        return _FakeCompletedProcess(stdout="ok")

    monkeypatch.setattr(b.subprocess, "run", _fake_run)

    b.ClaudeCliDriver().complete("hi", model="sonnet")

    env = captured["env"]
    assert env is not None
    for var in _PROVIDER_REDIRECT_ENV_SAMPLE:
        assert var not in env
    assert env["MY_HARMLESS_TEST_VAR"] == "keep-me"
    assert env["PATH"] == "/usr/bin:/bin"


def test_dispatch_strips_provider_redirect_env_vars(tmp_path, monkeypatch):
    _set_provider_redirect_env(monkeypatch)
    monkeypatch.delenv("PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV", raising=False)
    captured = {}

    def _fake_popen(cmd, cwd, env, stdout, stderr):
        captured["env"] = env
        return _FakePopenResult(42)

    monkeypatch.setattr(b.subprocess, "Popen", _fake_popen)

    b.ClaudeCliDriver().dispatch(
        "implement", system=None, model="sonnet", allowed_tools="Bash",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    env = captured["env"]
    assert env is not None
    for var in _PROVIDER_REDIRECT_ENV_SAMPLE:
        assert var not in env
    assert env["MY_HARMLESS_TEST_VAR"] == "keep-me"


def test_usage_probe_text_strips_provider_redirect_env_vars(monkeypatch):
    _set_provider_redirect_env(monkeypatch)
    monkeypatch.delenv("PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV", raising=False)
    captured = {}

    def _fake_run(cmd, capture_output, text, check, env=None):
        captured["env"] = env
        return _FakeCompletedProcess(stdout=json.dumps({"result": "usage text"}))

    monkeypatch.setattr(b.subprocess, "run", _fake_run)

    b.ClaudeCliDriver().usage_probe_text()

    env = captured["env"]
    assert env is not None
    for var in _PROVIDER_REDIRECT_ENV_SAMPLE:
        assert var not in env


def test_claude_provider_env_allow_escape_hatch_restores_full_inheritance(monkeypatch):
    _set_provider_redirect_env(monkeypatch)
    monkeypatch.setenv("PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV", "1")
    captured = {}

    def _fake_run(cmd, cwd, capture_output, text, env=None):
        captured["env"] = env
        return _FakeCompletedProcess(stdout="ok")

    monkeypatch.setattr(b.subprocess, "run", _fake_run)

    b.ClaudeCliDriver().complete("hi", model="sonnet")

    env = captured["env"]
    for var, value in _PROVIDER_REDIRECT_ENV_SAMPLE.items():
        assert env[var] == value


# ---------- T4: served vs requested model in the audit sidecar ----------
def test_complete_records_served_model_alongside_requested_tier(tmp_path, monkeypatch):
    """The JSON payload's own "model" field is what the CLI actually served -
    distinct from the requested tier string ("sonnet"). Both must land in the
    sidecar so a provider-redirect drift is visible even without T2/T3."""
    payload = {
        "result": "the answer",
        "model": "claude-sonnet-4-5-20260101",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "total_cost_usd": 0.01,
        "duration_ms": 123,
    }
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    b.ClaudeCliDriver().complete(
        "hi", model="sonnet", cell_dir=str(tmp_path),
    )

    lines = (tmp_path / "review_token_costs.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["model"] == "sonnet"
    assert record["served_model"] == "claude-sonnet-4-5-20260101"


def test_record_token_usage_writes_null_served_model_when_absent(tmp_path):
    """Older call sites (or the non-JSON text-output path) don't have a
    served-model value at all - the sidecar write must degrade to null,
    not raise KeyError."""
    b.ClaudeCliDriver().record_token_usage(
        {"input_tokens": 1, "output_tokens": 1, "model": "sonnet"},
        cell_dir=str(tmp_path),
    )

    lines = (tmp_path / "review_token_costs.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["served_model"] is None


# ---------- T2: served model must match the requested tier ----------
def test_complete_raises_provider_identity_mismatch_when_served_model_diverges(
    tmp_path, monkeypatch,
):
    """If the requested tier is "sonnet" but the CLI's own JSON payload
    reports a non-Anthropic model string, a 3rd-party-provider redirect got
    through despite T1 (e.g. PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV set
    intentionally, or a redirect var outside the known set) - this must be a
    loud failure, not a silently wrong review/dispatch."""
    payload = {
        "result": "the answer",
        "model": "mistral-large-2",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    with pytest.raises(b.ProviderIdentityMismatch):
        b.ClaudeCliDriver().complete("hi", model="sonnet", cell_dir=str(tmp_path))


def test_complete_records_usage_even_on_identity_mismatch(tmp_path, monkeypatch):
    """The mismatch must still be visible in the audit sidecar (feeds T4) -
    raising must not skip the record_token_usage() call."""
    payload = {
        "result": "the answer",
        "model": "mistral-large-2",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    with pytest.raises(b.ProviderIdentityMismatch):
        b.ClaudeCliDriver().complete("hi", model="sonnet", cell_dir=str(tmp_path))

    lines = (tmp_path / "review_token_costs.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["served_model"] == "mistral-large-2"


def test_complete_passes_silently_when_served_model_matches_tier_prefix(
    tmp_path, monkeypatch,
):
    payload = {
        "result": "the answer",
        "model": "claude-opus-4-1-20260101",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    result = b.ClaudeCliDriver().complete("hi", model="opus", cell_dir=str(tmp_path))
    assert result == "the answer"


def test_complete_skips_identity_check_when_cell_dir_none(monkeypatch):
    """Callers that don't request structured output (cell_dir=None, e.g. the
    overlord path) never parse the JSON payload at all - no identity check to
    skip, and a non-JSON stdout still passes through unaffected."""
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout="plain text reply, not JSON"
        ),
    )

    result = b.ClaudeCliDriver().complete("hi", model="sonnet")
    assert result == "plain text reply, not JSON"


def test_complete_cell_dir_none_raises_on_api_error_stdout(monkeypatch):
    """Mode 25: on the cell_dir=None path a CLI transport error returned as
    stdout (e.g. 'API Error: Connection closed mid-response...', returncode
    0) must raise, not pass through as if it were valid output - otherwise
    the planner feeds the error string to the executor as its tech-lead
    checklist. _run_planner's fails-open-to-None guard catches the raised
    exception."""
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout="API Error: Connection closed mid-response. Last token:"
        ),
    )
    with pytest.raises(RuntimeError, match="API Error"):
        b.ClaudeCliDriver().complete("hi", model="sonnet")


def test_complete_cell_dir_none_raises_on_nonzero_returncode(monkeypatch):
    """Mode 25: a non-zero returncode on the cell_dir=None path must raise
    even when stdout is empty (the error detail lives in stderr), so a
    failed CLI invocation can never be mistaken for a successful empty
    reply."""
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout="", returncode=1,
        ),
    )
    with pytest.raises(RuntimeError, match="returncode=1"):
        b.ClaudeCliDriver().complete("hi", model="sonnet")


def test_complete_cell_dir_none_passes_clean_text(monkeypatch):
    """Mode 25 regression guard: a normal non-JSON reply with returncode 0
    and no 'API Error:' marker still passes through unchanged - the new
    fail-closed check must not fire on legitimate output."""
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout="the overlord's policy decision text",
        ),
    )
    result = b.ClaudeCliDriver().complete("hi", model="sonnet")
    assert result == "the overlord's policy decision text"


def test_complete_skips_identity_check_for_unrecognized_tier(tmp_path, monkeypatch):
    """A model string outside the known opus/sonnet/haiku tiers has no
    expected prefix to check against - fail open (no crash) rather than
    guessing, matching the documented "only known tiers" scope."""
    payload = {
        "result": "the answer",
        "model": "anything-at-all",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    result = b.ClaudeCliDriver().complete(
        "hi", model="some-custom-tier", cell_dir=str(tmp_path),
    )
    assert result == "the answer"


# ---------- T3: fail-closed identity preflight wired into resource_status() ----------
def test_verify_identity_returns_ok_true_for_genuine_anthropic_response(monkeypatch):
    monkeypatch.setattr(b, "_claude_identity_status", None)
    payload = {"model": "claude-sonnet-4-5-20260101"}
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    result = b.ClaudeCliDriver().verify_identity()

    assert result == {"ok": True, "model": "claude-sonnet-4-5-20260101", "reason": ""}


def test_verify_identity_returns_ok_false_for_non_anthropic_model(monkeypatch):
    monkeypatch.setattr(b, "_claude_identity_status", None)
    payload = {"model": "mistral-large-2"}
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    result = b.ClaudeCliDriver().verify_identity()

    assert result["ok"] is False
    assert "mistral-large-2" in result["reason"]


def test_verify_identity_caches_result_and_does_not_reprobe(monkeypatch):
    monkeypatch.setattr(b, "_claude_identity_status", None)
    calls = []

    def _fake_run(cmd, capture_output, text, env=None):
        calls.append(cmd)
        return _FakeCompletedProcess(stdout=json.dumps({"model": "claude-sonnet-4-5"}))

    monkeypatch.setattr(b.subprocess, "run", _fake_run)

    driver = b.ClaudeCliDriver()
    first = driver.verify_identity()
    second = driver.verify_identity()

    assert first == second
    assert len(calls) == 1


def test_resource_status_reflects_failed_identity_check_same_as_usage_pause(monkeypatch):
    import pipeline_mcp_server as p
    monkeypatch.setattr(p, "_read_usage_state", lambda: {"paused": False})
    monkeypatch.setattr(
        b, "_claude_identity_status",
        {"ok": False, "model": "mistral-large-2",
         "reason": "Claude backend identity check failed: served 'mistral-large-2', expected claude-sonnet-*"},
    )

    status = b.ClaudeCliDriver().resource_status()

    assert status["ok"] is False
    assert "identity" in status["reason"].lower()


def test_resource_status_ok_when_identity_not_yet_checked(monkeypatch):
    """An empty (never-probed) identity cache must fail OPEN, not block every
    role before any preflight has ever run - matches resource_status()'s
    existing fail-open behavior for missing usage state."""
    import pipeline_mcp_server as p
    monkeypatch.setattr(p, "_read_usage_state", lambda: {"paused": False})
    monkeypatch.setattr(b, "_claude_identity_status", None)

    assert b.ClaudeCliDriver().resource_status()["ok"] is True


# ---------- LocalInferenceProvider selection (PIPELINE_LOCAL_PROVIDER) ----------
def test_ollama_driver_defaults_to_ollama_provider(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_PROVIDER", raising=False)
    driver = b.OllamaDriver()
    assert isinstance(driver.provider, b.inference_providers.OllamaProvider)


# ---------- T16: OllamaDriver(provider_name=) explicit pin ----------
def test_ollama_driver_provider_name_overrides_env(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "ollama")
    driver = b.OllamaDriver(provider_name="mlx")
    assert isinstance(driver.provider, b.inference_providers.MLXProvider)


def test_ollama_driver_no_provider_name_still_reads_env(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "lmstudio")
    driver = b.OllamaDriver()
    assert isinstance(driver.provider, b.inference_providers.LMStudioProvider)


# ---------- PIPELINE_LOCAL_ENDPOINT provider-scoped resolution ----------
# Regression coverage for a live-discovered bug: PIPELINE_LOCAL_ENDPOINT is a
# single global env var, so a dispatch role pinned to mlx (endpoint :8080)
# and a review role pinned to ollama (endpoint :11434) running in the SAME
# process silently shared one endpoint - review's reachability probe hit
# MLX's port and 404'd, permanently gating review ("Review backend gated...
# 404 Not Found for url 'http://localhost:8080/api/tags'") even though a
# real Ollama server was listening on :11434 the whole time. Found running a
# live production-shaped benchmark trial (dispatch=mlx, review/overlord/
# planner=ollama).
def test_endpoint_defaults_to_providers_own_default_when_nothing_set(monkeypatch):
    """Regression guard for the latent half of the same bug: with NO env var
    set at all, every provider previously defaulted to Ollama's port
    (11434) regardless of which provider was actually selected - only
    masked in practice because callers always set PIPELINE_LOCAL_ENDPOINT
    explicitly. mlx's own default_endpoint is :8080; that must now win."""
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT_MLX", raising=False)
    driver = b.OllamaDriver(provider_name="mlx")
    assert driver.endpoint == "http://localhost:8080"


def test_endpoint_global_env_var_still_works_when_provider_scoped_unset(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT_MLX", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:9999")
    driver = b.OllamaDriver(provider_name="mlx")
    assert driver.endpoint == "http://localhost:9999"


def test_endpoint_provider_scoped_env_var_wins_over_global(monkeypatch):
    """The actual bug fix: dispatch (mlx) and review (ollama) must resolve
    independent endpoints even when the process-wide PIPELINE_LOCAL_ENDPOINT
    is set for one of them."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:8080")
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", "http://localhost:11434")
    mlx_driver = b.OllamaDriver(provider_name="mlx")
    ollama_driver = b.OllamaDriver(provider_name="ollama")
    assert mlx_driver.endpoint == "http://localhost:8080"
    assert ollama_driver.endpoint == "http://localhost:11434"


class _UnimplementedFakeProvider:
    """Stands in for a not-yet-built provider (all three registered
    providers - ollama/mlx/lmstudio - are real implementations now), so the
    two contracts below can still be regression-tested without depending on
    any specific provider being unimplemented."""

    def chat(self, *a, **k):
        raise NotImplementedError("fake stub provider")

    def reachable(self, endpoint):
        raise NotImplementedError("fake stub provider")


def test_ollama_driver_complete_raises_on_unimplemented_provider(monkeypatch):
    # An unimplemented provider must surface NotImplementedError from
    # complete() (via _chat -> provider.chat), not silently fall back to
    # Ollama or hang.
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "provider", _UnimplementedFakeProvider())
    with pytest.raises(NotImplementedError):
        driver.complete("p", model="opus")


def test_ollama_driver_resource_status_reports_not_ok_for_unimplemented_provider(
    monkeypatch,
):
    # resource_status()'s contract is "never raises, always returns an
    # {ok, reason} dict" - an unimplemented provider must report itself as
    # unavailable, not propagate NotImplementedError out of the gate check.
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "provider", _UnimplementedFakeProvider())
    status = driver.resource_status()
    assert status["ok"] is False
    assert "fake stub provider" in status["reason"]


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


def test_dispatch_passes_local_agent_provider_env_default(tmp_path, monkeypatch):
    """Unset PIPELINE_LOCAL_PROVIDER resolves to "ollama" and dispatch()
    forwards it to the subprocess as LOCAL_AGENT_PROVIDER, so local_agent.py
    can tell which wire protocol to speak without re-deriving it itself."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env)
        or _FakePopenResult(4243),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_PROVIDER", raising=False)

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_PROVIDER"] == "ollama"


def test_dispatch_passes_local_agent_provider_env_lmstudio(tmp_path, monkeypatch):
    """PIPELINE_LOCAL_PROVIDER=lmstudio must reach the dispatch subprocess as
    LOCAL_AGENT_PROVIDER=lmstudio, not silently stay pinned to ollama."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env)
        or _FakePopenResult(4244),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:1234")
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "lmstudio")

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_PROVIDER"] == "lmstudio"


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


def test_dispatch_passes_think_flag_to_subprocess(tmp_path, monkeypatch):
    """PIPELINE_LOCAL_THINK=false must reach the dispatch subprocess as
    LOCAL_AGENT_THINK=false so local_agent.py can pass "think": false to
    Ollama's /api/chat for a Qwen3 hybrid model. Re-read live per dispatch
    (mirroring PIPELINE_LOCAL_MAX_STEPS) so an env edit takes effect without
    restarting the MCP server."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env)
        or _FakePopenResult(4245),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("PIPELINE_LOCAL_THINK", "false")

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_THINK"] == "false"


def test_dispatch_omits_think_flag_when_unset(tmp_path, monkeypatch):
    """Unset PIPELINE_LOCAL_THINK must not inject LOCAL_AGENT_THINK at all —
    non-Qwen3 models (devstral, gpt-oss, qwen3-coder) get an unchanged env and
    local_agent.py omits the `think` key from the request body."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env)
        or _FakePopenResult(4246),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_THINK", raising=False)

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert "LOCAL_AGENT_THINK" not in captured["env"]


def test_dispatch_omits_think_flag_for_invalid_value(tmp_path, monkeypatch):
    """A garbage PIPELINE_LOCAL_THINK value (not "true"/"false") must not
    inject LOCAL_AGENT_THINK — the backend guard mirrors local_agent.py's
    `if THINK in ("true", "false")` so a typo neither silently disables
    reasoning on a model the caller intended to think nor enables it on one
    they didn't. Only the exact tokens opt in."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env)
        or _FakePopenResult(4247),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("PIPELINE_LOCAL_THINK", "yes")

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert "LOCAL_AGENT_THINK" not in captured["env"]


# ---------- Transcript persistence / resume env-var plumbing ----------

def test_dispatch_always_sets_transcript_path_inside_cwd(tmp_path, monkeypatch):
    """Every local dispatch must persist its transcript so a later rework
    can resume it. LOCAL_AGENT_TRANSCRIPT_PATH is always set to a deterministic
    path inside the given cwd (cwd / ".agent_transcript.json") on every
    dispatch call — not just rework ones."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env, cwd=cwd) or _FakePopenResult(4248),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    transcript = captured["env"]["LOCAL_AGENT_TRANSCRIPT_PATH"]
    assert transcript == str(tmp_path / ".agent_transcript.json")


def test_dispatch_passes_resume_transcript_path_when_set(tmp_path, monkeypatch):
    """When resume_transcript_path is given, it must reach the subprocess as
    LOCAL_AGENT_RESUME_TRANSCRIPT_PATH so local_agent.py loads the prior
    transcript instead of cold-starting."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(4249),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    resume_path = tmp_path / "prior_transcript.json"

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        resume_transcript_path=resume_path,
    )

    assert captured["env"]["LOCAL_AGENT_RESUME_TRANSCRIPT_PATH"] == str(resume_path)


def test_dispatch_omits_resume_transcript_path_when_unset(tmp_path, monkeypatch):
    """Without resume_transcript_path (a first/non-rework dispatch),
    LOCAL_AGENT_RESUME_TRANSCRIPT_PATH must not appear in the subprocess env
    at all — no behavior change for a cold-start dispatch."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(4250),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH" not in captured["env"]


def test_dispatch_passes_resume_append_content_when_set(tmp_path, monkeypatch):
    """When resume_append_content is given alongside resume_transcript_path,
    it must reach the subprocess as LOCAL_AGENT_RESUME_APPEND_CONTENT so
    local_agent.py appends it as a user message after loading the transcript."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(4251),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    resume_path = tmp_path / "prior_transcript.json"
    append_content = "The code reviewer REQUESTED CHANGES on your previous attempt. Address this feedback:\nfix the bug"

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        resume_transcript_path=resume_path,
        resume_append_content=append_content,
    )

    assert captured["env"]["LOCAL_AGENT_RESUME_APPEND_CONTENT"] == append_content


def test_dispatch_omits_resume_append_content_when_unset(tmp_path, monkeypatch):
    """Without resume_append_content, LOCAL_AGENT_RESUME_APPEND_CONTENT must
    not appear in the subprocess env — local_agent.py just resumes the
    transcript with no appended user message."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(4252),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    resume_path = tmp_path / "prior_transcript.json"

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        resume_transcript_path=resume_path,
    )

    assert "LOCAL_AGENT_RESUME_APPEND_CONTENT" not in captured["env"]


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


def test_dispatch_passes_rework_full_suite_env(tmp_path, monkeypatch):
    """L1 (REVIEWER_ESCALATION_PLAN.md): rework_full_suite=True must reach the
    agent subprocess as LOCAL_AGENT_REWORK_FULL_SUITE=1 so the harness raises
    the done-bar to full-suite-green on a CI-fail rework round. Absent by
    default so cold-start dispatches keep the oracle-green bar."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(52),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    b.OllamaDriver().dispatch(
        "fix the test", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        acceptance=["tests/test_x.py"],
        rework_full_suite=True,
    )
    assert captured["env"]["LOCAL_AGENT_REWORK_FULL_SUITE"] == "1"


def test_dispatch_omits_rework_full_suite_env_by_default(tmp_path, monkeypatch):
    """Regression guard: a cold-start dispatch (rework_full_suite unset) must
    NOT set LOCAL_AGENT_REWORK_FULL_SUITE, or the full-suite done-bar would
    silently apply to fresh dispatches and change cold-start behavior."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(53),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        acceptance=["tests/test_x.py"],
    )
    assert "LOCAL_AGENT_REWORK_FULL_SUITE" not in captured["env"]


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


# ---------- OllamaDriver num_ctx/temperature per-dispatch/per-chat plumbing (ENV-KNOBS) ----------
#
# Bug: OllamaDriver.__init__ read PIPELINE_LOCAL_NUM_CTX/PIPELINE_LOCAL_TEMPERATURE
# once at construction, so dispatch() and _chat() always wrote the captured
# values into the child env / request body, silently clobbering per-model
# overrides (e.g. tests/benchmark/models.py's gptoss row) set by the invoking
# harness after the driver already existed. Fix: both re-read the env vars on
# every call, mirroring PIPELINE_LOCAL_MAX_STEPS's existing per-dispatch
# pattern above.

def test_dispatch_rereads_num_ctx_and_temperature_on_each_call(tmp_path, monkeypatch):
    """Positive: construct ONE driver, THEN set PIPELINE_LOCAL_NUM_CTX /
    PIPELINE_LOCAL_TEMPERATURE in the env, call dispatch(), and assert the
    child env reflects the post-construction values — proves they are read
    per-dispatch, not cached at __init__."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)

    driver = b.OllamaDriver()

    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.setenv("PIPELINE_LOCAL_TEMPERATURE", "1.0")

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(303),
    )

    driver.dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_NUM_CTX"] == "32768"
    assert captured["env"]["LOCAL_AGENT_TEMPERATURE"] == "1.0"


def test_dispatch_falls_back_to_init_defaults_for_num_ctx_and_temperature(
    tmp_path, monkeypatch,
):
    """Negative/boundary: with the env vars unset, the subprocess sees the
    __init__ defaults (16384 / 0.3)."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(304),
    )

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_NUM_CTX"] == "16384"
    assert captured["env"]["LOCAL_AGENT_TEMPERATURE"] == "0.3"


def test_dispatch_malformed_num_ctx_raises_value_error_like_max_steps(
    tmp_path, monkeypatch,
):
    """Boundary: an empty-string override must fail the same way
    PIPELINE_LOCAL_MAX_STEPS already does today (a bare ValueError from the
    int() conversion) rather than crashing some other, inconsistent way or
    being silently swallowed into a bogus value."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    driver = b.OllamaDriver()
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "")

    with pytest.raises(ValueError):
        driver.dispatch(
            "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
            cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        )


def test_dispatch_malformed_temperature_raises_value_error_like_max_steps(
    tmp_path, monkeypatch,
):
    """Same boundary as above, for PIPELINE_LOCAL_TEMPERATURE."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    driver = b.OllamaDriver()
    monkeypatch.setenv("PIPELINE_LOCAL_TEMPERATURE", "")

    with pytest.raises(ValueError):
        driver.dispatch(
            "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
            cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        )


def test_chat_rereads_num_ctx_and_temperature_on_each_call(monkeypatch):
    """_chat() backs both complete() and the review loop, so it must also
    honor env changes made after construction, not just dispatch()."""
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)

    driver = b.OllamaDriver()

    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.setenv("PIPELINE_LOCAL_TEMPERATURE", "1.0")

    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    driver.complete("p", model="opus")

    assert captured["options"]["num_ctx"] == 32768
    assert captured["options"]["temperature"] == 1.0


def test_chat_falls_back_to_init_defaults_for_num_ctx_and_temperature(monkeypatch):
    """Negative/boundary: with the env vars unset, _chat() sends the
    __init__ defaults (16384 / 0.3)."""
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)

    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    b.OllamaDriver().complete("p", model="opus")

    assert captured["options"]["num_ctx"] == 16384
    assert captured["options"]["temperature"] == 0.3


# ---------- OllamaDriver per-model tuning table (_LOCAL_MODEL_TUNING) ----------
#
# temperature/num_ctx findings from tests/benchmark A/B experiments are tied
# to a specific model tag (e.g. "gpt-oss:20b"), not a global default. Without
# a per-model table, applying a finding means remembering to flip
# PIPELINE_LOCAL_TEMPERATURE/PIPELINE_LOCAL_NUM_CTX every time the active
# local model tag changes — easy to forget, and silently wrong when
# forgotten. Fix: a module-level table keyed by the RESOLVED model tag,
# consulted after the env var (operator override still wins) and before the
# constructor-captured default.

def test_chat_uses_tuned_table_values_when_present_and_no_env_override(monkeypatch):
    """Positive: a model tag present in the tuning table drives num_ctx/
    temperature for _chat(), with no env var set."""
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    monkeypatch.setattr(
        b, "_LOCAL_MODEL_TUNING",
        {"fake-model:1b": {"temperature": 0.5, "num_ctx": 8192}},
    )

    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    driver = b.OllamaDriver()
    driver._chat([{"role": "user", "content": "hi"}], "fake-model:1b")

    assert captured["options"]["num_ctx"] == 8192
    assert captured["options"]["temperature"] == 0.5


def test_dispatch_uses_tuned_table_values_when_present_and_no_env_override(
    tmp_path, monkeypatch,
):
    """Positive: same as above, but through dispatch()'s child-env plumbing.
    model="opus" resolves (via PIPELINE_LOCAL_MODEL_DEFAULT, unset here) to
    the RESOLVED tag "fake-model:1b", which is what the table is keyed on."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "fake-model:1b")
    monkeypatch.setattr(
        b, "_LOCAL_MODEL_TUNING",
        {"fake-model:1b": {"temperature": 0.5, "num_ctx": 8192}},
    )

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(305),
    )

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_NUM_CTX"] == "8192"
    assert captured["env"]["LOCAL_AGENT_TEMPERATURE"] == "0.5"


def test_chat_env_override_wins_over_tuned_table(monkeypatch):
    """Operator override (PIPELINE_LOCAL_TEMPERATURE/NUM_CTX) must win over a
    table entry for the same model tag."""
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.setenv("PIPELINE_LOCAL_TEMPERATURE", "1.0")
    monkeypatch.setattr(
        b, "_LOCAL_MODEL_TUNING",
        {"fake-model:1b": {"temperature": 0.5, "num_ctx": 8192}},
    )

    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    driver = b.OllamaDriver()
    driver._chat([{"role": "user", "content": "hi"}], "fake-model:1b")

    assert captured["options"]["num_ctx"] == 32768
    assert captured["options"]["temperature"] == 1.0


def test_dispatch_env_override_wins_over_tuned_table(tmp_path, monkeypatch):
    """Same override precedence as above, through dispatch()."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.setenv("PIPELINE_LOCAL_TEMPERATURE", "1.0")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "fake-model:1b")
    monkeypatch.setattr(
        b, "_LOCAL_MODEL_TUNING",
        {"fake-model:1b": {"temperature": 0.5, "num_ctx": 8192}},
    )

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(306),
    )

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_NUM_CTX"] == "32768"
    assert captured["env"]["LOCAL_AGENT_TEMPERATURE"] == "1.0"


def test_gptoss_20b_tuned_to_low_temperature_from_ab_experiment():
    """gpt-oss:20b's entry reflects the 2026-07-03 temperature A/B experiment
    (tests/benchmark/_runs/full_20260703_postfix vs temp_tune_20260703):
    temp=1.0 scored 6/15 success (3 zero-code-landed failures); temp=0.3
    scored 9/15 success (1 zero-code-landed failure) with an unchanged
    ground-truth-pass rate (11/15 both). num_ctx stays 32768 (unrelated to
    the temperature finding, kept as already tuned)."""
    assert b._LOCAL_MODEL_TUNING["gpt-oss:20b"] == {
        "temperature": 0.3, "num_ctx": 32768,
    }


def test_chat_falls_back_to_init_defaults_when_model_tag_absent_from_table(
    monkeypatch,
):
    """Regression guard: a model tag with NO entry in the (now non-empty)
    table must still fall back to self.num_ctx/self.temperature exactly as
    before the table existed - only gpt-oss:20b is tuned, "opus" (which
    resolves to the devstral default in this test env) must not be."""
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_OPUS", raising=False)
    assert b._resolve_local_model("opus") not in b._LOCAL_MODEL_TUNING

    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    b.OllamaDriver().complete("p", model="opus")

    assert captured["options"]["num_ctx"] == 16384
    assert captured["options"]["temperature"] == 0.3


def test_dispatch_falls_back_to_init_defaults_when_model_tag_absent_from_table(
    tmp_path, monkeypatch,
):
    """Same regression guard as above, through dispatch()."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_OPUS", raising=False)
    assert b._resolve_local_model("opus") not in b._LOCAL_MODEL_TUNING

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(307),
    )

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_NUM_CTX"] == "16384"
    assert captured["env"]["LOCAL_AGENT_TEMPERATURE"] == "0.3"


def test_chat_partial_table_entry_only_overrides_the_key_present(monkeypatch):
    """Negative/boundary: a table entry need not set both keys. Only
    temperature is tuned here, so num_ctx must still resolve via the
    existing fallback chain (env, then constructor default)."""
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    monkeypatch.setattr(
        b, "_LOCAL_MODEL_TUNING", {"fake-model:1b": {"temperature": 0.5}},
    )

    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    driver = b.OllamaDriver()
    driver._chat([{"role": "user", "content": "hi"}], "fake-model:1b")

    assert captured["options"]["num_ctx"] == 16384
    assert captured["options"]["temperature"] == 0.5


def test_dispatch_partial_table_entry_only_overrides_the_key_present(
    tmp_path, monkeypatch,
):
    """Same partial-entry boundary as above, through dispatch(): only
    num_ctx is tuned, so temperature falls back to the constructor default."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "fake-model:1b")
    monkeypatch.setattr(
        b, "_LOCAL_MODEL_TUNING", {"fake-model:1b": {"num_ctx": 8192}},
    )

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(308),
    )

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_NUM_CTX"] == "8192"
    assert captured["env"]["LOCAL_AGENT_TEMPERATURE"] == "0.3"


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
        lambda cmd, cwd, env, stdout, stderr: captured.update(cmd=cmd) or _FakePopenResult(99),
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
        lambda cmd, cwd, env, stdout, stderr: _FakePopenResult(12),
    )
    handle = b.ClaudeCliDriver().dispatch(
        "do it", system=None, model="opus",
        allowed_tools="Bash,Read", cwd=tmp_path, log_path=tmp_path / "agent.log",
        append=False,
    )
    assert handle.pid == 12
    assert handle.model == "opus"


# ---------- Gap 5: Ollama 429 must surface as RateLimitedError, not a generic HTTPError ----------
class _FakeStatusResponse:
    """A response that surfaces a real status_code so _chat's 429 short-circuit fires."""
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise b.httpx.HTTPStatusError(
                f"{self.status_code} simulated", request=None, response=self
            )


def test_chat_raises_rate_limited_on_429(monkeypatch):
    """_chat must surface a 429 from the chat endpoint as RateLimitedError
    (a RuntimeError subclass) so the orchestrator can route it to deferral.
    It must NOT be wrapped into a generic HTTPError, which would later be
    converted to a bare RuntimeError("unreachable") and misclassified as an
    inconclusive review by review_story."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: _FakeStatusResponse(429),
    )

    driver = b.OllamaDriver()
    with pytest.raises(b.RateLimitedError, match="429"):
        driver._chat([{"role": "user", "content": "hi"}], model="gpt-oss:20b")
    # And it must NOT be the generic httpx error - the whole point of the
    # short-circuit is to keep RateLimitedError distinct from HTTPError.
    assert not isinstance(
        b.RateLimitedError("x"),
        b.httpx.HTTPError,
    )


def test_complete_propagates_rate_limited_not_generic_runtime_error(monkeypatch):
    """A 429 on a single-shot complete() must propagate as RateLimitedError,
    NOT as the generic RuntimeError('unreachable') the callers wrap
    httpx.HTTPError into. The orchestrator distinguishes the two via
    `except backend.RateLimitedError` BEFORE the generic fallback."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: _FakeStatusResponse(429),
    )

    driver = b.OllamaDriver()
    with pytest.raises(b.RateLimitedError):
        driver.complete("p", model="gpt-oss:20b")
    # Negative assertion: must NOT be a plain RuntimeError with the
    # "unreachable" message that the httpx.HTTPError handler would produce.
    try:
        driver.complete("p", model="gpt-oss:20b")
    except b.RateLimitedError as e:
        assert "unreachable" not in str(e)
    except RuntimeError as e:
        pytest.fail(f"got generic RuntimeError({e!r}); expected RateLimitedError")


def test_review_loop_propagates_rate_limited_through_complete(tmp_path, monkeypatch):
    """_review_loop (via complete() in review mode) must let RateLimitedError
    escape — wrapping it in a generic RuntimeError would defeat the
    orchestrator's `except backend.RateLimitedError` branch and route the
    429 into the inconclusive path instead of deferral."""
    driver = b.OllamaDriver()

    def _boom(*args, **kwargs):
        raise b.RateLimitedError("simulated 429")

    monkeypatch.setattr(driver, "_chat", _boom)

    with pytest.raises(b.RateLimitedError):
        driver.complete(
            "review this", system="s", model="gpt-oss:20b",
            allowed_tools="Bash,Read", cwd=str(tmp_path),
        )


def test_non_429_httpx_error_still_wraps_as_generic_runtime_error(monkeypatch):
    """Guard: only 429 must trigger RateLimitedError. A real backend error
    (500, network drop, etc.) should still surface as the generic
    RuntimeError so existing failure paths in the orchestrator continue to
    handle it the same way."""
    def _boom(url, json, timeout):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "post", _boom)
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    with pytest.raises(RuntimeError, match="unreachable"):
        b.OllamaDriver().complete("p", model="gpt-oss:20b")


# ---------- Gap 4: large-diff reviewer guidance ----------
def test_review_loop_preamble_mentions_diff_stat():
    """The local-model review preamble must guide the model to use
    `git diff --stat` first and per-file `git diff -- <file>` for large
    diffs, because bash output is truncated to 3000 chars. Drop this
    guidance and a multi-file change can be silently cut off. Assert by
    inspecting the module source directly (the preamble is a multi-line
    string built inside _review_loop)."""
    module_src = open(b.__file__).read()
    assert "git diff --stat" in module_src, (
        "reviewer preamble must mention `git diff --stat` so the model "
        "scopes large diffs before reading them"
    )
    assert "git diff -- <file>" in module_src, (
        "reviewer preamble must mention per-file `git diff -- <file>` "
        "as the safe way to read a multi-file change past the 3000-char "
        "bash-output cap"
    )
    # Sanity: confirm _run_readonly_tool actually truncates bash output,
    # so the preamble's advice is grounded in real behavior.
    assert "(pr.stdout + pr.stderr)[:3000]" in module_src


def test_claude_reviewer_prompt_mentions_diff_stat():
    """The Claude reviewer's prompt must also tell Claude to scope large
    diffs via `git diff --stat` first — same 3000-char bash cap applies
    on the Claude side, and Claude was shown to be similarly vulnerable
    to silently-truncated diffs on Tier-2 multi-file stories."""
    import importlib
    p = importlib.import_module("pipeline_mcp_server")
    server_src = open(p.__file__).read()
    # The reviewer prompt now lives in pipeline/review.py; check both.
    review = importlib.import_module("pipeline.review")
    review_src = open(review.__file__).read()
    combined = server_src + review_src
    assert "git diff --stat" in combined
    # And it should explicitly warn about the truncation.
    assert "3000 chars" in combined or "truncat" in combined.lower()


def test_run_readonly_tool_truncates_bash_output_at_3000_chars(tmp_path, monkeypatch):
    """Bash output from the reviewer's read-only tool is truncated to 3000
    chars. The reviewer's preamble explicitly tells the model to avoid
    relying on a single `git diff` for large changes because of this cap;
    this test guards the cap itself so a future 'fix' doesn't silently
    inflate it."""
    import subprocess as _sp
    long_output = "x" * 5000

    class _FakeProc:
        stdout = long_output
        stderr = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _FakeProc())

    result = b._run_readonly_tool("bash", {"command": "echo x"}, tmp_path)
    assert len(result) <= 3000
    assert result == "x" * 3000  # the cap is a hard cut, not a smart trim


# ---------- Gap 7: /api/ps loaded-model probe ----------
class _FakePsResponse:
    def __init__(self, models):
        self._payload = {"models": models}
        self.status_code = 200

    def raise_for_status(self):
        if self.status_code >= 400:
            raise b.httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=self
            )

    def json(self):
        return self._payload


def test_ollama_loaded_models_parses_api_ps(monkeypatch):
    """_ollama_loaded_models must return the set of `name` fields from
    /api/ps so dispatch_story can compare against the about-to-load model."""
    monkeypatch.setattr(
        b.httpx, "get",
        lambda url, timeout: _FakePsResponse([
            {"name": "gpt-oss:20b", "size_vram": 12000000000},
            {"name": "devstral:24b", "size_vram": 14000000000},
        ]),
    )

    loaded = b._ollama_loaded_models("http://localhost:11434")
    assert loaded == {"gpt-oss:20b", "devstral:24b"}


def test_ollama_loaded_models_returns_empty_set_on_http_error(monkeypatch):
    """Observability hook, never a gate: on any failure (endpoint down,
    timeout, malformed JSON), the helper must return an empty set so the
    caller skips the mismatch warning rather than crashing dispatch."""
    def _boom(url, timeout):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "get", _boom)

    assert b._ollama_loaded_models("http://localhost:11434") == set()


def test_ollama_loaded_models_handles_missing_models_field(monkeypatch):
    """Some Ollama versions / proxies return an empty payload; we must
    not crash on that."""
    monkeypatch.setattr(
        b.httpx, "get",
        lambda url, timeout: _FakePsResponse([]),
    )
    assert b._ollama_loaded_models("http://localhost:11434") == set()
