"""Tests for the local dispatch agent loop (scripts/local_agent.py): chat() streaming/retry against a persistent 5xx, 5xx escalation, provider routing (LOCAL_AGENT_PROVIDER), wall-clock timeout, and the reasoning-model 'thinking' field.

Split out of test_local_agent.py to keep it under the project's line-count target; shared fixtures/helpers (including the loaded `la` module itself) moved to tests.unit._local_agent_test_helpers.
"""
import json
import subprocess

import httpx
import pytest

from tests.unit._local_agent_test_helpers import (
    _FakeStreamCM,
    _FakeStreamResponse,
    _init_git_repo,
    _sequence_chat,
    _status_error,
    la,
)


def test_chat_uses_stream_one_turn_when_provider_is_ollama(monkeypatch):
    """The default (ollama) branch must never touch the provider seam."""
    monkeypatch.setattr(la, "PROVIDER", "ollama")

    def _boom(messages):
        raise AssertionError("_provider_chat_turn must not be called for ollama")

    monkeypatch.setattr(la, "_provider_chat_turn", _boom)
    monkeypatch.setattr(
        la, "_stream_one_turn",
        lambda payload: {"role": "assistant", "content": "ok"},
    )
    msg = la.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "ok"


def test_chat_routes_through_provider_when_not_ollama(monkeypatch):
    """LOCAL_AGENT_PROVIDER=lmstudio (or mlx) must skip Ollama's streaming
    /api/chat path entirely and use the provider's blocking chat() instead."""
    monkeypatch.setattr(la, "PROVIDER", "lmstudio")

    def _boom(payload):
        raise AssertionError("_stream_one_turn must not be called for lmstudio")

    monkeypatch.setattr(la, "_stream_one_turn", _boom)
    monkeypatch.setattr(
        la, "_provider_chat_turn",
        lambda messages: {"role": "assistant", "content": "from lmstudio",
                           "tool_calls": [{"function": {"name": "done", "arguments": "{}"}}]},
    )
    msg = la.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "from lmstudio"
    assert msg["tool_calls"][0]["function"]["name"] == "done"


def test_provider_chat_turn_extracts_message_from_envelope(monkeypatch):
    """_provider_chat_turn must return just the message dict (matching
    _stream_one_turn's return contract), not the full provider envelope."""
    captured = {}

    class _FakeProvider:
        def chat(self, messages, *, model, num_ctx, temperature, tools, endpoint, timeout):
            captured.update(model=model, num_ctx=num_ctx, temperature=temperature,
                             tools=tools, endpoint=endpoint, timeout=timeout)
            return {"message": {"role": "assistant", "content": "hi"},
                    "prompt_eval_count": 3, "eval_count": 5}

    monkeypatch.setattr(la.inference_providers, "get_local_provider", lambda: _FakeProvider())
    msg = la._provider_chat_turn([{"role": "user", "content": "hi"}])
    assert msg == {"role": "assistant", "content": "hi"}
    assert captured["model"] == la.MODEL
    assert captured["tools"] == la.TOOLS


