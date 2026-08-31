"""Tests for the local agent's context-compaction path.

Covers five defects found 2026-08-07 while auditing the 5xx/context guardrail
across providers:

S1  _provider_chat_turn discarded the envelope's prompt_eval_count, so
    _last_prompt_eval_count / _measured_chars_per_token stayed None on
    lmstudio/mlx and the proactive trim - the primary defense - never fired
    at all on those providers.
S2  _trim_resumed_transcript dropped whole blocks (assistant decisions and
    all) when nearly all the bytes live in tool-role output. Claude Code's
    /compact clears older tool outputs first for exactly this reason.
S3  The drop-note said only "[N turns dropped]", discarding facts that are
    mechanically recoverable from the dropped span (files written, commands
    run, last test result) and that the agent then re-derives.
S4  LM Studio returns HTTP 400 on context overflow ("Trying to keep the
    first N tokens when context the overflows..."). The 4xx fast-path
    treated that as fatal, bypassing the trim that would have fixed it.
S5  recover_from_oversized_5xx fired its rounds back-to-back with no sleep,
    and gave up when the trim could not shrink the payload - but that is
    positive evidence the 500 was NOT an overflow, i.e. exactly the case
    where backing off and retrying unchanged is the right remedy.
"""
import importlib.util
import json
import os
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
_spec = importlib.util.spec_from_file_location(
    "local_agent_compaction_under_test",
    str(Path(__file__).parent.parent.parent / "scripts" / "local_agent.py"),
)
la = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(la)


def _assistant_call(name, args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def _tool(content):
    return {"role": "tool", "content": content}


def _head():
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]


# ---------------------------------------------------------------- S1

class _FakeProvider:
    def __init__(self, prompt_eval_count):
        self._n = prompt_eval_count
        self.name = "lmstudio"

    def chat(self, messages, **kw):
        return {"message": {"role": "assistant", "content": "hi"},
                "prompt_eval_count": self._n, "eval_count": 5}


def test_provider_turn_records_prompt_eval_count(monkeypatch):
    """The proactive trim reads _last_prompt_eval_count; on lmstudio/mlx it
    was never set, so the trim could not fire. The providers already return
    the count - it was being thrown away."""
    monkeypatch.setattr(la, "_last_prompt_eval_count", None)
    monkeypatch.setattr(la, "_measured_chars_per_token", None)
    monkeypatch.setattr(la.inference_providers, "get_local_provider",
                        lambda *a, **k: _FakeProvider(1000))

    la._provider_chat_turn([{"role": "user", "content": "x" * 2350}])

    assert la._last_prompt_eval_count == 1000


def test_provider_turn_calibrates_chars_per_token(monkeypatch):
    """Without calibration the trim budget uses the fixed 4.0 guess, which
    the module's own docstring records as ~70% too high against a live
    ~2.35 - producing budgets ~28% over the real context window."""
    monkeypatch.setattr(la, "_last_prompt_eval_count", None)
    monkeypatch.setattr(la, "_measured_chars_per_token", None)
    monkeypatch.setattr(la.inference_providers, "get_local_provider",
                        lambda *a, **k: _FakeProvider(1000))

    la._provider_chat_turn([{"role": "user", "content": "x" * 2000}])

    # 2000 content chars + the TOOLS schema, over 1000 tokens.
    expected = (2000 + len(json.dumps(la.TOOLS))) / 1000
    assert la._measured_chars_per_token == pytest.approx(expected)


def test_provider_turn_still_returns_bare_message(monkeypatch):
    """Callers (chat()/main()) consume the message dict directly; recording
    usage must not change the return shape."""
    monkeypatch.setattr(la.inference_providers, "get_local_provider",
                        lambda *a, **k: _FakeProvider(10))
    out = la._provider_chat_turn([{"role": "user", "content": "x"}])
    assert out == {"role": "assistant", "content": "hi"}


def test_provider_turn_survives_missing_usage(monkeypatch):
    """A provider that reports no usage (0/absent) must not crash the turn or
    poison the calibration with a divide-by-zero."""
    monkeypatch.setattr(la, "_measured_chars_per_token", None)
    monkeypatch.setattr(la.inference_providers, "get_local_provider",
                        lambda *a, **k: _FakeProvider(0))
    out = la._provider_chat_turn([{"role": "user", "content": "x"}])
    assert out["content"] == "hi"
    assert la._measured_chars_per_token is None


