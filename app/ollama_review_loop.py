"""Phase functions behind ``OllamaDriver._review_loop``.

The read-only review loop is split into: build the transcript, run one model
turn (chat, usage records, proactive trim, tool dispatch), then the
fail-closed tail (terminal-line salvage, final no-tools verdict call).

Names that tests patch (the helpers and constants imported by
``app.backend_ollama``) are looked up on that module at call time via
``_home`` rather than copied here, so a patch on ``app.backend_ollama`` is
seen by the moved code.
"""
from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app import backend_ollama as _home

if TYPE_CHECKING:
    from app.backend_ollama import OllamaDriver

_REVIEW_PREAMBLE = (
    "You are reviewing code in the current directory, READ-ONLY - do not "
    "edit anything. If the review request below already includes the full "
    "diff (marked '--- git diff ... ---'), do NOT re-run `git diff` to "
    "fetch it again — read it directly and use bash/view_file only for "
    "context not already shown (e.g. surrounding lines in a file, or a "
    "related file the diff doesn't touch). If it does NOT already include "
    "the diff, first use the bash tool to inspect the changes (e.g. `git "
    "diff`, `git log -p -1`), and view_file to read files. Then call "
    "submit_review exactly once with verdict APPROVE or REQUEST_CHANGES. "
    "Always call a tool; do not answer in prose.\n\n"
    "For a diff not already included above: bash output is truncated to "
    "3000 chars per call, so a bare `git diff` may silently cut off the "
    "end. Start with `git diff --stat` to see the scope, then use "
    "`git diff -- <file>` per file for the parts you need, and "
    "`view_file <path>` for surrounding context. Do NOT rely on a "
    "single `git diff` for a multi-file change."
)


@dataclass(frozen=True)
class ReviewContext:
    """Per-review values that travel together through the phases."""

    driver: OllamaDriver
    cwd: str
    cell_dir: str | None
    resolved_model: str
    num_ctx: int
    max_steps: int


@dataclass
class ReviewState:
    """Mutable cross-turn state of one review loop."""

    nudged: bool = False
    findings_nudged: bool = False
    last_prose: str = ""
    fallthrough_reason: str = "step cap exhausted"
    stopped: bool = False


def start_review(driver: OllamaDriver, model: str, cwd: str,
                 cell_dir: str | None) -> ReviewContext:
    """Resolve the model and context size, open the review log section."""
    resolved_model = _home._resolve_local_model(
        model, provider=getattr(driver.provider, "name", "ollama"))
    num_ctx = _home._tuned_num_ctx(resolved_model, driver.num_ctx, endpoint=driver.endpoint)
    _home._append_review_log(
        cwd,
        f"=== review cycle "
        f"{datetime.datetime.now(datetime.UTC).isoformat()} "
        f"model={resolved_model} ===\n",
    )
    # Re-read live from os.environ, mirroring dispatch()'s live re-read of
    # PIPELINE_LOCAL_MAX_STEPS (see its comment) rather than only using
    # the value captured once at __init__ time - otherwise a plist/env
    # edit to the review cap silently has no effect on a long-lived MCP
    # server process (2026-07-07 web-client-epic retro §7).
    max_steps = int(
        os.environ.get("PIPELINE_LOCAL_REVIEW_MAX_STEPS", str(driver.review_max_steps))
    )
    return ReviewContext(driver, cwd, cell_dir, resolved_model, num_ctx, max_steps)


def build_messages(prompt: str, system: str | None) -> list[dict]:
    """The initial system + user transcript."""
    system_content = _REVIEW_PREAMBLE + ("\n\n" + system if system else "")
    return [{"role": "system", "content": system_content},
            {"role": "user", "content": prompt}]


def run_review_steps(ctx: ReviewContext, state: ReviewState,
                     messages: list[dict]) -> str | None:
    """Run turns until a verdict is submitted (returned) or the loop stops."""
    for i in range(ctx.max_steps):
        verdict = _run_step(ctx, state, messages, i)
        if verdict is not None:
            return verdict
        if state.stopped:
            break
    return None


