"""Chat/transport impls extracted from the local dispatch agent module (LA-CHAT).

The functions below were moved VERBATIM from the agent module
(_effective_chars_per_token, _stream_one_turn, _provider_chat_turn,
_ollama_payload, chat, _repair_triple_quoted_strings, recover_tool_calls),
renamed to ``<name>_impl`` with ``origin`` added as the first parameter.

Why ``origin``: the agent module is file-execed under MULTIPLE module
names in one pytest process (the shared test helper's "local_agent",
test_dropped_top_level_vars' "local_agent_dropped_vars", the acceptance
fixtures' per-param variants). A proxy resolving one canonical sys.modules
name cannot route to the right instance. Each delegating wrapper in the
agent module therefore passes its own module's ``globals()`` dict,
and every agent-module-owned free variable below is read as
``origin["NAME"]`` at call time. ``monkeypatch.setattr(mod, "NAME", fake)``
writes into that same dict, so both reads AND re-binds land on the instance
the test actually patched (the refined variant of pipeline/service.py's
``_ServerRef`` call-time-resolution pattern).

Calibration contract: _stream_one_turn_impl and _provider_chat_turn_impl
write the agent module's calibration globals through ``origin`` — dict
assignments, not locals — on every turn that reports a prompt_eval_count,
so the tests that reset/read ``la._measured_chars_per_token`` /
``la._last_prompt_eval_count`` and the resident _main_impl's reset at
dispatch start all stay on the same dict. A turn that reports NO count
performs no write, preserving the prior calibration.

This module imports ONLY the stdlib (+ the shared recover_tool_calls parser
from pipeline.local_agent_common, exactly as the agent module imports it) —
it must never import the agent module or its config module (no cycles, ever).
"""
from __future__ import annotations

import json
import re
import sys  # noqa: F401 (kept for parity with the moved cluster's module context; tests patch module attrs on la, not here)
import time

import httpx

from pipeline.local_agent_common import recover_tool_calls as _recover_tool_calls_shared

# Rough chars-per-token estimate (no tokenizer available here). Observed live
# 2026-07-20: a resumed transcript sat at ~129544 chars / ~32386 tokens
# (~4.0 chars/token) right before hitting NUM_CTX=32768 and truncating -
# llama.cpp then returned a 500 on every retry (the truncated request is
# identical each time, so CHAT_MAX_ATTEMPTS's retry can never help). A rework
# resume reuses the ENTIRE prior transcript and appends more (reviewer
# feedback, a tech-lead fix checklist) with no bound, so repeated rework
# cycles on the same story compound: story 93fdc371 died this way on its
# 2nd AND 3rd rework attempts, both times on the model's very first turn.
_CHARS_PER_TOKEN_ESTIMATE = 4


def _effective_chars_per_token_impl(origin) -> float:
    """The live-calibrated chars/token ratio when available, else the fixed
    _CHARS_PER_TOKEN_ESTIMATE guess."""
    return origin["_measured_chars_per_token"] or _CHARS_PER_TOKEN_ESTIMATE