# ---------------------------------------------------------------- S2

def test_evicts_tool_output_before_dropping_assistant_turns():
    """Nearly all bytes are tool output; all the meaning is in the short
    assistant turns. Evicting output must keep every assistant decision."""
    messages = _head()
    for i in range(6):
        messages.append(_assistant_call("bash", {"command": f"cmd{i}"}))
        messages.append(_tool("X" * 5000))

    out = la._trim_resumed_transcript(messages, 8000)

    kept_calls = [m for m in out if m.get("tool_calls")]
    assert len(kept_calls) == 6, "assistant decisions must survive eviction"
    assert sum(la._message_char_len(m) for m in out) <= 8000


def test_eviction_preserves_head_of_each_tool_output():
    """The signal in a tool result is at the front ("ERROR: ... invalid
    Python syntax at line 106"). Eviction must keep that, not blank it."""
    messages = _head()
    messages.append(_assistant_call("create_file", {"path": "a.py"}))
    messages.append(_tool("ERROR: content for a.py has invalid Python syntax at line 9. " + "Z" * 9000))
    messages.append(_assistant_call("bash", {"command": "pytest"}))
    messages.append(_tool("Y" * 9000))

    out = la._trim_resumed_transcript(messages, 4000)

    joined = " ".join(str(m.get("content") or "") for m in out)
    assert "invalid Python syntax at line 9" in joined


def test_recent_tool_output_survives_eviction_verbatim():
    """The agent acts on its most recent result; evicting that one would
    break the very next turn. Eviction works oldest-first."""
    messages = _head()
    for i in range(8):
        messages.append(_assistant_call("bash", {"command": f"cmd{i}"}))
        messages.append(_tool(f"result-{i} " + "Q" * 4000))

    out = la._trim_resumed_transcript(messages, 12000)

    last_tool = [m for m in out if m.get("role") == "tool"][-1]
    assert "Q" * 4000 in str(last_tool["content"]), "newest output must stay verbatim"


def test_trim_is_noop_when_already_under_budget():
    """Boundary: nothing over budget means the list is returned untouched."""
    messages = _head() + [_assistant_call("bash", {"command": "ls"}), _tool("ok")]
    assert la._trim_resumed_transcript(messages, 100_000) is messages


# ---------------------------------------------------------------- S3

def _block(*messages):
    return list(messages)


def test_digest_names_files_written_in_the_span():
    """A dropped span's file writes are mechanically recoverable; making the
    agent re-derive them is what drives the read-loop/park pathology."""
    blocks = [
        _block(_assistant_call("create_file", {"path": "rate_limiter.py"}),
               _tool("created rate_limiter.py")),
        _block(_assistant_call("str_replace", {"path": "test_rate_limiter.py"}),
               _tool("edited")),
    ]
    digest = la._dropped_span_digest(blocks)
    assert "rate_limiter.py" in digest
    assert "test_rate_limiter.py" in digest


def test_digest_does_not_list_files_that_were_only_read():
    """view_file is not a mutation. Listing a merely-inspected file as
    modified is the same class of false fact the LLM summary produced."""
    blocks = [_block(_assistant_call("view_file", {"path": "untouched.py"}),
                     _tool("...contents..."))]
    digest = la._dropped_span_digest(blocks)
    assert "untouched.py" not in digest


def test_digest_reports_last_test_result_from_the_span():
    """"Did the tests pass" is the fact the compaction test showed a local
    model inverting most often. Read it off the span instead of asking."""
    blocks = [_block(_assistant_call("bash", {"command": "pytest -q"}),
                     _tool("collected 12 items\n3 failed, 9 passed"))]
    digest = la._dropped_span_digest(blocks)
    assert "3 failed" in digest


def test_digest_reports_the_failure_not_the_eviction_marker():
    """The digest reads blocks AFTER eviction, so a long test log has already
    been truncated with a trailing marker. Taking the tail naively surfaces
    the marker instead of the failure summary - which is the one line the
    digest exists to carry."""
    evicted = ("=== FAILURES ===\ntest_available_tokens_reports_full_at_start\n"
               "[... 435 chars of this tool output evicted to fit the context window ...]")
    blocks = [_block(_assistant_call("bash", {"command": "pytest -q"}), _tool(evicted))]
    digest = la._dropped_span_digest(blocks)
    assert "FAILURES" in digest
    assert "evicted to fit the context window" not in digest