def _run_step(ctx: ReviewContext, state: ReviewState, messages: list[dict],
              i: int) -> str | None:
    # The local model investigates thoroughly but rarely converges to
    # the submit_review terminator on its own. It will call it when
    # pushed (the same model calls `done` in dispatch mode), so when the
    # step budget is nearly spent without a verdict, nudge to press for
    # one. Without this the loop exhausts into UNKNOWN, the orchestrator
    # records changes_requested with empty feedback, and the redispatched
    # agent re-runs blind -> review/rework loop -> park.
    #
    # Re-nudge on EVERY remaining step in the window, not just once: a
    # single nudge is easy for a weak local model to lose track of deep
    # in a long tool-calling transcript. gpt-oss exhausted the full
    # 20-step cap in 3 of 5 observed review cycles on 2026-07-04
    # (ratelimiter_inspect benchmark) despite the one-shot nudge firing
    # — it just kept calling view_file/bash past it with nothing
    # reinforcing the instruction for the remaining steps.
    remaining = ctx.max_steps - i
    if remaining <= 5:
        messages.append({"role": "user", "content":
            f"You have {remaining} review step(s) left. Stop investigating "
            "and call submit_review now with verdict APPROVE or "
            "REQUEST_CHANGES (or end your reply with a 'VERDICT: APPROVE' "
            "or 'VERDICT: REQUEST_CHANGES' line)."})
    # Snapshot what this turn actually sends, so the trim can calibrate
    # chars-per-token against the real prompt_eval_count this exact request
    # comes back with (the tools schema is part of every prompt and is
    # counted in prompt_eval_count, so it belongs in the numerator - see
    # scripts/local_agent.py's copy).
    sent_chars = (
        sum(_home._review_msg_chars(msg) for msg in messages)
        + len(json.dumps(ctx.driver._REVIEW_TOOLS))
    )
    try:
        envelope = _chat_turn(ctx, messages, i)
        m = envelope["message"]
        messages.append(m)
        _maybe_trim(ctx, messages, envelope, sent_chars)
        if m.get("content"):
            state.last_prose = m["content"]
            _home._append_review_log(ctx.cwd, m["content"] + "\n")
        tcs = (m.get("tool_calls") or _home._recover_tool_calls(m.get("content", ""))
               or _home._infer_review_tool_call(m.get("content", "")))
        if not tcs:
            _nudge_for_tool(state, messages)
            return None
        state.nudged = False
        return _run_tool_calls(ctx, state, messages, tcs, i)
    except RuntimeError:
        raise  # preserve the httpx.HTTPError -> RuntimeError contract
    except Exception:  # noqa: BLE001 (deliberate: any other per-step failure must fail safe rather than crash the harness, per the comment below)
        # Any other failure this step (malformed response shape, a bad
        # tool call, a read-only tool erroring) must not crash the
        # caller. Fail safe into the same "no verdict" path a
        # genuinely inconclusive review already takes, rather
        # than propagating and taking down the whole harness process.
        state.fallthrough_reason = "loop error"
        state.stopped = True
        return None


def _chat_turn(ctx: ReviewContext, messages: list[dict], i: int) -> dict:
    """One /api/chat call plus its usage records; returns the envelope."""
    driver = ctx.driver
    try:
        # _chat returns the full /api/chat envelope (not just the message),
        # so we can pull prompt_eval_count / eval_count / total_duration
        # for the per-call token-cost sidecar alongside the message body.
        envelope = driver._chat(messages, ctx.resolved_model,
                                tools=driver._REVIEW_TOOLS)
    except _home.inference_providers.RateLimitedError:
        raise  # 429 must reach the deferral path unwrapped
    except (_home.httpx.HTTPError, RuntimeError) as e:
        # A RuntimeError is _chat's own exhaustion raise (budget spent /
        # attempts used up); it must keep the review-specific wording so
        # callers matching on it are unaffected.
        raise RuntimeError(
            f"Local backend at {driver.endpoint} (model={ctx.resolved_model}) "
            f"is unreachable or errored during review: {e}"
        ) from e
    _home.usage_record.record_ollama_envelope(
        envelope, model=ctx.resolved_model, role="review")
    if ctx.cell_dir is not None:
        driver.record_token_usage(
            {
                "input_tokens": envelope.get("prompt_eval_count", 0),
                "output_tokens": envelope.get("eval_count", 0),
                "duration_ns": envelope.get("total_duration"),
                "model": ctx.resolved_model,
            },
            cell_dir=ctx.cell_dir, role="review", step=i,
        )
    return envelope


