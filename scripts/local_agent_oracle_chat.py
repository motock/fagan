"""Chat/transport + token-estimate impls extracted from the oracle agent
module (LAO-CHAT, twin of LA-CHAT).

The functions below were moved VERBATIM from scripts/local_agent_oracle.py
(_CHARS_PER_TOKEN_ESTIMATE, _effective_chars_per_token, _stream_one_turn,
_provider_chat_turn, chat, _repair_triple_quoted_strings, _loads_tolerant,
recover_tool_calls), renamed to ``<name>_impl`` with ``origin`` added as the
first parameter.

Why ``origin``: the oracle module is file-execed under MULTIPLE module
names in one pytest process (the shared test helper's "local_agent_oracle",
plus the acceptance fixtures' per-param variants). A proxy resolving one
canonical sys.modules name cannot route to the right instance. Each
delegating wrapper in the oracle module therefore passes its own module's
``globals()`` dict, and every oracle-module-owned free variable below is
read as ``origin["NAME"]`` at call time. ``monkeypatch.setattr(mod, "NAME",
fake)`` writes into that same dict, so both reads AND re-binds land on the
instance the test actually patched (the refined variant of
pipeline/service.py's ``_ServerRef`` call-time-resolution pattern).

Calibration contract: _stream_one_turn_impl and _provider_chat_turn_impl
write the oracle module's calibration globals through ``origin`` — dict
assignments, not locals — on every turn that reports a prompt_eval_count,
so the tests that reset/read ``lao._measured_chars_per_token`` /
``lao._last_prompt_eval_count`` and the resident _main_impl's reset at
dispatch start all stay on the same dict. A turn that reports NO count
performs no write, preserving the prior calibration. Nothing is cached in
THIS module's globals: after the oracle module's ``global``-statement reset
the next turn must fall back to _CHARS_PER_TOKEN_ESTIMATE, not to a stale
ratio left behind here.

Divergences from the la twin (never-verbatim, by design): this module has
no ollama-payload helper — the oracle's chat() builds the request body
inline — and ``_loads_tolerant`` lives HERE (it is pure; la's copy lives in
pipeline.local_agent_common instead). The twins never import each other.

This module imports ONLY the stdlib + httpx — it must never import the
oracle module or its config module (no cycles, ever).
"""
from __future__ import annotations

import json
import re
import time

import httpx

# Rough chars-per-token estimate (no tokenizer available here). Ported from
# local_agent.py - see that file's comment for the live incident (story
# 93fdc371, 2026-07-20) that motivated this. Keep both copies in sync.
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
    assembled = {"role": role, "content": "".join(content_parts)}
    if tool_calls:
        assembled["tool_calls"] = tool_calls
    # Calibrate the chars/token ratio from ollama's own real count for this
    # request. Ported from local_agent.py - keep both copies in sync.
    if prompt_eval_count:
        origin["_last_prompt_eval_count"] = prompt_eval_count
        # The tools schema is counted in prompt_eval_count, so it must be
        # counted here too - see local_agent.py's copy for the measured bias
        # this avoids. Keep both copies in sync.
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
    payload = {"model": origin["MODEL"], "messages": messages, "tools": origin["TOOLS"], "stream": True,
               "options": {"num_ctx": origin["NUM_CTX"], "temperature": origin["TEMPERATURE"]}}
    if origin["THINK"] in ("true", "false"):
        payload["think"] = (origin["THINK"] == "true")
    elif origin["THINK"] in origin["_THINK_LEVELS"]:
        payload["think"] = origin["THINK"]
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
    the inner text produces a correctly-escaped JSON string in its place.

    Kept in sync with scripts/local_agent.py's copy (this module is a verbatim
    port of that agent for oracle grading)."""
    def _sub(m):
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        return json.dumps(inner)
    return re.sub(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'', _sub, candidate, flags=re.DOTALL)


def _loads_tolerant_impl(origin, candidate):
    """json.loads, tolerating raw control characters inside strings, with a
    triple-quote repair pass as a further fallback. Valid JSON is never
    transformed - both fallbacks only ever ACCEPT more inputs than a strict
    parse would, never reinterpret one that already parses.

    strict=False (observed live, 2026-07-17, Qwen2.5-Coder-14B-4bit on mlx,
    interval_merge task, the benchmark harness this oracle agent is
    dispatched through): a distinct malformation from the triple-quote case
    below - the model uses ordinary double-quoted JSON string syntax for a
    multi-line create_file `content` argument, but embeds a RAW literal
    newline instead of escaping it as `\\n`. A strict parse rejects this
    ("Invalid control character"); the triple-quote repair does not apply
    (no triple quotes present), so the tool call was silently dropped every
    retry and the agent looped regenerating the same correct-but-unparseable
    content until the wall-clock park, with the real fix never landing.
    json.loads(strict=False) permits control characters (newlines, tabs,
    etc.) inside strings without weakening validation of anything else -
    it never accepts input a strict parse would reject, it only stops
    rejecting on this one class of already-well-structured input.

    Kept in sync with scripts/local_agent.py's copy (this module is a
    verbatim port of that agent for oracle grading)."""
    try:
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        pass
    repaired = _repair_triple_quoted_strings_impl(origin, candidate)
    if repaired != candidate:
        try:
            return json.loads(repaired, strict=False)
        except json.JSONDecodeError:
            pass
    return None


def recover_tool_calls_impl(origin, content):
    """Pull a tool call out of message text when the native field is empty."""
    if not content:
        return None
    text = content.strip().replace("[TOOL_CALLS]", "")
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    m = re.search(r"(\[\s*\{.*\}\s*\]|\{.*\})", text, re.DOTALL)
    if m:
        candidates.append(m.group(1))
    for c in candidates:
        obj = _loads_tolerant_impl(origin, c)
        if obj is None:
            continue
        items = obj if isinstance(obj, list) else [obj]
        out = [{"function": {"name": it["name"], "arguments": it.get("arguments", it.get("parameters", {}))}}
               for it in items if isinstance(it, dict) and "name" in it]
        if out:
            return out
    return None