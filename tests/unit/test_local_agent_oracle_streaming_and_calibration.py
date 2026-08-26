"""Tests for the oracle-variant local dispatch agent loop (scripts/local_agent_oracle.py): chat() streaming/retry, provider routing, chars/token calibration, and the start of 5xx trim-retry handling.

Split out of test_local_agent_oracle.py to keep it under the project's line-count target; shared fixtures/helpers (including the loaded `lao` module itself) moved to tests.unit._local_agent_oracle_test_helpers.
"""
import json
import os
import subprocess

import httpx
import pytest

from tests.unit._local_agent_oracle_test_helpers import (  # noqa: F401
    _FakeStreamCM,
    _FakeStreamResponse,
    _finish_if_green_spy,
    _init_git_repo,
    _isolate_environ,
    _status_error,
    lao,
    load_oracle_module_with_env,
)


def test_chat_retries_on_provider_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(lao, "PROVIDER", "lmstudio")
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _flaky_5xx(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(503)
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(lao, "_provider_chat_turn", _flaky_5xx)
    msg = lao.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2
    assert msg["content"] == "ok"


def test_chat_does_not_retry_on_provider_4xx(monkeypatch):
    monkeypatch.setattr(lao, "PROVIDER", "lmstudio")
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _bad_request(messages):
        calls["n"] += 1
        raise _status_error(400)

    monkeypatch.setattr(lao, "_provider_chat_turn", _bad_request)
    try:
        lao.chat([{"role": "user", "content": "hi"}])
        assert False, "expected HTTPStatusError(400)"
    except httpx.HTTPStatusError:
        pass
    assert calls["n"] == 1, "4xx must NOT be retried"


def test_chat_retries_on_provider_rate_limited_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(lao, "PROVIDER", "mlx")
    monkeypatch.setattr(lao.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _rate_limited_then_ok(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise lao.inference_providers.RateLimitedError("429")
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(lao, "_provider_chat_turn", _rate_limited_then_ok)
    msg = lao.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2
    assert msg["content"] == "ok"






def test_stream_one_turn_assembles_streamed_chunks(monkeypatch):
    """_stream_one_turn accumulates content across streamed JSON chunks and
    returns the assembled message (same shape the old r.json()['message']
    returned). A done:true chunk terminates the stream."""
    # Simulate Ollama streaming: content split across 3 chunks, then a
    # done:true terminator. devstral emits tool calls as text content, so
    # tool_calls stays None and recover_tool_calls parses content downstream.
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "I'll "}}),
        json.dumps({"message": {"role": "assistant", "content": "create "}}),
        json.dumps({"message": {"role": "assistant", "content": "a file."}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    msg = lao._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["role"] == "assistant"
    assert msg["content"] == "I'll create a file."
    assert "tool_calls" not in msg  # none emitted in this stream


def test_stream_one_turn_captures_native_tool_calls(monkeypatch):
    """For models that use native tool_calls (not devstral's text style),
    _stream_one_turn captures them from the done chunk and attaches them to
    the assembled message so main()'s m.get('tool_calls') sees them."""
    lines = [
        json.dumps({"message": {"role": "assistant", "content": ""}}),
        json.dumps({"message": {"role": "assistant", "content": ""},
                     "tool_calls": [{"function": {"name": "bash",
                                                   "arguments": {"command": "ls"}}}],
                     "done": True}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    msg = lao._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert msg["tool_calls"] == [{"function": {"name": "bash",
                                                "arguments": {"command": "ls"}}}]


def test_stream_one_turn_raises_on_5xx(monkeypatch):
    """A 5xx response makes raise_for_status raise HTTPStatusError, which
    chat() then retries. _stream_one_turn itself must surface it."""
    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse([], status_code=500))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    try:
        lao._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
        assert False, "expected HTTPStatusError(500)"
    except httpx.HTTPStatusError as e:
        assert e.response.status_code == 500


def test_stream_one_turn_uses_configurable_connect_timeout(monkeypatch):
    """connect=10.0 was hardcoded and too short for a cold local-model load -
    observed live 2026-07-13: both glm-4.7-flash (~17s cold load) and
    qwen3-coder:30b timed out identically. Ollama aborts the in-flight load
    and frees the memory it had claimed the instant the client gives up, so
    a too-short connect timeout causes an infinite load/abort/retry cycle
    that never completes rather than a genuine failure. CONNECT_TIMEOUT_
    SECONDS makes this configurable like the other chat-retry knobs
    (READ_SILENCE_SECONDS, CHAT_MAX_ATTEMPTS)."""
    monkeypatch.setattr(lao, "CONNECT_TIMEOUT_SECONDS", 45.0)
    captured = {}

    def _fake_stream(method, url, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        lines = [json.dumps({"message": {"role": "assistant", "content": "hi"}, "done": True})]
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    lao._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert captured["timeout"].connect == 45.0


# ---------- live chars/token calibration from ollama's own prompt_eval_count
# (ported from local_agent.py, 2026-07-29) ----------

def test_oracle_stream_one_turn_captures_prompt_eval_count_and_calibrates_ratio(monkeypatch):
    monkeypatch.setattr(lao, "_measured_chars_per_token", None)
    monkeypatch.setattr(lao, "_last_prompt_eval_count", None)
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "hi"}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                    "prompt_eval_count": 100}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    payload = {"model": "x", "messages": [{"role": "user", "content": "x" * 250}],
               "tools": [], "stream": True}
    lao._stream_one_turn(payload)
    assert lao._last_prompt_eval_count == 100
    # 250 message chars + len("[]") for the empty tools schema, over 100
    # measured prompt tokens (see the tools-schema test below).
    assert lao._measured_chars_per_token == pytest.approx((250 + 2) / 100)


def test_oracle_stream_one_turn_leaves_calibration_unset_without_prompt_eval_count(monkeypatch):
    monkeypatch.setattr(lao, "_measured_chars_per_token", None)
    monkeypatch.setattr(lao, "_last_prompt_eval_count", None)
    lines = [json.dumps({"message": {"role": "assistant", "content": "hi"}, "done": True})]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    lao._stream_one_turn({"model": "x", "messages": [], "tools": [], "stream": True})
    assert lao._last_prompt_eval_count is None
    assert lao._measured_chars_per_token is None


def test_oracle_stream_one_turn_counts_tools_schema_in_calibration(monkeypatch):
    """The tools schema is counted in prompt_eval_count, so it must be in the
    numerator too - see local_agent.py's copy of this test for the measured
    bias (0.67 vs a true ~2.35 on an early turn) this guards against."""
    monkeypatch.setattr(lao, "_measured_chars_per_token", None)
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "hi"}}),
        json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                    "prompt_eval_count": 100}),
    ]

    def _fake_stream(method, url, **kwargs):
        return _FakeStreamCM(_FakeStreamResponse(lines, status_code=200))

    monkeypatch.setattr(lao.httpx, "stream", _fake_stream)
    tools = [{"type": "function", "function": {"name": "x" * 146}}]
    tools_chars = len(json.dumps(tools))
    payload = {"model": "x", "messages": [{"role": "user", "content": "y" * 250}],
               "tools": tools, "stream": True}
    lao._stream_one_turn(payload)
    assert lao._measured_chars_per_token == pytest.approx((250 + tools_chars) / 100)