def _maybe_trim(ctx: ReviewContext, messages: list[dict], envelope: dict,
                sent_chars: int) -> None:
    # Proactive trim: once a turn's REAL measured prompt_eval_count
    # is already close to num_ctx, shrink the transcript now
    # rather than letting it grow into a 500/UNKNOWN-verdict on a
    # later turn. See _trim_review_transcript's docstring for the
    # live overflow rates this addresses.
    prompt_eval_count = envelope.get("prompt_eval_count")
    if not (
        prompt_eval_count
        and prompt_eval_count >= ctx.num_ctx * _home.REVIEW_PROACTIVE_TRIM_THRESHOLD
    ):
        return
    # Calibrate from THIS turn's real numbers rather than the
    # fixed guess. With the guess, the target could land above
    # the context window itself and the trim would not prevent
    # the overflow it exists to prevent: at num_ctx=16384, a
    # 4.0-based budget is 49152 chars, which at the live-
    # measured ~2.35 chars/token is ~20900 tokens - already
    # over the 16384 ceiling.
    chars_per_token = (
        sent_chars / prompt_eval_count
        if sent_chars > 0
        else _home._CHARS_PER_TOKEN_ESTIMATE
    )
    budget_chars = int(ctx.num_ctx * chars_per_token * 0.75)
    trimmed = _home._trim_review_transcript(messages, budget_chars)
    if len(trimmed) != len(messages):
        messages[:] = trimmed


def _nudge_for_tool(state: ReviewState, messages: list[dict]) -> None:
    """No tool call this turn: nudge once, then stop the loop."""
    if state.nudged:
        state.stopped = True  # still no tool after a nudge -> give up (-> salvage/park)
        return
    state.nudged = True
    messages.append({"role": "user", "content":
        "Call a tool (bash/view_file to investigate, or submit_review to finish). Do not reply in prose."})


def _run_tool_calls(ctx: ReviewContext, state: ReviewState, messages: list[dict],
                    tcs: list, i: int) -> str | None:
    """Execute the turn's tool calls; returns the verdict text once submitted."""
    for tc in tcs:
        try:
            fn = tc["function"]["name"]
            args = tc["function"]["arguments"]
        except (KeyError, TypeError):
            continue  # malformed tool-call shape -> skip, keep reviewing
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        _home._append_review_log(
            ctx.cwd, f"TOOL: {fn} {json.dumps(args, separators=(',', ':'), default=str)}\n"
        )
        if fn == "submit_review":
            verdict_text = _submit_review(ctx, state, messages, args, i)
            if verdict_text is not None:
                return verdict_text
            continue
        result = _home._run_readonly_tool(fn, args, Path(ctx.cwd))
        messages.append({"role": "tool", "content": result})
        _home._append_review_log(
            ctx.cwd, f"RESULT: {result[:_home._REVIEW_LOG_TRUNCATE]}\n")
    return None