def _stream_one_turn_impl(origin, payload):
    """One streamed chat turn against Ollama's /api/chat. Accumulates the
    assistant message across newline-delimited JSON chunks and returns the
    assembled message dict ({role, content, tool_calls?}) — the same shape
    the non-streaming path returned via r.json()["message"], so main() and
    recover_tool_calls() work unchanged.

    Streaming lets the per-chunk read timeout (READ_SILENCE_SECONDS) fire
    only on a genuine stall (no bytes for N seconds), not on a legitimately
    long generation. A slow-but-progressing gen streams a chunk every ~1-2s
    and never trips it.

    Raises httpx.HTTPStatusError on a bad response (4xx/5xx) or
    httpx.TransportError (TimeoutException/ConnectError/ReadError) on a
    connect/read stall — chat() decides which of those are retryable.
    """
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls = None
    role = "assistant"
    prompt_eval_count = None
    with httpx.stream(
        "POST", f"{origin['ENDPOINT']}/api/chat", json=payload,
        timeout=httpx.Timeout(connect=origin["CONNECT_TIMEOUT_SECONDS"], read=origin["READ_SILENCE_SECONDS"],
                              write=10.0, pool=10.0),
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = chunk.get("message") or {}
            if msg.get("role"):
                role = msg["role"]
            if msg.get("content"):
                content_parts.append(msg["content"])
            if msg.get("thinking"):
                thinking_parts.append(msg["thinking"])
            # Tool calls may land at the top level of a chunk or inside its
            # message; capture from either. (devstral emits tool calls as
            # text content, so tool_calls stays None and recover_tool_calls
            # parses the assembled content downstream.)
            tc = chunk.get("tool_calls") or msg.get("tool_calls")
            if tc:
                tool_calls = tc
            if chunk.get("done"):
                prompt_eval_count = chunk.get("prompt_eval_count")
                break
    content = "".join(content_parts)
    # Reasoning models (e.g. gpt-oss:20b) stream their chain-of-thought in a
    # separate `thinking` field and may leave `content` entirely empty for a
    # turn. Fall back to the assembled thinking text ONLY when there is no
    # real content at all — never append it alongside genuine content, since
    # that would leak raw reasoning traces into recover_tool_calls() parsing
    # and downstream commit messages/logs.
    if not content.strip() and thinking_parts:
        content = "".join(thinking_parts)
    assembled = {"role": role, "content": content}
    if tool_calls:
        assembled["tool_calls"] = tool_calls
    # Calibrate the chars/token ratio from ollama's own real count for this
    # request — see _measured_chars_per_token's docstring for why the fixed
    # estimate alone is not trustworthy.
    if prompt_eval_count:
        origin["_last_prompt_eval_count"] = prompt_eval_count
        # The tools schema is part of every prompt and is counted in
        # prompt_eval_count, so it must be counted in the numerator too -
        # omitting it biases the ratio badly low early in a run, when the
        # ~3.4KB schema dominates a still-small transcript (measured: 0.67
        # vs a true ~2.35, shrinking the reactive-5xx trim budget ~3.5x more
        # than needed). Chat-template scaffolding is still unaccounted for,
        # which leaves a small residual bias in the same safe (over-trim)
        # direction, and shrinks as the transcript grows.
        sent_chars = (
            sum(origin["_message_char_len"](m) for m in payload.get("messages", []))
            + len(json.dumps(payload.get("tools") or []))
        )
        if sent_chars > 0:
            origin["_measured_chars_per_token"] = sent_chars / prompt_eval_count
    return assembled


def _provider_chat_turn_impl(origin, messages):
    """One provider-backed chat turn for a non-Ollama PROVIDER (lmstudio,
    mlx). Blocking, not streamed — these servers are OpenAI-compatible and
    stream tool calls as index-based deltas that need reassembly across
    chunks, a materially different (and riskier) parser than Ollama's
    whole-message-per-chunk NDJSON; deferred, see
    MODEL_PROVIDER_ABSTRACTION_PLAN.md S3. Bounded by TIMEOUT (the overall
    dispatch wall-clock budget) rather than a per-chunk silence timeout.
    Returns just the assembled message dict — the same shape
    _stream_one_turn returns — so chat()/main() work unchanged regardless of
    which provider is active.

    Records prompt_eval_count/calibration the same way _stream_one_turn does.
    This return shape stays the bare message, but the usage fields must NOT be
    discarded: both LMStudioProvider.chat and MLXProvider.chat already map the
    OpenAI `usage` block into prompt_eval_count, and dropping it left
    _last_prompt_eval_count permanently None on those providers — which is the
    condition main()'s proactive trim is gated on, so the primary overflow
    defense never fired at all outside Ollama (2026-08-07 audit).
    """
    envelope = origin["inference_providers"].get_local_provider().chat(
        messages, model=origin["MODEL"], num_ctx=origin["NUM_CTX"], temperature=origin["TEMPERATURE"],
        tools=origin["TOOLS"], endpoint=origin["ENDPOINT"], timeout=origin["TIMEOUT"],
    )
    prompt_eval_count = envelope.get("prompt_eval_count")
    if prompt_eval_count:
        origin["_last_prompt_eval_count"] = prompt_eval_count
        # Same numerator as _stream_one_turn: the tools schema is part of
        # every prompt and is counted in prompt_eval_count, so it belongs in
        # the chars total too.
        sent_chars = (
            sum(origin["_message_char_len"](m) for m in messages)
            + len(json.dumps(origin["TOOLS"]))
        )
        if sent_chars > 0:
            origin["_measured_chars_per_token"] = sent_chars / prompt_eval_count
    return envelope["message"]


def _ollama_payload_impl(origin, messages):
    """Build the Ollama /api/chat request body for one turn.

    Extracted from chat() so the Qwen3/gemma4 thinking-mode flag (THINK) is
    unit-testable without an HTTP boundary. `think` is included only when
    LOCAL_AGENT_THINK is explicitly "true"/"false" (bool) or one of the
    graded-reasoning levels (passed through verbatim as a string) — omitted
    otherwise so models with no tuned opinion get an unchanged request body
    (see THINK's comment).
    """
    payload = {"model": origin["MODEL"], "messages": messages, "tools": origin["TOOLS"], "stream": True,
               "options": {"num_ctx": origin["NUM_CTX"], "temperature": origin["TEMPERATURE"]}}
    if origin["THINK"] in ("true", "false"):
        payload["think"] = (origin["THINK"] == "true")
    elif origin["THINK"] in origin["_THINK_LEVELS"]:
        payload["think"] = origin["THINK"]
    return payload


def chat_impl(origin, messages):
    """One LLM turn, with retry.

    A single transient stall (queue contention, network blip, 5xx, 429)
    must not kill a 30-minute run. For PROVIDER == "ollama" (the default) we
    stream so a slow generation doesn't trip the timeout; other providers go
    through _provider_chat_turn's blocking call instead (see its docstring).
    Either way, retry covers the transient failures: 4xx is a bad request
    (retrying won't help) so it raises immediately; 5xx, transport errors
    (timeout/connect/read), and RateLimitedError (429) are retried up to
    CHAT_MAX_ATTEMPTS. If every attempt fails, the last exception propagates
    to main()'s except, which commits WIP and returns 1 — same terminal
    behavior as before, but only after we've genuinely tried.
    """
    payload = origin["_ollama_payload"](messages)
    last_exc: Exception | None = None
    for attempt in range(1, origin["CHAT_MAX_ATTEMPTS"] + 1):
        try:
            if origin["PROVIDER"] == "ollama":
                return origin["_stream_one_turn"](payload)
            return origin["_provider_chat_turn"](messages)
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise  # 4xx — bad request, retrying is pointless
            last_exc = e
        except httpx.TransportError as e:
            last_exc = e  # timeout / connect / read — transient, retry
        except origin["inference_providers"].RateLimitedError as e:
            last_exc = e  # 429 — transient, retry like a 5xx
        if attempt < origin["CHAT_MAX_ATTEMPTS"]:
            time.sleep(origin["CHAT_RETRY_BACKOFF"] * attempt)
    assert last_exc is not None  # loop ran ≥1 attempt; only reachable w/ an exc
    raise last_exc


def _repair_triple_quoted_strings_impl(origin, candidate):
    """Rewrite Python-style triple-quoted string literals (\"\"\"...\"\"\" or
    '''...''') as JSON-encoded strings. Weaker local models emit multi-line
    code arguments (a str_replace's new_str/old_str) using Python triple-quote
    syntax with literal newlines, which is not valid JSON - json.loads rejects
    it at the opening \"\"\", so the tool call is silently dropped and the
    edit never lands (observed systematically with Qwen2.5-Coder-14B-4bit on
    mlx: 12/12 dropped calls, see MLX_DEFAULT_PROVIDER_PLAN.md). json.dumps of
    the inner text produces a correctly-escaped JSON string in its place."""
    def _sub(m):
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        return json.dumps(inner)
    return re.sub(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'', _sub, candidate, flags=re.DOTALL)


def recover_tool_calls_impl(origin, content):
    """Pull a tool call out of message text when the native field is empty.

    Delegates to the shared parser in local_agent_common, binding this
    file's own diverged _repair_triple_quoted_strings as the injectable
    repair fallback (see that module's _loads_tolerant docstring for why
    the repair function isn't imported alongside it)."""
    return _recover_tool_calls_shared(
        content, lambda candidate: _repair_triple_quoted_strings_impl(origin, candidate))