def test_oracle_effective_chars_per_token_prefers_calibrated_value(monkeypatch):
    monkeypatch.setattr(lao, "_measured_chars_per_token", 2.35)
    assert lao._effective_chars_per_token() == 2.35


def test_oracle_effective_chars_per_token_falls_back_to_estimate(monkeypatch):
    monkeypatch.setattr(lao, "_measured_chars_per_token", None)
    assert lao._effective_chars_per_token() == lao._CHARS_PER_TOKEN_ESTIMATE


def test_oracle_main_resets_calibration_globals_at_start(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao, "_measured_chars_per_token", 1.5)
    monkeypatch.setattr(lao, "_last_prompt_eval_count", 99999)
    monkeypatch.setattr(lao, "MAX_STEPS", 1)
    observed = {}

    def _fake_chat(messages):
        observed["last_prompt_eval_count"] = lao._last_prompt_eval_count
        observed["measured_chars_per_token"] = lao._measured_chars_per_token
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(lao, "chat", _fake_chat)
    lao.main()
    assert observed["last_prompt_eval_count"] is None
    assert observed["measured_chars_per_token"] is None


def test_oracle_main_proactively_trims_when_measured_tokens_near_num_ctx(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao, "NUM_CTX", 100)
    monkeypatch.setattr(lao, "PROACTIVE_TRIM_THRESHOLD", 0.85)
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
            monkeypatch.setattr(lao, "_last_prompt_eval_count", 90)
            monkeypatch.setattr(lao, "_measured_chars_per_token", 1.0)
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(lao, "chat", _fake_chat)
    rc = lao.main()
    out = capsys.readouterr().out
    assert rc == 0, f"expected the run to finish cleanly, got rc={rc}\noutput: {out!r}"
    assert "trimming proactively" in out, f"expected a proactive-trim log line, output: {out!r}"