def _submit_review(ctx: ReviewContext, state: ReviewState, messages: list[dict],
                   args: dict, i: int) -> str | None:
    """Handle a submit_review call; None means it was rejected for retry."""
    verdict = str(args.get("verdict", "")).upper()
    if verdict not in ("APPROVE", "REQUEST_CHANGES"):
        verdict = "REQUEST_CHANGES"  # malformed -> safe default
    body = args.get("pr_body") or args.get("summary", "")
    title = args.get("pr_title", "")
    # A REQUEST_CHANGES with no findings gives the
    # redispatched agent nothing to act on -> it reworks
    # blind. Reject it once and demand specifics; if the
    # model still gives nothing on the retry, fail closed
    # and accept it anyway rather than looping forever or
    # silently upgrading to APPROVE.
    if (verdict == "REQUEST_CHANGES" and not body.strip()
            and not state.findings_nudged):
        state.findings_nudged = True
        messages.append({"role": "tool", "content":
            "REQUEST_CHANGES rejected: no findings given. "
            "Call submit_review again with a summary that "
            "states the specific problems - which file, "
            "what is wrong, and what must change."})
        return None
    _home._append_review_log(ctx.cwd, f"VERDICT: {verdict}\n")
    if ctx.cell_dir is not None:
        # The verdict-bearing call is i (the same step the message was
        # already recorded for); write a second row tagged with verdict=
        # so the analysis script can attribute total cost to the final
        # call without re-deriving it from the transcript.
        ctx.driver.record_token_usage(
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "model": ctx.resolved_model,
            },
            cell_dir=ctx.cell_dir, role="review",
            step=i, verdict=verdict,
        )
    return f"VERDICT: {verdict}\n\n{title}\n{body}".strip()


def salvage_terminal_verdict(cwd: str, last_prose: str) -> str | None:
    """No submit_review within the step cap. Salvage ONLY an explicit terminal
    verdict line — the model's actual conclusion, written last. An inline
    mention of "VERDICT: APPROVE" earlier in the prose (the model echoing
    the convergence nudge, or describing what an approve would require) must
    NOT be trusted: _parse_verdict's match is unanchored, so returning such
    prose would false-positive an APPROVE and auto-merge unreviewed code
    (fail-open). Fail-closed: no terminal verdict line -> "" -> UNKNOWN ->
    park for a human/Claude rather than auto-merge. Return ONLY the terminal
    verdict line, not the full prose: _parse_verdict is an unanchored
    re.search that matches the FIRST "VERDICT:" substring, so returning
    prose with an inline "VERDICT: APPROVE" mention earlier and a terminal
    "VERDICT: REQUEST_CHANGES" line would parse to APPROVE and auto-merge
    unreviewed code (a second fail-open). The terminal line is the model's
    actual conclusion; handing just that to _parse_verdict leaves it nothing
    else to false-match on."""
    lines = [ln.strip() for ln in last_prose.splitlines() if ln.strip()]
    if lines and re.search(r"^VERDICT:\s*(APPROVE|REQUEST_CHANGES)\s*$",
                           lines[-1], re.IGNORECASE):
        _home._append_review_log(cwd, f"{lines[-1]}\n")
        return lines[-1]
    return None


def final_verdict_call(ctx: ReviewContext, messages: list[dict]) -> str | None:
    """Final no-tools verdict call: a model that exhausted the tool budget
    without ever writing a terminal VERDICT line still has its
    observations. Ask once, without tools, for the conclusion. Fail
    closed: any error or unparseable answer returns None, falling through
    to the existing no-verdict path."""
    try:
        final_env = ctx.driver._chat(
            messages + [{"role": "user", "content":
                "You have exhausted your tool budget. Based only on what you "
                "have already inspected, reply with exactly one line: "
                "`VERDICT: APPROVE` or `VERDICT: REQUEST_CHANGES`, followed "
                "by a one-paragraph justification."}],
            ctx.resolved_model,
        )
        final_text = (final_env.get("message", {}).get("content") or "").strip()
        final_match = re.search(
            r"^\s*`?VERDICT:\s*(APPROVE|REQUEST_CHANGES)`?\s*$",
            final_text, re.IGNORECASE | re.MULTILINE,
        )
        if final_match:
            final_verdict = final_match.group(1).upper()
            _home._append_review_log(ctx.cwd, f"FINAL-VERDICT: {final_verdict}\n")
            return f"VERDICT: {final_verdict}\n\n{final_text}"
    except Exception as exc:  # noqa: BLE001 - the final call must never break the review
        _home._append_review_log(
            ctx.cwd, f"final-verdict call failed ({type(exc).__name__})\n")
    return None
