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
    src = open(p.__file__).read()
    assert "git diff --stat" in src
    # And it should explicitly warn about the truncation.
    assert "3000 chars" in src or "truncat" in src.lower()


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