# ---------- main() trims and retries once on a persistent 5xx (ported from
# local_agent.py — this variant was missing the recovery entirely, so an
# oversized transcript on an acceptance-bearing dispatch (the production
# path: backend.py routes every story with an `acceptance` block here) died
# on the first unrecoverable 5xx instead of shrinking and retrying) ----------

def test_oracle_main_trims_transcript_and_retries_once_on_persistent_5xx(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    # A tiny NUM_CTX means genuine post-head content (grown by the two
    # successful reads below) already exceeds the trim budget, so trimming
    # reliably triggers without needing to hand-construct a huge transcript.
    monkeypatch.setattr(lao, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        if calls["n"] == 3:
            raise _status_error(500)
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(lao, "chat", _fake_chat)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 0, f"expected the trim-and-retry to recover, got rc={rc}\noutput: {out!r}"
    assert calls["n"] == 4, (
        f"expected exactly 4 chat() calls (2 reads, fail once, succeed on "
        f"the trim-retry), got {calls['n']}\noutput: {out!r}"
    )
    assert "escalating trim and retrying" in out, f"expected the escalation trim log line, output: {out!r}"


def test_oracle_main_gives_up_when_trim_retry_also_fails(tmp_path, monkeypatch, capsys):
    """Escalation is bounded, not an open-ended loop: if chat() still fails
    after every escalation round, main() must give up (return 1) rather
    than retrying indefinitely.

    Call count relaxed from 4 to the bounded round count on 2026-08-07:
    an unshrinkable payload now retries unchanged after a backoff instead
    of bailing on the first round (see recover_from_oversized_5xx). The
    property under test - terminates, returns 1 - is unchanged."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        raise _status_error(500)

    monkeypatch.setattr(lao, "chat", _fake_chat)
    monkeypatch.setattr(lao.time, "sleep", lambda _s: None)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 1, f"expected give-up after the trim-retry also fails, got rc={rc}\noutput: {out!r}"
    assert 4 <= calls["n"] <= 6, (
        f"expected the 2 reads plus a bounded escalation (<=3 rounds), "
        f"got {calls['n']}\noutput: {out!r}"
    )


# ---------- 5xx escalation must not crash the oracle agent loop (2026-07-30) ----------
# Ported from test_local_agent.py: recover_from_oversized_5xx only catches
# httpx.HTTPStatusError, so a 4xx it re-raises or a non-HTTP backend failure
# (TransportError, RateLimitedError) propagates out of the helper. It is called
# from inside main()'s except-HTTPStatusError handler, and a sibling
# except-Exception does NOT catch exceptions raised from within another except
# body - so without the guard at the call site such a failure escapes main() and
# kills the run, regressing the original "must not crash the agent loop"
# invariant. The oracle runs the production path for acceptance-bearing
# dispatches, so it must carry the same guard as local_agent.py.

def test_oracle_main_does_not_crash_on_transport_error_during_5xx_escalation(
    tmp_path, monkeypatch, capsys,
):
    """A TransportError raised by chat() during an escalation round must give up
    (return 1), not propagate out of main() and crash the oracle agent loop."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        if calls["n"] == 3:
            raise _status_error(500)  # enter the 5xx escalation handler
        raise httpx.TransportError("connection reset")  # escalation round failure

    monkeypatch.setattr(lao, "chat", _fake_chat)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 1, (
        f"expected graceful give-up on a TransportError during escalation, got "
        f"rc={rc}\noutput: {out!r}"
    )
    assert calls["n"] == 4, (
        f"expected 4 chat() calls (2 reads + 5xx + one escalation round), got "
        f"{calls['n']}\noutput: {out!r}"
    )
    assert "LLM call failed during 5xx escalation" in out, f"output: {out!r}"


def test_oracle_main_does_not_crash_on_4xx_during_5xx_escalation(
    tmp_path, monkeypatch, capsys,
):
    """A 4xx re-raised by the escalation helper must give up (return 1), not
    escape main() and crash the oracle agent loop - matching the original
    trim-retry path, which caught every failure and returned 1."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])
    monkeypatch.setattr(lao, "NUM_CTX", 10)
    calls = {"n": 0}

    def _fake_chat(messages):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "view_file", "arguments": {"path": "a.txt"}}}]}
        if calls["n"] == 3:
            raise _status_error(500)  # enter the 5xx escalation handler
        raise _status_error(400)  # escalation round re-raises a 4xx

    monkeypatch.setattr(lao, "chat", _fake_chat)

    rc = lao.main()
    out = capsys.readouterr().out

    assert rc == 1, (
        f"expected graceful give-up on a 4xx during escalation, got rc={rc}\n"
        f"output: {out!r}"
    )
    assert calls["n"] == 4, (
        f"expected 4 chat() calls (2 reads + 5xx + one escalation round), got "
        f"{calls['n']}\noutput: {out!r}"
    )
    assert "LLM call failed during 5xx escalation" in out, f"output: {out!r}"


# ---------- transcript persistence + resume (ported from local_agent.py, see
# test_local_agent_persistence.py for the reference test suite) ----------
# These tests need a fresh module import per test with different env vars
# (LOCAL_AGENT_RESUME_TRANSCRIPT_PATH etc. are read at call time via
# os.environ.get, but loading a fresh module keeps each test isolated from
# the module-level `lao` instance shared by the rest of this file).



def test_oracle_persistence_writes_valid_json(tmp_path):
    transcript_file = tmp_path / "transcript.json"
    env = {"LOCAL_AGENT_TRANSCRIPT_PATH": str(transcript_file)}
    mod = load_oracle_module_with_env(env)
    messages = mod.PersistingList(transcript_path=str(transcript_file))
    msg = {"role": "assistant", "content": "hi"}
    messages.append(msg)
    assert transcript_file.exists()
    with open(transcript_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data == [msg]
    assert not list(tmp_path.glob("*.tmp"))


def test_oracle_resume_loads_valid_transcript(tmp_path):
    transcript_file = tmp_path / "resume.json"
    data = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    transcript_file.write_text(json.dumps(data), encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(transcript_file)}
    mod = load_oracle_module_with_env(env)
    assert mod._load_resume_transcript() == data


def test_oracle_resume_appends_new_user_turn(tmp_path):
    transcript_file = tmp_path / "resume.json"
    data = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    transcript_file.write_text(json.dumps(data), encoding="utf-8")
    env = {
        "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(transcript_file),
        "LOCAL_AGENT_RESUME_APPEND_CONTENT": "feedback",
    }
    mod = load_oracle_module_with_env(env)
    loaded = mod._load_resume_transcript()
    messages = mod.PersistingList()
    messages.extend(loaded)
    if loaded and env.get("LOCAL_AGENT_RESUME_APPEND_CONTENT"):
        messages.append({"role": "user", "content": env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]})
    assert messages[-1] == {"role": "user", "content": "feedback"}
    assert len(messages) == 3


def test_oracle_resume_fallback_missing_file(tmp_path, capsys):
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(tmp_path / "nonexistent.json")}
    mod = load_oracle_module_with_env(env)
    loaded = mod._load_resume_transcript()
    assert loaded is None
    out, _ = capsys.readouterr()
    assert "RESUME FAILED" in out


def test_oracle_resume_fallback_invalid_json(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{invalid json", encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    mod = load_oracle_module_with_env(env)
    loaded = mod._load_resume_transcript()
    assert loaded is None
    out, _ = capsys.readouterr()
    assert "RESUME FAILED" in out


def test_oracle_resume_fallback_invalid_shape_empty_list(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("[]", encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    mod = load_oracle_module_with_env(env)
    loaded = mod._load_resume_transcript()
    assert loaded is None
    out, _ = capsys.readouterr()
    assert "RESUME FAILED" in out


def test_oracle_resume_fallback_invalid_shape_dict(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"role": "system", "content": "sys"}), encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    mod = load_oracle_module_with_env(env)
    loaded = mod._load_resume_transcript()
    assert loaded is None
    out, _ = capsys.readouterr()
    assert "RESUME FAILED" in out


def test_oracle_resume_fallback_invalid_shape_unknown_role(tmp_path, capsys):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps([{"role": "narrator", "content": "sys"}]), encoding="utf-8")
    env = {"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(bad_file)}
    mod = load_oracle_module_with_env(env)
    loaded = mod._load_resume_transcript()
    assert loaded is None
    out, _ = capsys.readouterr()
    assert "RESUME FAILED" in out


def test_oracle_no_persistence_when_path_unset(tmp_path):
    env = {"LOCAL_AGENT_TRANSCRIPT_PATH": None}
    mod = load_oracle_module_with_env(env)
    messages = mod.PersistingList()
    messages.append({"role": "assistant", "content": "hi"})
    assert not list(tmp_path.glob("*.json"))


def test_resume_env_helper_sets_var_for_isolation_regression(tmp_path):
    """Companion to test_resume_env_does_not_leak_into_next_test below: sets
    LOCAL_AGENT_RESUME_TRANSCRIPT_PATH via the helper so the next test (which
    must run immediately after this one - see its docstring) can assert it
    doesn't survive into a fresh test's os.environ."""
    dummy = tmp_path / "resume.json"
    load_oracle_module_with_env({"LOCAL_AGENT_RESUME_TRANSCRIPT_PATH": str(dummy)})
    assert os.environ["LOCAL_AGENT_RESUME_TRANSCRIPT_PATH"] == str(dummy)


def test_resume_env_does_not_leak_into_next_test():
    """Regression for the test-isolation leak: load_oracle_module_with_env
    mutates os.environ directly with no teardown, so a var set by the
    previous test (see above) silently survives into this one and into any
    dispatch test that happens to run afterward in the same pytest process
    (e.g. test_dispatch_omits_resume_transcript_path_when_unset in
    test_backend.py, which builds its subprocess env from os.environ).
    This test is order-dependent by design: it must run directly after
    test_resume_env_helper_sets_var_for_isolation_regression (this repo's
    pyproject.toml configures no test-randomization plugin, so pytest's
    default in-file execution order is deterministic)."""
    assert "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH" not in os.environ


def test_oracle_atomic_write_no_temp_file(tmp_path):
    transcript_file = tmp_path / "transcript.json"
    env = {"LOCAL_AGENT_TRANSCRIPT_PATH": str(transcript_file)}
    mod = load_oracle_module_with_env(env)
    messages = mod.PersistingList(transcript_path=str(transcript_file))
    messages.append({"role": "assistant", "content": "hi"})
    assert not list(tmp_path.glob("*.tmp"))


def test_oracle_main_uses_fresh_messages_when_no_resume_path(monkeypatch, tmp_path):
    """When LOCAL_AGENT_RESUME_TRANSCRIPT_PATH is unset, main() must build the
    fresh [system, user-task] pair, matching local_agent.py's fallback."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOCAL_AGENT_RESUME_TRANSCRIPT_PATH", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_TRANSCRIPT_PATH", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_RESUME_APPEND_CONTENT", raising=False)
    monkeypatch.setenv("LOCAL_AGENT_TASK", "do the task")
    monkeypatch.setenv("LOCAL_AGENT_MAX_STEPS", "1")
    monkeypatch.setattr(lao, "MAX_STEPS", 1)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])

    captured = {}

    def _fake_chat(messages):
        captured["messages"] = list(messages)
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(lao, "chat", _fake_chat)
    monkeypatch.setattr(lao, "oracle_result", lambda: (True, ""))
    monkeypatch.setattr(lao, "worktree_dirty", lambda: False)
    monkeypatch.setattr(lao, "exclude_runtime_artifacts", lambda: None)

    lao.main()

    messages = captured["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "do the task"}


def test_oracle_main_resumes_transcript_and_skips_fresh_pair(monkeypatch, tmp_path):
    """When LOCAL_AGENT_RESUME_TRANSCRIPT_PATH points at a valid transcript,
    main() must load it instead of building the fresh system/task pair."""
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.chdir(tmp_path)
    transcript_file = tmp_path / "resume.json"
    resumed = [{"role": "system", "content": "orig sys"},
               {"role": "user", "content": "orig task"},
               {"role": "assistant", "content": "prior reply"}]
    transcript_file.write_text(json.dumps(resumed), encoding="utf-8")
    monkeypatch.setenv("LOCAL_AGENT_RESUME_TRANSCRIPT_PATH", str(transcript_file))
    monkeypatch.delenv("LOCAL_AGENT_TRANSCRIPT_PATH", raising=False)
    monkeypatch.setenv("LOCAL_AGENT_RESUME_APPEND_CONTENT", "reviewer feedback")
    monkeypatch.setenv("LOCAL_AGENT_TASK", "do the task")
    monkeypatch.setattr(lao, "MAX_STEPS", 1)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", [])

    captured = {}

    def _fake_chat(messages):
        captured["messages"] = list(messages)
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "done", "arguments": {"summary": "ok"}}}]}

    monkeypatch.setattr(lao, "chat", _fake_chat)
    monkeypatch.setattr(lao, "oracle_result", lambda: (True, ""))
    monkeypatch.setattr(lao, "worktree_dirty", lambda: False)
    monkeypatch.setattr(lao, "exclude_runtime_artifacts", lambda: None)

    lao.main()

    messages = captured["messages"]
    assert messages[0] == resumed[0]
    assert messages[1] == resumed[1]
    assert messages[2] == resumed[2]
    assert messages[3] == {"role": "user", "content": "reviewer feedback"}