def test_digest_omits_sections_it_has_no_evidence_for():
    """Never assert a fact the span does not show - the exact failure mode
    the LLM-summary experiment exhibited (claiming a passing suite that had
    actually errored)."""
    blocks = [_block(_assistant_call("view_file", {"path": f"f{i}.py"}),
                     _tool("...")) for i in range(4)]
    digest = la._dropped_span_digest(blocks)
    assert digest == "", "no writes, no commands, no test run -> nothing to assert"


def test_digest_reaches_the_transcript_when_blocks_are_dropped():
    """Integration: with a budget too tight for eviction alone, the block-drop
    tier must fire AND carry the digest into the surviving transcript."""
    messages = _head()
    messages.append(_assistant_call("create_file", {"path": "rate_limiter.py"}))
    messages.append(_tool("created rate_limiter.py"))
    for i in range(10):
        messages.append(_assistant_call("bash", {"command": f"filler{i}"}))
        messages.append(_tool("Z" * 12000))

    out = la._trim_resumed_transcript(messages, 1500)

    note = " ".join(str(m.get("content") or "") for m in out)
    assert "were dropped from this transcript" in note, "block-drop tier must fire"
    assert "rate_limiter.py" in note, "digest must survive into the transcript"
    assert "wrote" not in note.lower(), "no file was written; must not claim one"


# ---------------------------------------------------------------- S4