def test_chat_retries_on_provider_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(la, "PROVIDER", "lmstudio")
    monkeypatch.setattr(la.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _flaky_5xx(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(503)
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(la, "_provider_chat_turn", _flaky_5xx)
    msg = la.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2
    assert msg["content"] == "ok"


def test_chat_does_not_retry_on_provider_4xx(monkeypatch):
    monkeypatch.setattr(la, "PROVIDER", "lmstudio")
    monkeypatch.setattr(la.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _bad_request(messages):
        calls["n"] += 1
        raise _status_error(400)

    monkeypatch.setattr(la, "_provider_chat_turn", _bad_request)
    try:
        la.chat([{"role": "user", "content": "hi"}])
        assert False, "expected HTTPStatusError(400)"
    except httpx.HTTPStatusError:
        pass
    assert calls["n"] == 1, "4xx must NOT be retried"


def test_chat_retries_on_provider_rate_limited_error_then_succeeds(monkeypatch):
    """A 429 from the local server (RateLimitedError) is transient — chat()
    must retry it like a 5xx, not treat it as a terminal failure."""
    monkeypatch.setattr(la, "PROVIDER", "mlx")
    monkeypatch.setattr(la.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _rate_limited_then_ok(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise la.inference_providers.RateLimitedError("429")
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(la, "_provider_chat_turn", _rate_limited_then_ok)
    msg = la.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2
    assert msg["content"] == "ok"


# ---------- Wall-clock timeout ----------

def test_main_exits_with_wip_commit_when_wall_clock_exceeded(tmp_path, monkeypatch):
    """When wall-clock timeout is exceeded between steps, main() must auto-WIP-commit and return 2."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "TIMEOUT", 0.0)  # expire immediately
    monkeypatch.setattr(la, "MAX_STEPS", 100)

    # Create a dirty worktree file so auto-WIP-commit has something to commit.
    (tmp_path / ".git").mkdir()
    (tmp_path / "work.txt").write_text("in progress")

    git_calls = []

    def fake_git_run(cmd, **kwargs):
        git_calls.append(cmd)
        class R:
            returncode = 0
            stdout = "mocked"
            stderr = ""
        return R()

    def fake_chat(messages):
        # A valid tool call reply so the loop advances at least one step.
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "done", "arguments": {"result": "x"}}}],
        }

    monkeypatch.setattr(la.subprocess, "run", fake_git_run)
    monkeypatch.setattr(la, "chat", fake_chat)
    monkeypatch.setattr(la, "worktree_dirty", lambda: True)
    monkeypatch.setattr(la, "auto_wip_commit", lambda reason: git_calls.append(["wip", reason]))

    # patch time.monotonic so the first check already sees elapsed > TIMEOUT
    call_count = [0]
    def fake_monotonic():
        call_count[0] += 1
        if call_count[0] == 1:
            return 0.0   # start time
        return 1000.0    # way past timeout
    monkeypatch.setattr(la.time, "monotonic", fake_monotonic)

    result = la.main()
    assert result == 2, "wall-clock timeout should exit with code 2 (same as step cap)"
    assert any("wip" in str(c) for c in git_calls), "auto-WIP-commit should run on timeout"


def test_stream_one_turn_assembles_streamed_chunks(monkeypatch):
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "I'll "}}),
        json.dumps({"message": {"role": "assistant", "content": "create a file."}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    msg = la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["role"] == "assistant"
    assert msg["content"] == "I'll create a file."


# ---------- reasoning-model 'thinking' field (gpt-oss:20b onboarding) ----------
# gpt-oss:20b streams its chain-of-thought in a separate `thinking` field on
# each chunk, distinct from `content`. If a turn's `content` is empty on every
# chunk (the model only "thought" and never emitted a final answer/tool call
# as content), the assembled message would otherwise have empty content —
# starving recover_tool_calls() of anything to parse. `thinking` must be used
# ONLY as a content fallback for a turn with zero real content; it must never
# be appended to genuine content (that would leak raw reasoning traces into
# tool-call parsing, commit messages, and logs).

def test_stream_one_turn_falls_back_to_thinking_when_content_entirely_empty(monkeypatch):
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "", "thinking": "Let me "}}),
        json.dumps({"message": {"role": "assistant", "content": "", "thinking": "think about this."}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    msg = la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["content"] == "Let me think about this."


def test_stream_one_turn_does_not_leak_thinking_into_real_content(monkeypatch):
    """When content IS present anywhere in the turn, thinking fragments (even
    ones interleaved on the same chunks) must never be appended to it."""
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "", "thinking": "pondering "}}),
        json.dumps({"message": {"role": "assistant", "content": "Hello ", "thinking": "more thoughts "}}),
        json.dumps({"message": {"role": "assistant", "content": "world.", "thinking": ""}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    msg = la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["content"] == "Hello world."


def test_stream_one_turn_native_tool_call_with_thinking_and_empty_content(monkeypatch):
    """Native tool_calls plus thinking-only turns (no real content) must still
    capture tool_calls correctly, and the thinking-fallback must not interfere
    with or duplicate the tool call."""
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "", "thinking": "figuring out the call "}}),
        json.dumps({
            "message": {
                "role": "assistant", "content": "", "thinking": "now calling.",
                "tool_calls": [{"function": {"name": "bash", "arguments": {"command": "ls"}}}],
            },
        }),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    msg = la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["tool_calls"] == [{"function": {"name": "bash", "arguments": {"command": "ls"}}}]
    assert msg["content"] == "figuring out the call now calling."


def test_stream_one_turn_no_thinking_key_is_noop(monkeypatch):
    """Existing-behavior regression: a plain devstral-style stream (text-only
    content, no `thinking` key at all in any chunk) must assemble identically
    to today's behavior — the absence of `thinking` must be a no-op."""
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "I'll "}}),
        json.dumps({"message": {"role": "assistant", "content": "create a file."}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    msg = la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["role"] == "assistant"
    assert msg["content"] == "I'll create a file."
    assert "tool_calls" not in msg


def test_stream_one_turn_uses_configurable_connect_timeout(monkeypatch):
    """connect=10.0 was hardcoded and too short for a cold local-model load -
    observed live 2026-07-13: both glm-4.7-flash (~17s cold load) and
    qwen3-coder:30b timed out identically. Ollama aborts the in-flight load
    and frees the memory it had claimed the instant the client gives up, so
    a too-short connect timeout causes an infinite load/abort/retry cycle
    that never completes rather than a genuine failure. Mirrors the same
    fix in local_agent_oracle.py (CONNECT_TIMEOUT_SECONDS)."""
    monkeypatch.setattr(la, "CONNECT_TIMEOUT_SECONDS", 45.0)
    captured = {}

    def _fake_stream(method, url, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        lines = [json.dumps({"message": {"role": "assistant", "content": "hi"}, "done": True})]
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert captured["timeout"].connect == 45.0


# ---------- live chars/token calibration from ollama's own prompt_eval_count
# (2026-07-29) ----------
# _CHARS_PER_TOKEN_ESTIMATE=4 is a guess with no tokenizer behind it. Live on
# the ollama server log: a real 41,921-token gpt-oss prompt measured ~2.35
# chars/token against a budget computed from the fixed 4.0 guess - the
# budget was already ~28% over NUM_CTX by the time a trim would fire. Ollama
# reports the real prompt token count for every turn in the streamed done
# chunk's `prompt_eval_count`; use it to replace the guess with a live ratio.

def test_stream_one_turn_captures_prompt_eval_count_and_calibrates_ratio(monkeypatch):
    monkeypatch.setattr(la, "_measured_chars_per_token", None)
    monkeypatch.setattr(la, "_last_prompt_eval_count", None)
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "hi"}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                    "prompt_eval_count": 100}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    payload = {"model": "x", "messages": [{"role": "user", "content": "x" * 250}],
               "tools": [], "stream": True}
    la._stream_one_turn(payload)
    assert la._last_prompt_eval_count == 100
    # 250 message chars + len("[]") for the empty tools schema, over 100
    # measured prompt tokens (see the tools-schema test below).
    assert la._measured_chars_per_token == pytest.approx((250 + 2) / 100)


def test_stream_one_turn_leaves_calibration_unset_without_prompt_eval_count(monkeypatch):
    """A done chunk that never reports a count (any non-ollama backend that
    happened to route here, or a truncated stream) must not crash and must
    not fabricate a calibration - the caller falls back to the fixed guess."""
    monkeypatch.setattr(la, "_measured_chars_per_token", None)
    monkeypatch.setattr(la, "_last_prompt_eval_count", None)
    lines = [json.dumps({"message": {"role": "assistant", "content": "hi"}, "done": True})]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    la._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert la._last_prompt_eval_count is None
    assert la._measured_chars_per_token is None


def test_stream_one_turn_counts_tools_schema_in_calibration(monkeypatch):
    """prompt_eval_count covers the WHOLE prompt - messages plus the tools
    schema plus chat-template scaffolding - so calibrating against message
    chars alone biases the ratio badly low early in a run, when the ~3.4KB
    tools schema dominates a still-small transcript. Measured: a first turn
    calibrated to 0.67 instead of ~2.35, shrinking the reactive-5xx trim
    budget ~3.5x more than needed. The tools payload must be counted."""
    monkeypatch.setattr(la, "_measured_chars_per_token", None)
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "hi"}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                    "prompt_eval_count": 100}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(la.httpx, "stream", _fake_stream)
    tools = [{"type": "function", "function": {"name": "x" * 146}}]
    tools_chars = len(json.dumps(tools))
    payload = {"model": "x", "messages": [{"role": "user", "content": "y" * 250}],
               "tools": tools, "stream": True}
    la._stream_one_turn(payload)
    assert la._measured_chars_per_token == pytest.approx((250 + tools_chars) / 100)


def test_effective_chars_per_token_prefers_calibrated_value(monkeypatch):
    monkeypatch.setattr(la, "_measured_chars_per_token", 2.35)
    assert la._effective_chars_per_token() == 2.35


def test_effective_chars_per_token_falls_back_to_estimate(monkeypatch):
    monkeypatch.setattr(la, "_measured_chars_per_token", None)
    assert la._effective_chars_per_token() == la._CHARS_PER_TOKEN_ESTIMATE


def test_main_resets_calibration_globals_at_start(tmp_path, monkeypatch):
    """A stale calibration from a prior dispatch (or, in-process, a prior
    test) must never leak into a fresh run - main() starts with no
    measurement, matching a cold agent process."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "_measured_chars_per_token", 1.5)
    monkeypatch.setattr(la, "_last_prompt_eval_count", 99999)
    monkeypatch.setattr(la, "MAX_STEPS", 1)
    observed = {}

    def _fake_chat(messages):
        observed["last_prompt_eval_count"] = la._last_prompt_eval_count
        observed["measured_chars_per_token"] = la._measured_chars_per_token
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(la, "chat", _fake_chat)
    la.main()
    assert observed["last_prompt_eval_count"] is None
    assert observed["measured_chars_per_token"] is None


def test_main_proactively_trims_when_measured_tokens_near_num_ctx(tmp_path, monkeypatch, capsys):
    """Reacting to a 500 with a bad char estimate is too late: once ollama's
    own measured prompt_eval_count for a turn is already close to NUM_CTX,
    trim the transcript BEFORE the next turn instead of waiting for a request
    to fail first."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(la, "NUM_CTX", 100)
    monkeypatch.setattr(la, "PROACTIVE_TRIM_THRESHOLD", 0.85)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        if calls["n"] == 3:
            # Stand in for _stream_one_turn's calibration side effect. Set via
            # monkeypatch, not raw assignment, so these module globals are
            # restored at teardown instead of leaking into later tests.
            monkeypatch.setattr(la, "_last_prompt_eval_count", 90)
            monkeypatch.setattr(la, "_measured_chars_per_token", 1.0)
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(la, "chat", _fake_chat)
    rc = la.main()
    out = capsys.readouterr().out
    assert rc == 0, f"expected the run to finish cleanly, got rc={rc}\noutput: {out!r}"
    assert "trimming proactively" in out, f"expected a proactive-trim log line, output: {out!r}"


# --- Qwen3 hybrid thinking-mode control (PIPELINE_LOCAL_THINK / LOCAL_AGENT_THINK) ---
#
# Qwen3.6-27B (and other Qwen3 dense models) emit a  Mattis... Mattis reasoning
# block by default. In the tool-calling loop that breaks dispatch: the block
# lands in `content` with no native tool_calls and the driver spins "no tool
# call" forever. Ollama's /api/chat accepts a top-level "think": false to
# suppress the block at the source so the model emits a clean native tool call.
# The flag is opt-in via LOCAL_AGENT_THINK ("false"/"true"); omitted entirely
# when unset so non-Qwen3 models (devstral, gpt-oss, qwen3-coder) get an
# unchanged request body.

def test_ollama_payload_omits_think_by_default(monkeypatch):
    """Unset LOCAL_AGENT_THINK must not add a `think` key — non-Qwen3 models
    get an unchanged /api/chat request."""
    monkeypatch.setattr(la, "THINK", "")
    p = la._ollama_payload([{"role": "user", "content": "hi"}])
    assert "think" not in p
    # The rest of the payload is intact.
    assert p["model"] == la.MODEL
    assert p["stream"] is True
    assert p["options"]["num_ctx"] == la.NUM_CTX
    assert p["options"]["temperature"] == la.TEMPERATURE


def test_ollama_payload_think_false_suppresses_qwen3_reasoning(monkeypatch):
    """LOCAL_AGENT_THINK=false adds "think": False so a Qwen3 hybrid model
    skips its  Mattis block and emits a clean native tool call."""
    monkeypatch.setattr(la, "THINK", "false")
    p = la._ollama_payload([{"role": "user", "content": "hi"}])
    assert p["think"] is False


def test_ollama_payload_think_true_when_explicitly_enabled(monkeypatch):
    """LOCAL_AGENT_THINK=true is honored for runs that DO want reasoning."""
    monkeypatch.setattr(la, "THINK", "true")
    p = la._ollama_payload([{"role": "user", "content": "hi"}])
    assert p["think"] is True


def test_ollama_payload_think_unknown_value_is_omitted(monkeypatch):
    """A garbage value must not produce a bogus "think": false that silently
    disables reasoning on a model the caller intended to think. Only the
    exact tokens "true"/"false" opt in; anything else is a no-op."""
    monkeypatch.setattr(la, "THINK", "yes")
    p = la._ollama_payload([{"role": "user", "content": "hi"}])
    assert "think" not in p


@pytest.mark.parametrize("level", ["low", "medium", "high", "max"])
def test_ollama_payload_think_level_is_passed_through(monkeypatch, level):
    """LOCAL_AGENT_THINK accepts the graded-reasoning level strings too (not
    just true/false) - passed through verbatim as Ollama's "think" field.
    Live-validated against gemma4:12b-mlx, which 400s on any value outside
    this set."""
    monkeypatch.setattr(la, "THINK", level)
    p = la._ollama_payload([{"role": "user", "content": "hi"}])
    assert p["think"] == level




def test_exclude_runtime_artifacts_adds_agent_transcript_json(tmp_path, monkeypatch):
    """The transcript-persistence file must be excluded the same way
    agent.log already is, so backend.py's LOCAL_AGENT_TRANSCRIPT_PATH write
    inside the worktree doesn't get swept into commits."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)

    la.exclude_runtime_artifacts()

    exclude_path = tmp_path / ".git" / "info" / "exclude"
    lines = exclude_path.read_text().splitlines()
    assert ".agent_transcript.json" in lines


def test_exclude_runtime_artifacts_agent_transcript_json_not_duplicated(tmp_path, monkeypatch):
    """Calling exclude_runtime_artifacts() twice must not duplicate the
    .agent_transcript.json line, matching the existing idempotent behavior
    for agent.log."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)

    la.exclude_runtime_artifacts()
    la.exclude_runtime_artifacts()

    exclude_path = tmp_path / ".git" / "info" / "exclude"
    lines = exclude_path.read_text().splitlines()
    assert lines.count(".agent_transcript.json") == 1


def test_exclude_runtime_artifacts_still_excludes_agent_log_and_pycache(tmp_path, monkeypatch):
    """Regression guard: adding .agent_transcript.json must not remove or
    reorder the pre-existing exclusions."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)

    la.exclude_runtime_artifacts()

    exclude_path = tmp_path / ".git" / "info" / "exclude"
    lines = exclude_path.read_text().splitlines()
    assert "agent.log" in lines
    assert "__pycache__/" in lines
    assert "*.pyc" in lines


def test_exclude_runtime_artifacts_hides_transcript_file_from_git_status(tmp_path, monkeypatch):
    """The actual observable behavior this fix delivers: once
    exclude_runtime_artifacts() has run, a real .agent_transcript.json file
    sitting in the worktree must not show up as untracked in `git status`."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)

    la.exclude_runtime_artifacts()
    (tmp_path / ".agent_transcript.json").write_text('{"messages": []}')

    status = subprocess.run(
        ["git", "status", "--porcelain"], check=False, cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert ".agent_transcript.json" not in status


# ---------------------------------------------------------------------------
# L1 production side (REVIEWER_ESCALATION_PLAN.md Layer 1): on a CI-fail-rework
# round, reject `done` when the full worktree suite isn't green, feeding the
# failing excerpt back - parallel to the dirty-worktree rejection. Non-rework
# dispatches keep today's behavior (commit-enforced, no suite gate).
# ---------------------------------------------------------------------------

def test_done_rejected_on_rework_round_when_full_suite_fails(tmp_path, monkeypatch, capsys):
    """CI-fail-rework round, clean worktree, but the agent's own test still
    fails: `done` must be rejected, the failing excerpt fed back into the
    conversation, and the loop must NOT exit 0. Bounded by the step cap."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(la, "MAX_STEPS", 3)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)
    excerpt = "FAILED test_rate_limiter.py::test_time_backwards_no_refill - assert 9.0 == 3.0"
    monkeypatch.setattr(la, "_full_suite_result", lambda: (False, excerpt, "test"))

    fake, calls = _sequence_chat([("done", {"summary": "first attempt"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    # Not accepted: the suite gate blocked done every step until the cap bound.
    assert rc != 0, f"done must not be accepted while the suite fails; rc={rc}\n{out!r}"
    assert "done rejected — full test suite still fails" in out, out
    # The excerpt was fed back: the second chat() call received it as a user
    # turn (calls[1] is the messages list as seen on the 2nd turn).
    assert len(calls) >= 2
    last_user = [m for m in calls[1] if m["role"] == "user"][-1]
    assert excerpt in last_user["content"], last_user["content"]


def test_done_rejected_message_does_not_presume_the_test_is_wrong(tmp_path, monkeypatch, capsys):
    """Root cause diagnosed live (2026-07-22/23, MODE-29-REVIEW-STORY-LOCK-GUARD):
    this message originated for the CI-fail-rework case, where the failure IS
    always the agent's own test (an oracle-scoped review never saw it). Once
    Gap 1 armed this same gate for ordinary REVIEW rework too, the message's
    flat assertion - "your own committed test has a wrong assertion" -
    became false in that case: the failure can equally be a still-incomplete
    IMPLEMENTATION. Observed consequence: immediately after this exact
    rejection, the agent pivoted to obsessively rewriting its test file for
    ~15 steps instead of fixing the implementation, because the message told
    it the test was the problem. The fed-back content must not assert which
    side is wrong; it must direct the agent to check both and make one
    targeted fix."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(la, "MAX_STEPS", 3)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)
    excerpt = "FAILED test_review_story_lock_guard.py::test_review_story_skips_when_lock_held"
    monkeypatch.setattr(la, "_full_suite_result", lambda: (False, excerpt, "test"))

    fake, calls = _sequence_chat([("done", {"summary": "first attempt"})])
    monkeypatch.setattr(la, "chat", fake)

    la.main()
    last_user = [m for m in calls[1] if m["role"] == "user"][-1]["content"]

    assert "your own committed test has a wrong assertion" not in last_user
    assert "implementation" in last_user.lower()
    assert excerpt in last_user


def test_done_accepted_on_non_rework_round_without_consulting_suite(tmp_path, monkeypatch, capsys):
    """Non-rework round, clean worktree: `done` is accepted as today (rc=0)
    and the full suite is NEVER consulted - proves the rework gate is scoped,
    not global."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", False)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)

    suite_calls: list = []

    def _suite_spy():
        suite_calls.append(True)
        return (False, "would-fail-but-uncalled", "test")

    monkeypatch.setattr(la, "_full_suite_result", _suite_spy)

    fake, _ = _sequence_chat([("done", {"summary": "done"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    assert rc == 0  # accepted, as today
    assert suite_calls == []  # suite never consulted on a non-rework round


def test_done_accepted_on_rework_round_when_full_suite_green(tmp_path, monkeypatch):
    """CI-fail-rework round, clean worktree, agent fixed its own test: full
    suite green -> `done` accepted (rc=0). This is the convergence case."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)
    monkeypatch.setattr(la, "_full_suite_result", lambda: (True, "", None))

    fake, _ = _sequence_chat([("done", {"summary": "fixed"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    assert rc == 0


def test_dirty_tree_auto_accept_does_not_bypass_suite_gate(tmp_path, monkeypatch, capsys):
    """Regression guard for a bypass the code review surfaced: the dirty-tree
    auto-accept-at-2 escape must NOT fire on a rework round when the full suite
    still fails. Without the gate at the auto-accept site, an agent could dodge
    the raised done-bar by calling done dirty (reject), done clean+failing-suite
    (reject), done dirty again (done_rejections>=2 -> auto-accept, return 0,
    suite never checked). On a rework round the suite must be checked before
    that escape too; a failing suite rejects instead of auto-accepting, bounded
    by the step cap."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(la, "MAX_STEPS", 4)
    # Always dirty: forces every done through the dirty-tree branch, so the
    # auto-accept-at-2 escape is the path under test (the clean-tree suite
    # gate is never reached).
    monkeypatch.setattr(la, "worktree_dirty", lambda: True)
    monkeypatch.setattr(la, "auto_wip_commit", lambda reason: None)
    monkeypatch.setattr(la, "_full_suite_result",
                        lambda: (False, "assert 9.0 == 3.0 - test_rate_limiter.py:62", "test"))

    fake, _ = _sequence_chat([("done", {"summary": "bypass attempt"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    # The bypass must NOT auto-accept a failing suite.
    assert rc != 0, f"auto-accept-at-2 bypassed the suite gate; rc={rc}\n{out!r}"
    assert "DONE with auto-WIP-commit" not in out, out
    # The suite was checked at the would-be-auto-accept site and rejected.
    assert "done rejected — full test suite still fails" in out, out


def test_rework_suite_reject_cap_parks_instead_of_burning_the_budget(
    tmp_path, monkeypatch, capsys):
    """Regression guard (root-caused live 2026-07-24 on the MODE40 stories):
    on a CI-fail-rework round a model that has corrupted the code and cannot
    green the suite alternates `done` (rejected: suite red) with narration
    ("I cannot resolve this"). That oscillation is invisible to NO_TOOL_CAP
    (a `done` tool call resets consecutive_no_tool) and to the per-target
    repetition guard (done/str_replace are both excluded from it), so before
    the cap the run burned the ENTIRE step/wall-clock budget doing nothing,
    parked, re-dispatched, and repeated — ~20h across 7+ re-dispatches on one
    story. The run must PARK (rc=2) after REWORK_SUITE_REJECT_CAP suite
    rejections, well before MAX_STEPS."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(la, "REWORK_SUITE_REJECT_CAP", 3)
    monkeypatch.setattr(la, "MAX_STEPS", 30)  # far above the cap
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)

    suite_calls: list = []

    def _suite_spy():
        suite_calls.append(True)
        return (False, "FAILED test_x.py::test_y - assert 1 == 2", "test")

    monkeypatch.setattr(la, "_full_suite_result", _suite_spy)

    # _sequence_chat emits `done` for every turn once the script runs out, so
    # this models an agent that keeps calling done on a persistently-red suite.
    fake, calls = _sequence_chat([("done", {"summary": "attempt"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 2, f"expected park (rc=2) at the cap; got rc={rc}\n{out!r}"
    assert "rework suite-reject cap (3) reached" in out, out
    # Parked exactly at the cap — did NOT burn all 30 steps.
    assert len(suite_calls) == 3, f"suite consulted {len(suite_calls)}x, expected 3"
    assert len(calls) <= 4, f"took {len(calls)} turns; expected to park by ~3"


def test_rework_suite_reject_cap_is_driven_by_the_constant(tmp_path, monkeypatch, capsys):
    """The park point honors REWORK_SUITE_REJECT_CAP, not a hardcoded 3: with
    the cap set to 2 the run parks after exactly two suite rejections."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(la, "REWORK_SUITE_REJECT_CAP", 2)
    monkeypatch.setattr(la, "MAX_STEPS", 30)
    monkeypatch.setattr(la, "worktree_dirty", lambda: False)

    suite_calls: list = []

    def _suite_spy():
        suite_calls.append(True)
        return (False, "FAILED test_x.py::test_y", "test")

    monkeypatch.setattr(la, "_full_suite_result", _suite_spy)

    fake, _ = _sequence_chat([("done", {"summary": "attempt"})])
    monkeypatch.setattr(la, "chat", fake)

    rc = la.main()
    out = capsys.readouterr().out

    assert rc == 2, f"expected park (rc=2); got rc={rc}\n{out!r}"
    assert "rework suite-reject cap (2) reached" in out, out
    assert len(suite_calls) == 2, f"suite consulted {len(suite_calls)}x, expected 2"


# ---------------------------------------------------------------------------
# Mode 40 follow-up: lint feedback, both per-edit (fast, in-run) and as part
# of the done-bar's _full_suite_result (so a rework agent can't exit DONE on
# a lint failure the way the live incident did - see MODE40-CI-ERROR-DETAIL/
# MODE40-LOCAL-LINT-GATE in project memory).
# ---------------------------------------------------------------------------

def test_full_suite_result_runs_lint_after_tests_pass_and_fails_on_lint_error(
    tmp_path, monkeypatch,
):
    """Tests green, lint red -> _full_suite_result reports failure with the
    lint output, not a bare pass. This is the exact gap that let the live
    MODE40-LOCAL-LINT-GATE agent exit DONE on ruff E402/F841 violations."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la.p, "detect_test_command", lambda cwd: (tmp_path, ["pytest", "-q"]))
    monkeypatch.setattr(la.p, "detect_lint_command", lambda cwd: (tmp_path, ["ruff", "check", "."]))
    monkeypatch.setattr(la.p, "_is_heavy", lambda argv: False)

    calls = []

    class _R:
        def __init__(self, rc, out="", err=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = err

    def _run(argv, cwd, capture_output, text, **kw):
        calls.append(argv)
        if argv[0] == "pytest":
            return _R(0)
        return _R(1, err="test_foo.py:5:1: F401 'os' imported but unused")

    monkeypatch.setattr(la.subprocess, "run", _run)

    ok, tail, gate = la._full_suite_result()
    assert ok is False
    assert gate == "lint"
    assert "F401" in tail
    assert calls == [["pytest", "-q"], ["ruff", "check", "."]]


def test_full_suite_result_tests_and_lint_both_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la.p, "detect_test_command", lambda cwd: (tmp_path, ["pytest", "-q"]))
    monkeypatch.setattr(la.p, "detect_lint_command", lambda cwd: (tmp_path, ["ruff", "check", "."]))
    monkeypatch.setattr(la.p, "_is_heavy", lambda argv: False)

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(la.subprocess, "run", lambda *a, **k: _R())

    ok, tail, gate = la._full_suite_result()
    assert ok is True
    assert tail == ""
    assert gate is None


def test_full_suite_result_skips_lint_when_not_detected(tmp_path, monkeypatch):
    """No lint signal for this repo (detect_lint_command -> None) -> lint is
    never invoked and behavior is unchanged from before this feature."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la.p, "detect_test_command", lambda cwd: (tmp_path, ["pytest", "-q"]))
    monkeypatch.setattr(la.p, "detect_lint_command", lambda cwd: None)
    monkeypatch.setattr(la.p, "_is_heavy", lambda argv: False)

    calls = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(argv, cwd, capture_output, text, **kw):
        calls.append(argv)
        return _R()

    monkeypatch.setattr(la.subprocess, "run", _run)

    ok, _tail, gate = la._full_suite_result()
    assert ok is True
    assert gate is None
    assert calls == [["pytest", "-q"]]  # lint subprocess never invoked


def test_full_suite_result_does_not_run_lint_when_tests_fail(tmp_path, monkeypatch):
    """Regression bar: a failing test suite short-circuits before lint runs
    at all - unchanged existing behavior, lint is an additional gate only
    reached once tests are already green."""
    monkeypatch.setattr(la, "CWD", tmp_path)
    monkeypatch.setattr(la.p, "detect_test_command", lambda cwd: (tmp_path, ["pytest", "-q"]))
    monkeypatch.setattr(la.p, "detect_lint_command", lambda cwd: (tmp_path, ["ruff", "check", "."]))
    monkeypatch.setattr(la.p, "_is_heavy", lambda argv: False)

    calls = []

    class _R:
        def __init__(self, rc, out="", err=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = err

    def _run(argv, cwd, capture_output, text, **kw):
        calls.append(argv)
        return _R(1, err="FAILED test_x.py::test_y")

    monkeypatch.setattr(la.subprocess, "run", _run)

    ok, tail, gate = la._full_suite_result()
    assert ok is False
    assert gate == "test"
    assert "test_y" in tail
    # Re-run once before rejecting (retry-once exemption); lint never invoked.
    assert calls == [["pytest", "-q"], ["pytest", "-q"]]