def test_oracle_exclude_runtime_artifacts_adds_agent_transcript_json(tmp_path, monkeypatch):
    """The transcript-persistence file must be excluded the same way
    agent.log already is, so backend.py's LOCAL_AGENT_TRANSCRIPT_PATH write
    inside the worktree doesn't get swept into commits."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)

    lao.exclude_runtime_artifacts()

    exclude_path = tmp_path / ".git" / "info" / "exclude"
    lines = exclude_path.read_text().splitlines()
    assert ".agent_transcript.json" in lines


def test_oracle_exclude_runtime_artifacts_agent_transcript_json_not_duplicated(tmp_path, monkeypatch):
    """Calling exclude_runtime_artifacts() twice must not duplicate the
    .agent_transcript.json line, matching the existing idempotent behavior
    for agent.log."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)

    lao.exclude_runtime_artifacts()
    lao.exclude_runtime_artifacts()

    exclude_path = tmp_path / ".git" / "info" / "exclude"
    lines = exclude_path.read_text().splitlines()
    assert lines.count(".agent_transcript.json") == 1


def test_oracle_exclude_runtime_artifacts_still_excludes_agent_log_and_pycache(tmp_path, monkeypatch):
    """Regression guard: adding .agent_transcript.json must not remove or
    reorder the pre-existing exclusions."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)

    lao.exclude_runtime_artifacts()

    exclude_path = tmp_path / ".git" / "info" / "exclude"
    lines = exclude_path.read_text().splitlines()
    assert "agent.log" in lines
    assert "__pycache__/" in lines
    assert "*.pyc" in lines


def test_oracle_exclude_runtime_artifacts_hides_transcript_file_from_git_status(tmp_path, monkeypatch):
    """The actual observable behavior this fix delivers: once
    exclude_runtime_artifacts() has run, a real .agent_transcript.json file
    sitting in the worktree must not show up as untracked in `git status`."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(lao, "CWD", tmp_path)

    lao.exclude_runtime_artifacts()
    (tmp_path / ".agent_transcript.json").write_text('{"messages": []}')

    status = subprocess.run(
        ["git", "status", "--porcelain"], check=False, cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert ".agent_transcript.json" not in status


def test_recover_tool_calls_repairs_python_triple_quoted_arguments():
    """Kept in sync with test_local_agent's copy: the oracle agent (which the
    benchmark harness dispatches through) must also salvage a str_replace whose
    multi-line code argument is emitted with Python triple-quote syntax, or the
    edit is silently dropped (observed with Qwen2.5-Coder-14B-4bit on mlx)."""
    content = (
        '```json\n'
        '{\n'
        '  "name": "str_replace",\n'
        '  "arguments": {\n'
        '    "path": "rate_limiter.py",\n'
        '    "old_str": "# TODO",\n'
        '    "new_str": """\n'
        'class TokenBucket:\n'
        '    def __init__(self, capacity):\n'
        '        self.capacity = capacity\n'
        '"""\n'
        '  }\n'
        '}\n'
        '```'
    )
    out = lao.recover_tool_calls(content)
    assert out and out[0]["function"]["name"] == "str_replace"
    args = out[0]["function"]["arguments"]
    assert args["path"] == "rate_limiter.py"
    assert "def __init__(self, capacity):" in args["new_str"]


def test_recover_tool_calls_tolerates_raw_newlines_in_json_string():
    """Kept in sync with test_local_agent's copy: a distinct malformation
    from the triple-quote case (observed live, 2026-07-17, Qwen2.5-Coder-14B-
    4bit on mlx, interval_merge task, the benchmark harness this oracle agent
    is dispatched through) - the model uses ordinary double-quoted JSON
    string syntax for a create_file's multi-line `content` argument, but
    embeds RAW literal newline bytes instead of escaping them as `\\n`. A raw
    json.loads rejects this ("Invalid control character"), the triple-quote
    repair does not apply, and the tool call was silently dropped every
    retry until the wall-clock park with the real fix never landing."""
    content = (
        '```json\n'
        '{\n'
        '  "name": "create_file",\n'
        '  "arguments": {\n'
        '    "path": "intervals.py",\n'
        '    "content": "def merge(x):\n'
        '    return x"\n'
        '  }\n'
        '}\n'
        '```'
    )
    out = lao.recover_tool_calls(content)
    assert out and out[0]["function"]["name"] == "create_file"
    args = out[0]["function"]["arguments"]
    assert args["path"] == "intervals.py"
    assert "def merge(x):" in args["content"]
    assert "return x" in args["content"]


def test_recover_tool_calls_returns_none_on_non_toolcall_prose():
    """The repair pass must fail closed on ordinary prose - no phantom call."""
    assert lao.recover_tool_calls("Looks good, nothing left to change.") is None


# ---------------------------------------------------------------------------
# L1: harness-enforced full-suite done-bar on CI-fail rework rounds.
# (REVIEWER_ESCALATION_PLAN.md Layer 1.) On a rework round triggered by a
# merge-gate CI failure, the agent's own broken test is the defect, but the
# acceptance oracle excludes that test file - so oracle-green must NOT be
# allowed to terminate the loop. finish_if_green must additionally require the
# full worktree suite green, feeding the failing excerpt back. Cold-start
# behavior stays byte-for-byte identical (oracle-green remains the bar).
# ---------------------------------------------------------------------------



def test_finish_if_green_cold_start_terminates_on_oracle_green_only(monkeypatch):
    """Cold start (REWORK_FULL_SUITE unset): oracle green is the done-bar and
    the full suite is NEVER consulted - even when it would fail. Proves the
    rework gate is scoped, not global, so a fresh dispatch's behavior is
    unchanged."""
    messages, commits, full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=True, full_ok=False, full_tail="would-fail-but-uncalled"
    )
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", False)
    assert lao.finish_if_green(3, messages=messages) is True
    assert commits == ["feat: implement task (acceptance oracle green)"]
    assert full_calls == []  # the full suite was not run on a cold start