def _err(status, body):
    req = httpx.Request("POST", "http://localhost:1234/v1/chat/completions")
    resp = httpx.Response(status, text=body, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


def test_lmstudio_overflow_400_is_classified_as_overflow():
    """LM Studio's real overflow body, captured from lmstudio-bug-tracker#237.
    Treated as a plain 4xx it kills the run; it is in fact trim-recoverable."""
    exc = _err(400, "Trying to keep the first 15857 tokens when context the "
                    "overflows. However, the model is loaded with context length "
                    "of only 4096 tokens, which is not enough.")
    assert la._is_context_overflow_error(exc) is True


def test_ordinary_400_is_not_classified_as_overflow():
    """A genuinely malformed request must keep failing fast - retrying and
    trimming it is pointless and would burn the step budget."""
    exc = _err(400, '{"error": "invalid role \'assistant_2\' in messages[3]"}')
    assert la._is_context_overflow_error(exc) is False


def test_openai_style_context_length_error_is_classified_as_overflow():
    """The OpenAI-compatible surface both lmstudio and mlx expose uses this
    error code; match it too rather than only LM Studio's prose."""
    exc = _err(400, '{"error": {"code": "context_length_exceeded", '
                    '"message": "This model\'s maximum context length is 4096 tokens"}}')
    assert la._is_context_overflow_error(exc) is True


def test_5xx_is_still_treated_as_overflow_candidate():
    """Ollama/llama.cpp return 500 for overflow - the original case must not
    regress now that classification is body-based."""
    exc = _err(500, "internal server error")
    assert la._is_context_overflow_error(exc) is True


# ---------------------------------------------------------------- S5

@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(la.time, "sleep", lambda s: slept.append(s))
    return slept


def test_recovery_backs_off_between_rounds(no_sleep, monkeypatch):
    """Three back-to-back requests is the worst response to a load-induced
    500. Rounds must be spaced."""
    monkeypatch.setattr(la, "_trim_resumed_transcript",
                        lambda m, c: list(m)[:-1] if len(m) > 1 else list(m))
    req = httpx.Request("POST", "http://x/api/chat")

    def chat_fn(messages):
        raise httpx.HTTPStatusError(
            "500", request=req, response=httpx.Response(500, request=req))

    la.recover_from_oversized_5xx(
        [{"role": "user", "content": f"m{i}"} for i in range(6)], chat_fn, step=1)

    assert len(no_sleep) >= 2, "expected a pause between escalation rounds"
    assert no_sleep == sorted(no_sleep), "backoff should not shrink"


def test_unshrinkable_payload_retries_unchanged_instead_of_giving_up(no_sleep, monkeypatch):
    """If the trim cannot shrink the payload, the 500 was not an overflow -
    which is precisely when waiting works. The old code returned None here
    and killed the run (2026-07-30, ~95%-complete)."""
    monkeypatch.setattr(la, "_trim_resumed_transcript", lambda m, c: list(m))
    req = httpx.Request("POST", "http://x/api/chat")
    calls = {"n": 0}

    def chat_fn(messages):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.HTTPStatusError(
                "500", request=req, response=httpx.Response(500, request=req))
        return {"role": "assistant", "content": "recovered"}

    out = la.recover_from_oversized_5xx(
        [{"role": "user", "content": "m"}], chat_fn, step=1)

    assert out == {"role": "assistant", "content": "recovered"}
    assert calls["n"] >= 2, "must retry unchanged rather than bail on first failure"


def test_recovery_still_propagates_4xx(no_sleep, monkeypatch):
    """A non-overflow 4xx during escalation is a real bad request; it must
    surface rather than be swallowed into another retry round."""
    monkeypatch.setattr(la, "_trim_resumed_transcript",
                        lambda m, c: list(m)[:-1] if len(m) > 1 else list(m))

    def chat_fn(messages):
        raise _err(400, '{"error": "malformed"}')

    with pytest.raises(httpx.HTTPStatusError):
        la.recover_from_oversized_5xx(
            [{"role": "user", "content": f"m{i}"} for i in range(4)], chat_fn, step=1)


def test_main_routes_overflow_400_to_escalation_not_death():
    """Wiring guard. The classifier is useless unless main()'s error branch
    consults it: the old `status_code < 500` test sent LM Studio's overflow
    400 straight to `return 1`. Asserted against the production source so a
    future edit that reverts the call site fails here, not silently in a
    dispatch."""
    for name in ("local_agent.py", "local_agent_oracle.py"):
        src = (Path(__file__).parent.parent.parent / "scripts" / name).read_text()
        branch = src[src.index("            m = chat(messages)"):]
        branch = branch[:branch.index("recover_from_oversized_5xx")]
        # Comments in this span legitimately quote the old test; judge code only.
        code = "\n".join(line for line in branch.splitlines()
                         if not line.strip().startswith("#"))
        assert "_is_context_overflow_error(e)" in code, (
            f"{name}: main()'s HTTPStatusError branch must classify by body")
        assert "status_code < 500" not in code, (
            f"{name}: bare status-code test still gates the escalation path")


def test_oracle_copy_stays_in_sync():
    """local_agent_oracle.py is a verbatim copy that runs the production path
    for acceptance-bearing dispatches. A fix applied to only one copy is a
    known recurring defect class in this repo."""
    oracle = (Path(__file__).parent.parent.parent / "scripts"
              / "local_agent_oracle.py").read_text()
    for symbol in ("_evict_tool_outputs", "_dropped_span_digest",
                   "_is_context_overflow_error", "_tool_call_pairs",
                   "_EVICT_KEEP_RECENT"):
        assert f"{symbol}" in oracle, f"oracle copy is missing {symbol}"
    # LAO-RECOVERY moved the 5xx-recovery constants into the recovery twin
    # module; the oracle script's only remaining mention is its migration
    # comment, so this check is definition-level (ast) against that module -
    # a substring scan would pass off the comment alone.
    import ast
    recovery_src = (Path(__file__).parent.parent.parent / "scripts"
                    / "local_agent_oracle_recovery.py").read_text()
    tree = ast.parse(recovery_src)
    defs = {t.id for n in tree.body if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)}
    assert "RECOVERY_BACKOFF_SECONDS" in defs, (
        "recovery module must define RECOVERY_BACKOFF_SECONDS at module level")
    assert "_RECOVERY_ROUNDS" in defs, (
        "recovery module must define _RECOVERY_ROUNDS at module level")


def test_recovery_gives_up_after_bounded_rounds(no_sleep, monkeypatch):
    """Bounded, not infinite: a persistently-dead backend must still return
    None so the caller can WIP-commit and exit rather than spin forever."""
    monkeypatch.setattr(la, "_trim_resumed_transcript", lambda m, c: list(m))
    req = httpx.Request("POST", "http://x/api/chat")
    calls = {"n": 0}

    def chat_fn(messages):
        calls["n"] += 1
        raise httpx.HTTPStatusError(
            "500", request=req, response=httpx.Response(500, request=req))

    out = la.recover_from_oversized_5xx(
        [{"role": "user", "content": "m"}], chat_fn, step=1)

    assert out is None
    assert calls["n"] <= 6, "escalation must be bounded"