def test_finish_if_green_rework_round_blocks_done_when_full_suite_fails(monkeypatch):
    """CI-fail rework round: oracle green but the agent's own test still fails.
    finish_if_green must NOT terminate, must NOT commit, and must feed the
    failing-test excerpt back into messages so the agent works the broken
    assertion on the next loop iteration instead of declaring done."""
    excerpt = "AssertionError: assert 9.0 == 3.0  - test_rate_limiter.py:62"
    messages, commits, full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=True, full_ok=False, full_tail=excerpt
    )
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    assert lao.finish_if_green(7, messages=messages) is False
    assert commits == []  # no auto-commit while the agent's own test still fails
    assert full_calls == [True]
    # The failing excerpt was fed back as a user turn so the model sees it.
    assert any(m["role"] == "user" and excerpt in m["content"] for m in messages), messages


def test_finish_if_green_rework_round_terminates_when_full_suite_green(monkeypatch):
    """CI-fail rework round: agent fixed its own test, full suite now green
    alongside the oracle. finish_if_green must terminate and commit - the
    raised done-bar is satisfied. This is the convergence case."""
    messages, commits, full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=True, full_ok=True, full_tail=""
    )
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    assert lao.finish_if_green(9, messages=messages) is True
    assert commits == ["feat: implement task (acceptance oracle green)"]
    assert full_calls == [True]


def test_finish_if_green_rework_oracle_not_green_returns_false(monkeypatch):
    """Oracle not green: no termination regardless of rework flag - the oracle
    is still the primary bar; the full suite is an additional gate on top."""
    messages, commits, full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=False, full_ok=True, full_tail=""
    )
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    assert lao.finish_if_green(2, messages=messages) is False
    assert commits == []
    assert full_calls == []  # short-circuited: oracle not green, suite not run


# ---------------------------------------------------------------------------
# Unbounded finish_if_green rework loop (found live 2026-07-29 on
# TRANSPORT-ALIAS-DEPRECATION): the `done` handler bounds its full-suite
# rejections with REWORK_SUITE_REJECT_CAP and parks (rc 2) once exhausted, but
# finish_if_green - the OTHER termination path, which fires automatically after
# every create_file/str_replace/bash - had no such bound. When the full suite
# could not be greened (there, a rework-authored test that contradicted the
# read-only acceptance oracle), it printed "ORACLE GREEN but full suite still
# fails" and returned False on every single mutating step until the step cap,
# burning the entire budget with no progress and no park signal.
# ---------------------------------------------------------------------------

def test_finish_if_green_parks_after_suite_reject_cap(monkeypatch):
    """finish_if_green must bound its full-suite rejections the same way the
    `done` handler does. After REWORK_SUITE_REJECT_CAP consecutive oracle-
    green/suite-red calls it must signal park rather than returning False
    forever, so an unsatisfiable suite ends the run instead of burning every
    remaining step."""
    messages, _commits, _full_calls = _finish_if_green_spy(
        monkeypatch, oracle_ok=True, full_ok=False, full_tail="1 failed"
    )
    monkeypatch.setattr(lao, "REWORK_FULL_SUITE", True)
    monkeypatch.setattr(lao, "REWORK_SUITE_REJECT_CAP", 3)
    lao._reset_suite_rejections()

    # The first CAP-1 rejections keep the agent working the failure.
    assert lao.finish_if_green(1, messages=messages) is False
    assert lao.finish_if_green(2, messages=messages) is False
    assert lao.suite_reject_cap_reached() is False

    # The CAP'th rejection trips the bound.
    assert lao.finish_if_green(3, messages=messages) is False
    assert lao.suite_reject_cap_reached() is True


