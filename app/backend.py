"""LLM backend abstraction — the seam between orchestration and execution.

pipeline_mcp_server.py owns the *why* (state machine, gating, decisions). This
module owns the *how* (spawning a model to do the work). ClaudeCliDriver wraps
today's `claude` CLI subprocess calls with identical mechanics to what it
replaced; a future local-model driver implements the same Backend protocol so
the orchestrator never depends on which backend runs a given role.

OllamaDriver owns the *harness* mechanics for local models (the native-tool-
calling dispatch loop, the read-only review loop, checkpoint plumbing, the
cost sidecar) - mechanics shared by any local inference server. Which server
actually serves the model (Ollama today; LM Studio/MLX are documented stubs)
is a separate concern, selected via PIPELINE_LOCAL_PROVIDER - see
inference_providers.py.
"""
from __future__ import annotations

import datetime
import functools
import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol

import httpx

from app import inference_providers
from app.inference_providers import (
    RateLimitedError,  # noqa: F401 (re-exported: backend.RateLimitedError)
)

# Warn if operator mistakenly set transport-only env vars
_logger = logging.getLogger("pipeline")
for _var, _real in (
    ("LOCAL_AGENT_MAX_STEPS", "PIPELINE_LOCAL_MAX_STEPS"),
    ("LOCAL_AGENT_NUM_CTX", "PIPELINE_LOCAL_NUM_CTX"),
    ("LOCAL_AGENT_TEMPERATURE", "PIPELINE_LOCAL_TEMPERATURE"),
    ("PIPELINE_TRANSPORT_NUM_CTX", "PIPELINE_LOCAL_NUM_CTX"),
    ("PIPELINE_TRANSPORT_TEMPERATURE", "PIPELINE_LOCAL_TEMPERATURE"),
    ("PIPELINE_TRANSPORT_MAX_STEPS", "PIPELINE_LOCAL_MAX_STEPS"),
):
    if _var in os.environ:
        _logger.warning(
            f"{_var} is set in the environment but is a transport-only value that backend.py overwrites on every dispatch - it has no effect as an input; "
            f"set {_real} instead."
        )
_logger = logging.getLogger("pipeline")
@dataclass
class AgentHandle:
    """A non-blocking agentic run (dispatch/review-style), identified by pid."""
    pid: int
    # The concrete model the agent actually boots with — for the local backend
    # this is the RESOLVED model (a logical tier like "sonnet" maps to e.g.
    # "minimax-m3:cloud" via PIPELINE_LOCAL_MODEL_DEFAULT), for the Claude
    # backend it's the model string passed verbatim. The orchestrator records
    # this on the manifest so the dashboard shows what really ran, not the
    # plan's declared tier. None for backends that don't surface it.
    model: str | None = None


class Backend(Protocol):
    def complete(
        self, prompt: str, *, system: str | None, model: str,
        allowed_tools: str | None = None, cwd: str | None = None,
        max_tokens: int | None = None,
        cell_dir: str | None = None,
    ) -> str:
        """Run a blocking, single invocation and return its captured output.

        `max_tokens` is accepted for signature parity across drivers but not
        acted on by any of them: the `claude` CLI dropped `--max-tokens` (see
        ClaudeCliDriver.complete), and Ollama-backed drivers cap the response
        via the model's own context window instead.

        `cell_dir`, when set, is the per-cell directory the caller is running
        in (the benchmark's <run>/<task>__<model>__t<trial>/ path). Drivers
        with structured per-call usage data (Ollama's response envelope,
        Claude's --output-format json) append a JSONL record to
        `<cell_dir>/review_token_costs.jsonl` so the data survives the
        worktree cleanup that wipes the live review.log. Drivers that can't
        extract structured usage silently skip the sidecar.
        """
        ...

    def dispatch(
        self, prompt: str, *, system: str | None, model: str,
        allowed_tools: str | None, cwd: Path, log_path: Path, append: bool,
        acceptance: list[str] | None = None,
        resume_transcript_path: Path | None = None,
        resume_append_content: str | None = None,
        rework_full_suite: bool = False,
    ) -> AgentHandle:
        """Spawn a non-blocking agentic run, streaming output to log_path."""
        ...

    def record_token_usage(
        self, usage: dict, *, cell_dir: str | None = None,
        role: str = "review", step: int | None = None,
        verdict: str | None = None,
    ) -> None:
        """Append a per-call token-usage record to the cell's sidecar.

        Default no-op so drivers without structured usage data don't have
        to implement anything. Drivers that do have it (Ollama, Claude)
        override to JSONL-append to <cell_dir>/review_token_costs.jsonl
        and swallow OSError so a failed write never breaks the review
        loop. The schema is documented on the concrete drivers.
        """
        ...

    def usage_probe_text(self) -> str:
        """Return the raw usage/cost report text for the resource gate."""
        ...

    def resource_status(self) -> dict:
        """Whether this backend is resource-available to take work right now.

        Returns {"ok": bool, "reason": str}. The orchestrator's per-role gate
        (advance_pipeline) consults the backend serving each role, so a limit
        on one backend (e.g. Claude's weekly usage) no longer freezes work on
        another (e.g. local dispatch). A future vLLM/cloud driver defines its
        own check here without touching the orchestrator.
        """
        ...


# Vars that can redirect the `claude` CLI off the first-party Anthropic API
# (Bedrock/Vertex, a custom ANTHROPIC_BASE_URL, or an injected auth token/key)
# — see `claude --help`'s "3P providers" section. With no `env=` passed to a
# `claude` subprocess call, it inherits the caller's full environment; if the
# invoking shell (or the scheduler's) has one of these exported, every
# "claude"-backend call (dispatch/review/overlord) silently rides it while
# the audit sidecar still reports "backend": "claude". Stripped by default
# from every ClaudeCliDriver subprocess call; PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV
# opts back into full inheritance for legitimate enterprise Bedrock/Vertex
# deployments.
_CLAUDE_PROVIDER_REDIRECT_VARS = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
)


def _first_party_claude_env() -> dict:
    """Environment for a `claude` subprocess call, isolated from provider
    redirects unless PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV explicitly restores
    full inheritance (deny-by-default, per Secure by Design)."""
    if os.environ.get("PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV", "").strip():
        return dict(os.environ)
    env = dict(os.environ)
    for var in _CLAUDE_PROVIDER_REDIRECT_VARS:
        env.pop(var, None)
    return env


class ProviderIdentityMismatch(RuntimeError):
    """The `claude` CLI's own JSON payload reports a served model that does
    not match the requested tier - T1's env strip was bypassed (e.g. via
    PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV) or a redirect var outside the known
    set got through. Loud by design: a silent divergence here means review/
    dispatch/overlord decisions were made by a different model than the one
    requested."""


# Only tiers with a known Anthropic model-name prefix are checked; a custom
# or unrecognized tier string has no expected prefix to compare against, so
# verification is skipped for it (fail open on the unknown, not a guess).
_CLAUDE_TIER_MODEL_PREFIXES = {
    "opus": "claude-opus-",
    "sonnet": "claude-sonnet-",
    "haiku": "claude-haiku-",
}


# Cached result of ClaudeCliDriver.verify_identity(), module-scoped for the
# process's lifetime — mirrors the usage-gate's own cached, poller-fed state
# (see resource_status()'s docstring) rather than re-probing on every call.
# None means "never checked yet"; resource_status() must fail OPEN on that,
# not treat an unchecked identity as a failure.
_claude_identity_status: dict | None = None


class ClaudeCliDriver:
    """Backend driver wrapping the `claude` CLI."""

    def complete(
        self, prompt: str, *, system: str | None = None, model: str,
        allowed_tools: str | None = None, cwd: str | None = None,
        max_tokens: int | None = None, cell_dir: str | None = None,
    ) -> str:
        cmd = ["claude", "-p", prompt, "--model", model]
        if system:
            cmd += ["--append-system-prompt", system]
        if allowed_tools:
            cmd += ["--allowedTools", allowed_tools]
        # `--max-tokens` was removed from the `claude` CLI (this project pins
        # v2.1.202+, which only exposes `--max-budget-usd`); passing it makes
        # the CLI exit 1 with an empty stdout, which callers silently read as
        # "" -> _parse_verdict returns UNKNOWN. max_tokens is accepted for
        # backward-compat signature parity with OllamaDriver.complete() (Ollama
        # caps via num_ctx, not a CLI flag) but is otherwise unused here.
        del max_tokens
        # When cell_dir is set, switch to --output-format json so we can
        # extract per-call usage (input_tokens, output_tokens,
        # total_cost_usd, duration_ms) and append it to the cell's
        # token-cost sidecar. Falls back to text output for any caller that
        # doesn't pass cell_dir (the overlord path, ad-hoc single-shots).
        if cell_dir is not None:
            cmd += ["--output-format", "json"]
        proc = subprocess.run(  # noqa: PLW1510 (check=False would break test fakes with fixed signatures; see backend.py test suite)
            cmd, cwd=cwd, capture_output=True, text=True,
            env=_first_party_claude_env(),
        )
        if cell_dir is None:
            # Fail closed on CLI transport errors. A non-zero returncode or
            # an "API Error:" body (e.g. "API Error: Connection closed
            # mid-response...") is not valid output - returning it as-is
            # would feed garbage to callers (the planner would hand the
            # executor an error string as its tech-lead checklist, observed
            # live 2026-07-20). Raise so _run_planner's fails-open-to-None
            # guard catches it instead. _invoke_overlord has no such guard,
            # so the overlord path will now raise on a transport error - an
            # acceptable, surfacing behavior change (a dead CLI call should
            # not silently produce a decision).
            if proc.returncode != 0 or "API Error:" in (proc.stdout or ""):
                detail = (proc.stdout or getattr(proc, "stderr", "") or "").strip()
                raise RuntimeError(
                    f"claude CLI call failed (returncode={proc.returncode}): "
                    f"{detail[:500]}"
                )
            return proc.stdout
        # Structured path: parse the JSON envelope, record usage, return
        # just the `result` field so callers (and _parse_verdict) see the
        # same text they would have seen without --output-format json.
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # Defensive: if the CLI somehow returned non-JSON despite
            # --output-format json, fall back to raw stdout so the caller's
            # verdict parser still has something to scan. No usage record
            # is written in that case.
            return proc.stdout
        result_text = payload.get("result", proc.stdout)
        usage = payload.get("usage", {}) or {}
        self.record_token_usage(
            {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                "total_cost_usd": payload.get("total_cost_usd"),
                "duration_ms": payload.get("duration_ms"),
                "model": model,
                "served_model": payload.get("model"),
            },
            cell_dir=cell_dir, role="complete",
        )
        expected_prefix = _CLAUDE_TIER_MODEL_PREFIXES.get(model)
        served_model = payload.get("model")
        if expected_prefix and served_model and not served_model.startswith(expected_prefix):
            raise ProviderIdentityMismatch(
                f"requested tier {model!r} but backend served {served_model!r}"
            )
        return result_text

    def record_token_usage(
        self, usage: dict, *, cell_dir: str | None = None,
        role: str = "review", step: int | None = None,
        verdict: str | None = None,
    ) -> None:
        """Append a Claude usage record to <cell_dir>/review_token_costs.jsonl.

        Best-effort: any OSError (unwritable cell_dir, missing parent,
        review_token_costs.jsonl pre-empted by a directory) is swallowed
        so a failed sidecar write never breaks the review loop - same
        pattern as _append_review_log for the prose transcript.
        """
        if cell_dir is None:
            return
        try:
            from datetime import datetime, timezone
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "backend": "claude",
                "model": usage.get("model", "?"),
                "served_model": usage.get("served_model"),
                "role": role,
                "step": step,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                "total_cost_usd": usage.get("total_cost_usd"),
                "duration_ms": usage.get("duration_ms"),
                "duration_ns": None,
                "verdict": verdict,
            }
            with open(Path(cell_dir) / "review_token_costs.jsonl", "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass

    def dispatch(
        self, prompt: str, *, system: str | None = None, model: str,
        allowed_tools: str | None = None, cwd: Path, log_path: Path, append: bool,
    ) -> AgentHandle:
        # stream-json (+ the verbose it requires) makes claude emit an event
        # immediately on startup and one per tool call, instead of buffering
        # everything until the final answer. check_story_status's "0 bytes
        # after exit -> failed launch" check depends on that: without
        # streaming, a long-running-but-legitimate agent looks identical to
        # one that never started.
        cmd = ["claude", "-p", prompt, "--model", model,
               "--output-format", "stream-json", "--verbose"]
        if system:
            cmd += ["--append-system-prompt", system]
        if allowed_tools:
            cmd += ["--allowedTools", allowed_tools]
        with open(log_path, "a" if append else "w") as log_file:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=_first_party_claude_env(),
                stdout=log_file, stderr=log_file,
            )
        return AgentHandle(pid=proc.pid, model=model)

    def usage_probe_text(self) -> str:
        proc = subprocess.run(
            ["claude", "-p", "/cost", "--output-format", "json"],
            capture_output=True, text=True, check=True,
            env=_first_party_claude_env(),
        )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Usage probe returned invalid JSON: {e}") from e
        return payload.get("result", "")

    def verify_identity(self) -> dict:
        """Confirm the `claude` CLI genuinely serves Anthropic Claude and not
        a 3rd-party-provider redirect that got past T1 (e.g. via the
        PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV escape hatch, or a redirect var
        outside T1's known set). Runs the cheapest possible call once per
        process and caches the result at module scope — see
        `_claude_identity_status`'s docstring. A malformed/unreadable
        response fails open (ok: True): this preflight can only report a
        confirmed mismatch, not prove a negative.
        """
        global _claude_identity_status
        if _claude_identity_status is not None:
            return _claude_identity_status
        tier = "sonnet"
        proc = subprocess.run(  # noqa: PLW1510 (check=False would break test fakes with fixed signatures; see backend.py test suite)
            ["claude", "-p", "1+1", "--model", tier, "--output-format", "json"],
            capture_output=True, text=True, env=_first_party_claude_env(),
        )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            _claude_identity_status = {"ok": True, "model": None, "reason": ""}
            return _claude_identity_status
        served = payload.get("model")
        expected_prefix = _CLAUDE_TIER_MODEL_PREFIXES[tier]
        if served and not served.startswith(expected_prefix):
            _claude_identity_status = {
                "ok": False, "model": served,
                "reason": (
                    f"Claude backend identity check failed: served {served!r}, "
                    f"expected {expected_prefix}*"
                ),
            }
        else:
            _claude_identity_status = {"ok": True, "model": served, "reason": ""}
        return _claude_identity_status

    def resource_status(self) -> dict:
        """Claude's gate is the poller-fed, hysteresis-stabilized usage state
        (see pipeline_mcp_server.check_usage / _usage_gate), not a live /cost
        probe — reading the cached `paused` flag here is cheap and reflects the
        same decision the poller already made. Imported locally because the
        orchestrator imports this module (a top-level import would cycle); by
        call time pipeline_mcp_server is fully loaded. Failing open (ok) on
        missing/garbled state matches check_usage's own fail-open behavior.

        Also folds in the cached provider-identity check (verify_identity) -
        an unchecked (None) cache fails open, same as missing usage state;
        only a confirmed mismatch blocks, through this same gate a tripped
        usage pause already uses.
        """
        from app import pipeline_mcp_server as _p  # local: avoids an import cycle
        paused = bool(_p._read_usage_state().get("paused", False))
        if paused:
            return {"ok": False, "reason": "Claude usage gate tripped"}
        if _claude_identity_status is not None and not _claude_identity_status.get("ok", True):
            return {"ok": False, "reason": _claude_identity_status.get("reason", "")}
        return {"ok": True, "reason": ""}


# Tier names (opus/sonnet/haiku) come from Claude persona frontmatter and
# story overrides — see pipeline_mcp_server.py's _persona_default_model. Map
# them to concrete local model names; any tier without its own override (or
# any unrecognized tier) falls back to PIPELINE_LOCAL_MODEL_DEFAULT.
_LOCAL_TIER_ENV = {
    "opus": "PIPELINE_LOCAL_MODEL_OPUS",
    "sonnet": "PIPELINE_LOCAL_MODEL_SONNET",
    "haiku": "PIPELINE_LOCAL_MODEL_HAIKU",
}
_LOCAL_DEFAULT_MODEL = "devstral:24b"

# Rough chars-per-token estimate for sizing the review-loop trim budget below
# (no tokenizer available here) - deliberately NOT dynamically calibrated
# like scripts/local_agent.py's _measured_chars_per_token: the trigger for
# _review_loop's proactive trim is the turn's own real, measured
# prompt_eval_count (no estimation involved), and a fixed conservative ratio
# is sufficient for sizing how much to cut once triggered.
_CHARS_PER_TOKEN_ESTIMATE = 4

# Once a review turn's measured prompt_eval_count reaches this fraction of
# num_ctx, _review_loop trims the transcript BEFORE the next turn instead of
# letting it grow unbounded (see that method's trim block for the live
# overflow rates this addresses).
REVIEW_PROACTIVE_TRIM_THRESHOLD = float(
    os.environ.get("PIPELINE_LOCAL_REVIEW_PROACTIVE_TRIM_THRESHOLD", "0.85"))


def _resolve_local_model(tier: str, provider: str = "ollama") -> str:
    # A value containing ':' (Ollama's tag separator, e.g. "devstral:24b")
    # is already a concrete model tag, not a tier name - return as-is
    # rather than looking it up in _LOCAL_TIER_ENV, where it would never
    # match and silently fall back to PIPELINE_LOCAL_MODEL_DEFAULT instead
    # of the caller's explicit choice (see _run_reviewer's
    # PIPELINE_LOCAL_REVIEW_MODEL override).
    if ":" in tier:
        return tier
    default = os.environ.get("PIPELINE_LOCAL_MODEL_DEFAULT", _LOCAL_DEFAULT_MODEL)
    env_var = _LOCAL_TIER_ENV.get(tier.lower())
    if not env_var:
        return default
    # Provider-scoped override (e.g. PIPELINE_LOCAL_MODEL_MLX_SONNET) is
    # checked before the provider-agnostic PIPELINE_LOCAL_MODEL_SONNET, so
    # two roles on different local providers never silently share one
    # tier->model mapping meant for a single wire format/model namespace.
    # provider= defaults to "ollama" - the historical implicit assumption
    # every existing caller made before this parameter existed.
    scoped_env_var = env_var.replace(
        "PIPELINE_LOCAL_MODEL_", f"PIPELINE_LOCAL_MODEL_{provider.upper()}_"
    )
    scoped = os.environ.get(scoped_env_var)
    if scoped:
        return scoped
    return os.environ.get(env_var, default)


# Per-model tuned defaults, keyed by the RESOLVED concrete model tag (e.g.
# "gpt-oss:20b"), not the tier ("sonnet"/"opus"/"haiku"). Populated as a
# model's temperature/num_ctx is empirically settled (see tests/benchmark/
# A/B experiments). An explicit PIPELINE_LOCAL_TEMPERATURE/PIPELINE_LOCAL_NUM_CTX
# env var always overrides an entry here (operator override wins); a model
# tag with no entry, or an entry missing one of the two keys, falls back to
# OllamaDriver's constructor-captured default for that specific value.
_LOCAL_MODEL_TUNING: dict[str, dict[str, float | int]] = {
    # 2026-07-03 A/B experiment (tests/benchmark/_runs/full_20260703_postfix
    # vs temp_tune_20260703, 15 cells each): temp=1.0 -> 6/15 success, 3
    # cells where the implementation file never landed at all; temp=0.3 ->
    # 9/15 success, only 1 zero-code-landed cell, same 11/15 ground-truth
    # pass rate. Lower temperature measurably improves self-correction
    # without costing correctness. No num_ctx entry: an earlier 32768 value
    # here was never itself A/B-tested (the experiment above only varied
    # temperature) and PIPELINE_LOCAL_NUM_CTX always overrides this table
    # anyway (see _tuned_num_ctx) - keeping an unvalidated number here just
    # invited it to be mistaken for a settled finding. See
    # TOKEN_CONTEXT_OPTIMIZATION_PLAN.md and the launchd plist's
    # PIPELINE_LOCAL_NUM_CTX for where num_ctx is actually decided.
    "gpt-oss:20b": {"temperature": 0.3},
}


def _tuned_num_ctx(model_tag: str, fallback: int) -> int:
    env = os.environ.get("PIPELINE_LOCAL_NUM_CTX")
    if env is not None:
        return int(env)
    tuned = _LOCAL_MODEL_TUNING.get(model_tag, {}).get("num_ctx")
    return int(tuned) if tuned is not None else fallback


def _tuned_temperature(model_tag: str, fallback: float) -> float:
    env = os.environ.get("PIPELINE_LOCAL_TEMPERATURE")
    if env is not None:
        return float(env)
    tuned = _LOCAL_MODEL_TUNING.get(model_tag, {}).get("temperature")
    return float(tuned) if tuned is not None else fallback


def _recover_tool_calls(content: str | None) -> list | None:
    """Recover tool calls from message text when the native tool_calls field
    is empty — the local model intermittently emits well-formed calls as text
    ([TOOL_CALLS]/bare arrays/```json fences). Mirrors scripts/local_agent.py's
    parser (kept in sync; both are small)."""
    if not content:
        return None
    text = content.strip().replace("[TOOL_CALLS]", "")
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    m = re.search(r"(\[\s*\{.*\}\s*\]|\{.*\})", text, re.DOTALL)
    if m:
        candidates.append(m.group(1))
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        items = obj if isinstance(obj, list) else [obj]
        out = [{"function": {"name": it["name"], "arguments": it.get("arguments", it.get("parameters", {}))}}
               for it in items if isinstance(it, dict) and "name" in it]
        if out:
            return out
    return None


def _infer_review_tool_call(content: str | None) -> list | None:
    """Last-resort recovery for the review loop: the local model sometimes
    emits a bare arguments object (no tool name) like {"command": "git diff"}.
    Infer the intended review tool from its keys (unambiguous for this small
    read-only tool set). Returns None if nothing parseable is found."""
    if not content:
        return None
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict) or "name" in obj:
        return None  # named calls are handled by _recover_tool_calls
    if "verdict" in obj:
        return [{"function": {"name": "submit_review", "arguments": obj}}]
    if "command" in obj:
        return [{"function": {"name": "bash", "arguments": obj}}]
    if "path" in obj:
        return [{"function": {"name": "view_file", "arguments": obj}}]
    return None


def _review_msg_chars(m: dict) -> int:
    n = len(str(m.get("content") or ""))
    tool_calls = m.get("tool_calls")
    if tool_calls:
        n += len(json.dumps(tool_calls))
    return n


def _trim_review_transcript(messages: list, max_chars: int) -> list:
    """Bound a review transcript to max_chars, dropping the oldest middle
    content when it would otherwise overflow the model's context window.

    Same block-preserving algorithm as scripts/local_agent.py's
    _trim_resumed_transcript (own copy - the review loop's message shape and
    call site differ enough that sharing the dispatch-loop original isn't a
    clean fit; keep both in sync if the algorithm changes): messages[:2] (the
    system+task head) is always kept, an assistant tool_calls message is
    never split from its own tool-role response, and content is dropped in
    whole blocks starting from the oldest until the rest fits the budget.
    """
    total = sum(_review_msg_chars(m) for m in messages)
    if total <= max_chars:
        return messages
    head_len = min(2, len(messages))
    head = messages[:head_len]
    head_chars = sum(_review_msg_chars(m) for m in head)
    blocks: list[list[dict]] = []
    i = head_len
    while i < len(messages):
        block = [messages[i]]
        i += 1
        while i < len(messages) and messages[i].get("role") == "tool":
            block.append(messages[i])
            i += 1
        blocks.append(block)
    budget = max_chars - head_chars
    kept: list[list[dict]] = []
    kept_chars = 0
    for block in reversed(blocks):
        block_chars = sum(_review_msg_chars(m) for m in block)
        if kept and kept_chars + block_chars > budget:
            break
        kept.append(block)
        kept_chars += block_chars
    kept.reverse()
    dropped = len(blocks) - len(kept)
    if dropped == 0:
        return messages
    note = {
        "role": "user",
        "content": (
            f"[{dropped} earlier turn(s) were dropped from this review "
            "transcript to fit the model's context window. Continue "
            "reviewing using only the history below.]"
        ),
    }
    return head + [note] + [m for block in kept for m in block]


_REVIEW_LOG_TRUNCATE = 2000


def _append_review_log(cwd: str, text: str) -> None:
    """Best-effort transcript write for the review loop: the verdict is the
    deliverable, the log is diagnostic only, so any OSError (unwritable
    path, read-only tree, review.log pre-empted by a directory) must be
    swallowed rather than breaking the review."""
    try:
        with open(Path(cwd) / "review.log", "a") as f:
            f.write(text)
    except OSError:
        pass


def _run_readonly_tool(fn: str, args: dict, cwd: Path) -> str:
    """Execute a review (read-only) tool: bash (run commands) or view_file."""
    if fn == "view_file":
        path = cwd / args.get("path", "")
        if not path.exists():
            return f"ERROR: {args.get('path')} does not exist."
        lines = path.read_text().splitlines(keepends=True)
        return "".join(f"{i + 1:4d}| {ln}" for i, ln in enumerate(lines))[:3000]
    if fn == "bash":
        # Acquire the heavy-build lock when the reviewer triggers a build
        # or test command — review runs cargo/npm/etc. to verify the agent's
        # claim, and we don't want it stomping on a concurrent in-flight
        # dispatch's build. Same lock the agent harness uses, see
        # pipeline_mcp_server._heavy_lock docstring.
        import shlex

        from app import (
            pipeline_mcp_server as _p,  # local: avoid import cycle at module load
        )
        cmd = args.get("command", "")
        try:
            argv0 = shlex.split(cmd)[0] if cmd.strip() else ""
        except ValueError:
            argv0 = ""
        if argv0 and _p._is_heavy([argv0]):
            with _p._heavy_lock():
                pr = subprocess.run(cmd, check=False, shell=True, cwd=cwd,
                                    capture_output=True, text=True)
        else:
            pr = subprocess.run(cmd, check=False, shell=True, cwd=cwd,
                                capture_output=True, text=True)
        return (pr.stdout + pr.stderr)[:3000] or "(no output)"
    return f"unknown tool {fn}"


class OllamaDriver:
    """Backend driver for Ollama's native /api/chat endpoint.

    Uses the native API rather than Ollama's OpenAI-compatible /v1 surface
    because only the native API accepts `options.num_ctx`. That control turned
    out to matter in practice: Ollama's default context (131072) made a 24B
    model's KV cache blow past 24GB of unified memory, forcing a CPU/GPU
    split that made a single completion time out at 600s. Pinning num_ctx to
    a size that actually fits keeps the model 100% on GPU. This is an Ollama-
    specific knob - a future vLLM/cloud driver would set its context size
    differently (server-side, e.g. --max-model-len) and would not share this
    class.

    complete() has two modes, matching ClaudeCliDriver.complete()'s behavior:
    a self-contained prompt (overlord-style: allowed_tools without Bash, no
    cwd) is a single /api/chat round-trip; a review-style call (allowed_tools
    includes Bash + a worktree cwd, see _run_reviewer) runs a blocking
    READ-ONLY tool loop (bash to run tests + view_file) and ends when the model
    calls submit_review, returning a `VERDICT:` block for _parse_verdict. The
    loop exposes no edit tools, so review cannot modify the tree.

    dispatch() runs scripts/local_agent.py as a subprocess in `cwd`, giving
    the model a real native-tool-calling loop (create_file/str_replace/
    view_file/bash/checkpoint/done) to edit files, run tests, use git, and
    checkpoint. It deliberately does NOT use OpenHands: a full investigation
    (Local_LLM_Port_Plan.md §Status/§8) found OpenHands' CLI is the wrong
    harness for a 24B local model - its ~7 bloated tool schemas collapse
    devstral's native `[TOOL_CALLS]` adherence and its strict native-only
    parsing has no recovery when a tool call arrives as text. The minimal
    loop (clean tool set + tolerant parser + non-destructive editor + loop
    guard + commit enforcement) was validated end-to-end on real TDD stories.
    See scripts/local_agent.py for the loop and its guards.

    dispatch() refuses any allowed_tools that excludes Edit/Write: the local
    *dispatch* harness is write-oriented. Read-only roles don't use dispatch()
    at all — review goes through complete()'s review-loop mode above — so this
    guard is just a guard against misconfiguring a write role with read-only
    tools.
    """

    def __init__(self, provider_name: str | None = None) -> None:
        # Resolved per-instance (not module-cached) so a PIPELINE_LOCAL_PROVIDER
        # change between OllamaDriver() constructions takes effect, mirroring
        # this class's own live-env-read pattern elsewhere (see e.g.
        # review_max_steps below). complete()/resource_status() consult it
        # directly; dispatch() forwards self.provider.name to the subprocess
        # as LOCAL_AGENT_PROVIDER, which scripts/local_agent(_oracle).py use
        # to pick between Ollama's streaming /api/chat and a provider's
        # blocking chat() (see MODEL_PROVIDER_ABSTRACTION_PLAN.md S3).
        # provider_name= pins a specific provider (see _DRIVERS' "ollama"/
        # "lmstudio"/"mlx" entries, RELIABILITY_PLAN.md T16), bypassing the
        # env lookup; omitted/None keeps today's PIPELINE_LOCAL_PROVIDER-
        # resolved behavior (the "local" back-compat alias in _DRIVERS).
        self.provider = inference_providers.get_local_provider(provider_name)
        # Provider-scoped PIPELINE_LOCAL_ENDPOINT_<PROVIDER> is checked
        # before the process-wide PIPELINE_LOCAL_ENDPOINT, which is checked
        # before the provider's own default_endpoint (not a hardcoded
        # Ollama-specific fallback) - so two roles pinned to two different
        # local providers in the same process (e.g. dispatch=mlx,
        # review=ollama) resolve independent endpoints instead of silently
        # sharing one global override meant for a single provider. Found via
        # a live production-shaped run where review (ollama) inherited
        # dispatch's (mlx) PIPELINE_LOCAL_ENDPOINT override and 404'd
        # probing Ollama's /api/tags against MLX's port.
        self.endpoint = (
            os.environ.get(f"PIPELINE_LOCAL_ENDPOINT_{self.provider.name.upper()}")
            or os.environ.get("PIPELINE_LOCAL_ENDPOINT")
            or self.provider.default_endpoint
        ).rstrip("/")
        self.timeout = float(os.environ.get("PIPELINE_LOCAL_TIMEOUT_SECONDS", "600"))
        # 16384 fits 100% on GPU on a 24GB M4 and gives the agentic loop real
        # headroom; complete()'s self-contained prompts are smaller so the same
        # value is safe there too.
        self.num_ctx = int(os.environ.get("PIPELINE_LOCAL_NUM_CTX", "16384"))
        self.dispatch_timeout = float(
            os.environ.get("PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS", "900"))
        self.max_steps = int(os.environ.get("PIPELINE_LOCAL_MAX_STEPS", "40"))
        self.temperature = float(os.environ.get("PIPELINE_LOCAL_TEMPERATURE", "0.3"))
        self.review_max_steps = int(os.environ.get("PIPELINE_LOCAL_REVIEW_MAX_STEPS", "20"))

    def complete(
        self, prompt: str, *, system: str | None = None, model: str,
        allowed_tools: str | None = None, cwd: str | None = None,
        max_tokens: int | None = None,
        cell_dir: str | None = None,
    ) -> str:
        # max_tokens is not honored by either driver now (see
        # ClaudeCliDriver.complete); Ollama caps the response via the model's
        # own context window (num_ctx in _chat), so we accept the kwarg to
        # satisfy the protocol but don't act on it here.
        # Review-style call: allowed_tools includes Bash and a worktree cwd is
        # given (see _run_reviewer). The model must actually run the tests and
        # read files, then emit a VERDICT — so run a blocking read-only tool
        # loop, mirroring how ClaudeCliDriver.complete() transparently runs a
        # tool loop when `claude -p` is given allowed_tools+cwd. Overlord-style
        # calls (allowed_tools="Read", no cwd) fall through to single-shot.
        if cwd is not None and allowed_tools and "Bash" in allowed_tools.split(","):
            return self._review_loop(prompt, system=system, model=model,
                                     cwd=cwd, cell_dir=cell_dir)

        resolved_model = _resolve_local_model(model, provider=getattr(self.provider, "name", "ollama"))
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            envelope = self._chat(messages, resolved_model)
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"Local backend at {self.endpoint} (model={resolved_model}) "
                f"is unreachable or errored: {e}"
            ) from e
        # envelope is the full /api/chat response; the assistant message
        # is in ["message"]. record_token_usage is best-effort and a no-op
        # when cell_dir is None, matching ClaudeCliDriver.complete()'s
        # "only record on the structured path" policy.
        if cell_dir is not None:
            self.record_token_usage(
                {
                    "input_tokens": envelope.get("prompt_eval_count", 0),
                    "output_tokens": envelope.get("eval_count", 0),
                    "duration_ns": envelope.get("total_duration"),
                    "model": resolved_model,
                },
                cell_dir=cell_dir, role="complete",
            )
        return envelope["message"]["content"]

    def _chat(self, messages: list, model: str, tools: list | None = None) -> dict:
        # num_ctx/temperature are resolved per call (env override > per-model
        # tuning table > constructor default), mirroring dispatch()'s
        # PIPELINE_LOCAL_MAX_STEPS pattern below, so a per-model override set
        # after this driver was constructed (e.g. tests/benchmark/models.py's
        # gptoss row, or an entry in _LOCAL_MODEL_TUNING) actually takes
        # effect for complete()/review calls too, instead of being silently
        # shadowed by the value captured at OllamaDriver.__init__ time.
        # The actual wire call (request shape, 429 detection, response
        # envelope) lives in self.provider - see inference_providers.py.
        num_ctx = _tuned_num_ctx(model, self.num_ctx)
        temperature = _tuned_temperature(model, self.temperature)
        # Returns the full response envelope (not just the "message" body)
        # so callers that have structured usage data (review loop's
        # prompt_eval_count/eval_count/total_duration for the per-call
        # token-cost sidecar) can extract it. The single-shot complete()
        # path peels off ["message"] itself; the review loop peels off
        # both the message and the usage fields.
        return self.provider.chat(
            messages, model=model, num_ctx=num_ctx, temperature=temperature,
            tools=tools, endpoint=self.endpoint, timeout=self.timeout,
        )

    # Read-only tools for the review loop. No create/edit — review must not
    # modify the tree (the "review does not merge / does not edit" guarantee).
    # bash is needed to run the test suite; like the Claude reviewer's
    # Bash+Read, it is not sandboxed, but it runs in the isolated worktree.
    # submit_review is the explicit terminator (like dispatch's `done`): far
    # more reliable for a weak local model than scanning free prose for a
    # VERDICT line, which in testing it failed to emit cleanly.
    _REVIEW_TOOLS: ClassVar[list[dict]] = [
        {"type": "function", "function": {
            "name": "bash", "description": "Run a bash command in the worktree (run tests, git diff/log, etc.).",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
        {"type": "function", "function": {
            "name": "view_file", "description": "Show a file's contents with line numbers.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
        {"type": "function", "function": {
            "name": "submit_review", "description": "Submit your final review verdict. Call this once, after running the tests and inspecting the changes. If verdict is REQUEST_CHANGES, summary is required and must list the specific problems: which file, what is wrong, and what must change.",
            "parameters": {"type": "object", "properties": {
                "verdict": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES"]},
                "summary": {"type": "string", "description": "brief justification; required when verdict is REQUEST_CHANGES - state the specific file, problem, and required fix"},
                "pr_title": {"type": "string", "description": "PR title (if APPROVE)"},
                "pr_body": {"type": "string", "description": "PR body (if APPROVE)"}},
                "required": ["verdict"]}}},
    ]

    def _review_loop(self, prompt: str, *, system: str | None, model: str,
                      cwd: str, cell_dir: str | None = None) -> str:
        """Blocking read-only agentic loop for review. The model runs tests and
        reads files via tools, then calls submit_review with its verdict. Returns
        a `VERDICT: ...` text block (so pipeline_mcp_server._parse_verdict, shared
        with the Claude path, scans it unchanged). Uses the same tolerant parsing
        as dispatch, plus key-based tool inference, since the local model
        intermittently emits tool calls as text and drops the tool name."""
        resolved_model = _resolve_local_model(model, provider=getattr(self.provider, "name", "ollama"))
        num_ctx = _tuned_num_ctx(resolved_model, self.num_ctx)
        _append_review_log(
            cwd,
            f"=== review cycle "
            f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} "
            f"model={resolved_model} ===\n",
        )
        preamble = (
            "You are reviewing code in the current directory, READ-ONLY. First use "
            "the bash tool to run the test suite and inspect the changes (e.g. "
            "`git diff`, `git log -p -1`), and view_file to read files — do not edit "
            "anything. Then call submit_review exactly once with verdict APPROVE or "
            "REQUEST_CHANGES (REQUEST_CHANGES if the tests fail). Always call a tool; "
            "do not answer in prose.\n\n"
            "For large diffs: bash output is truncated to 3000 chars per call, so a "
            "bare `git diff` may silently cut off the end. Start with `git diff --stat` "
            "to see the scope, then use `git diff -- <file>` per file for the parts you "
            "need, and `view_file <path>` for surrounding context. Do NOT rely on a "
            "single `git diff` for a multi-file change."
        )
        system_content = preamble + ("\n\n" + system if system else "")
        messages = [{"role": "system", "content": system_content},
                    {"role": "user", "content": prompt}]
        nudged = False
        findings_nudged = False
        last_prose = ""
        fallthrough_reason = "step cap exhausted"
        # Re-read live from os.environ, mirroring dispatch()'s live re-read of
        # PIPELINE_LOCAL_MAX_STEPS (see its comment) rather than only using
        # the value captured once at __init__ time - otherwise a plist/env
        # edit to the review cap silently has no effect on a long-lived MCP
        # server process (2026-07-07 web-client-epic retro §7).
        review_max_steps = int(
            os.environ.get("PIPELINE_LOCAL_REVIEW_MAX_STEPS", str(self.review_max_steps))
        )
        for i in range(review_max_steps):
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
            remaining = review_max_steps - i
            if remaining <= 5:
                messages.append({"role": "user", "content":
                    f"You have {remaining} review step(s) left. Stop investigating "
                    "and call submit_review now with verdict APPROVE or "
                    "REQUEST_CHANGES (or end your reply with a 'VERDICT: APPROVE' "
                    "or 'VERDICT: REQUEST_CHANGES' line)."})
            # Snapshot what this turn actually sends, so the trim below can
            # calibrate chars-per-token against the real prompt_eval_count
            # this exact request comes back with (the tools schema is part of
            # every prompt and is counted in prompt_eval_count, so it belongs
            # in the numerator - see scripts/local_agent.py's copy).
            sent_chars = (
                sum(_review_msg_chars(msg) for msg in messages)
                + len(json.dumps(self._REVIEW_TOOLS))
            )
            try:
                try:
                    # _chat now returns the full /api/chat envelope (not just
                    # the message), so we can pull prompt_eval_count /
                    # eval_count / total_duration for the per-call
                    # token-cost sidecar alongside the message body itself.
                    envelope = self._chat(messages, resolved_model,
                                           tools=self._REVIEW_TOOLS)
                except httpx.HTTPError as e:
                    raise RuntimeError(
                        f"Local backend at {self.endpoint} (model={resolved_model}) "
                        f"is unreachable or errored during review: {e}"
                    ) from e
                m = envelope["message"]
                if cell_dir is not None:
                    self.record_token_usage(
                        {
                            "input_tokens": envelope.get("prompt_eval_count", 0),
                            "output_tokens": envelope.get("eval_count", 0),
                            "duration_ns": envelope.get("total_duration"),
                            "model": resolved_model,
                        },
                        cell_dir=cell_dir, role="review", step=i,
                    )
                messages.append(m)
                # Proactive trim: once a turn's REAL measured prompt_eval_count
                # is already close to num_ctx, shrink the transcript now
                # rather than letting it grow into a 500/UNKNOWN-verdict on a
                # later turn. See _trim_review_transcript's docstring for the
                # live overflow rates this addresses (backend.py had no
                # trimming here at all, unlike the dispatch agent loop).
                prompt_eval_count = envelope.get("prompt_eval_count")
                if (
                    prompt_eval_count
                    and prompt_eval_count >= num_ctx * REVIEW_PROACTIVE_TRIM_THRESHOLD
                ):
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
                        else _CHARS_PER_TOKEN_ESTIMATE
                    )
                    budget_chars = int(num_ctx * chars_per_token * 0.75)
                    trimmed = _trim_review_transcript(messages, budget_chars)
                    if len(trimmed) != len(messages):
                        messages[:] = trimmed
                if m.get("content"):
                    last_prose = m["content"]
                    _append_review_log(cwd, m["content"] + "\n")
                tcs = (m.get("tool_calls") or _recover_tool_calls(m.get("content", ""))
                       or _infer_review_tool_call(m.get("content", "")))
                if not tcs:
                    if nudged:
                        break  # still no tool after a nudge -> give up (-> salvage/park)
                    nudged = True
                    messages.append({"role": "user", "content":
                        "Call a tool (bash/view_file to investigate, or submit_review to finish). Do not reply in prose."})
                    continue
                nudged = False
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
                    _append_review_log(
                        cwd, f"TOOL: {fn} {json.dumps(args, separators=(',', ':'), default=str)}\n"
                    )
                    if fn == "submit_review":
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
                                and not findings_nudged):
                            findings_nudged = True
                            messages.append({"role": "tool", "content":
                                "REQUEST_CHANGES rejected: no findings given. "
                                "Call submit_review again with a summary that "
                                "states the specific problems - which file, "
                                "what is wrong, and what must change."})
                            continue
                        _append_review_log(cwd, f"VERDICT: {verdict}\n")
                        if cell_dir is not None:
                            # The verdict-bearing call is i (the same step
                            # the message above was already recorded for);
                            # write a second row tagged with verdict= so
                            # the analysis script can attribute total cost
                            # to the final call without re-deriving it from
                            # the transcript.
                            self.record_token_usage(
                                {
                                    "input_tokens": 0,
                                    "output_tokens": 0,
                                    "model": resolved_model,
                                },
                                cell_dir=cell_dir, role="review",
                                step=i, verdict=verdict,
                            )
                        return f"VERDICT: {verdict}\n\n{title}\n{body}".strip()
                    result = _run_readonly_tool(fn, args, Path(cwd))
                    messages.append({"role": "tool", "content": result})
                    _append_review_log(cwd, f"RESULT: {result[:_REVIEW_LOG_TRUNCATE]}\n")
            except RuntimeError:
                raise  # preserve the httpx.HTTPError -> RuntimeError contract above
            except Exception:  # noqa: BLE001 (deliberate: any other per-step failure must fail safe rather than crash the harness, per the comment below)
                # Any other failure this step (malformed response shape, a bad
                # tool call, a read-only tool erroring) must not crash the
                # caller. Fail safe into the same "no verdict" path a
                # genuinely inconclusive review already takes below, rather
                # than propagating and taking down the whole harness process.
                fallthrough_reason = "loop error"
                break
        # No submit_review within the step cap. Salvage ONLY an explicit terminal
        # verdict line — the model's actual conclusion, written last. An inline
        # mention of "VERDICT: APPROVE" earlier in the prose (the model echoing
        # the convergence nudge, or describing what an approve would require) must
        # NOT be trusted: _parse_verdict's match is unanchored, so returning such
        # prose would false-positive an APPROVE and auto-merge unreviewed code
        # (fail-open). Fail-closed: no terminal verdict line -> "" -> UNKNOWN ->
        # park for a human/Claude rather than auto-merge. Return ONLY the terminal
        # verdict line, not the full prose: _parse_verdict is an unanchored
        # re.search that matches the FIRST "VERDICT:" substring, so returning
        # prose with an inline "VERDICT: APPROVE" mention earlier and a terminal
        # "VERDICT: REQUEST_CHANGES" line would parse to APPROVE and auto-merge
        # unreviewed code (a second fail-open). The terminal line is the model's
        # actual conclusion; handing just that to _parse_verdict leaves it nothing
        # else to false-match on.
        lines = [ln.strip() for ln in last_prose.splitlines() if ln.strip()]
        if lines and re.search(r"^VERDICT:\s*(APPROVE|REQUEST_CHANGES)\s*$",
                               lines[-1], re.IGNORECASE):
            _append_review_log(cwd, f"{lines[-1]}\n")
            return lines[-1]
        _append_review_log(cwd, f"no verdict ({fallthrough_reason})\n")
        return ""

    # The local agent loop lives in a standalone script so it can run as a
    # pollable subprocess; run it with this project's venv python (which has
    # httpx and can import pipeline_mcp_server for in-process checkpointing).
    _AGENT_SCRIPT = Path(__file__).resolve().parent / "scripts" / "local_agent.py"
    _AGENT_SCRIPT_ORACLE = Path(__file__).resolve().parent / "scripts" / "local_agent_oracle.py"
    _VENV_PYTHON = Path(__file__).resolve().parent / ".venv" / "bin" / "python3"

    def record_token_usage(
        self, usage: dict, *, cell_dir: str | None = None,
        role: str = "review", step: int | None = None,
        verdict: str | None = None,
    ) -> None:
        """Append an Ollama usage record to <cell_dir>/review_token_costs.jsonl.

        Same JSONL sidecar as ClaudeCliDriver.record_token_usage; populated
        fields differ because Ollama's /api/chat envelope has
        prompt_eval_count/eval_count/total_duration (nanoseconds) but no
        cache fields or USD cost. Best-effort: OSError is swallowed so a
        failed sidecar write never breaks the review loop - same pattern
        as ClaudeCliDriver.record_token_usage and _append_review_log.
        """
        if cell_dir is None:
            return
        try:
            record = {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "backend": "ollama",
                "model": usage.get("model", "?"),
                "role": role,
                "step": step,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_creation_input_tokens": None,
                "cache_read_input_tokens": None,
                "total_cost_usd": None,
                "duration_ms": None,
                "duration_ns": usage.get("duration_ns"),
                "verdict": verdict,
            }
            with open(Path(cell_dir) / "review_token_costs.jsonl", "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass

    def dispatch(
        self, prompt: str, *, system: str | None = None, model: str,
        allowed_tools: str | None = None, cwd: Path, log_path: Path, append: bool,
        acceptance: list[str] | None = None,
        resume_transcript_path: Path | None = None,
        resume_append_content: str | None = None,
        rework_full_suite: bool = False,
    ) -> AgentHandle:
        if allowed_tools and not ({"Edit", "Write"} & set(allowed_tools.split(","))):
            raise NotImplementedError(
                f"OllamaDriver.dispatch is a writing/coding harness and cannot "
                f"honor read-only allowed_tools={allowed_tools!r} (it would let "
                f"the agent edit files anyway). Keep this role on claude until a "
                f"read-only local tool set is implemented and verified."
            )
        resolved_model = _resolve_local_model(model, provider=getattr(self.provider, "name", "ollama"))
        # Fix #1: if the story carries an `acceptance` block, switch to the
        # oracle-graded harness variant (script + MODE env). The list is
        # passed as a JSON string to keep the env-var contract uniform with
        # the other LOCAL_AGENT_* knobs.
        acceptance = acceptance or []
        oracle_mode = bool(acceptance)
        agent_script = self._AGENT_SCRIPT_ORACLE if oracle_mode else self._AGENT_SCRIPT
        env = {
            **os.environ,
            "LOCAL_AGENT_MODEL": resolved_model,
            "LOCAL_AGENT_SYSTEM": system or "",
            "LOCAL_AGENT_TASK": prompt,
            "LOCAL_AGENT_ENDPOINT": self.endpoint,
            "LOCAL_AGENT_TIMEOUT": str(self.dispatch_timeout),
            "LOCAL_AGENT_PROVIDER": self.provider.name,
        }
        # num_ctx/temperature are resolved per dispatch (env override >
        # per-model tuning table > constructor default) so a per-model
        # override (e.g. tests/benchmark/models.py's gptoss row, or an entry
        # in _LOCAL_MODEL_TUNING keyed on the resolved model tag) actually
        # takes effect instead of being silently shadowed by the value
        # captured at OllamaDriver.__init__ time. Fall back to that captured
        # value when neither is set, so existing callers that rely on the
        # constructor default are not broken.
        num_ctx = _tuned_num_ctx(resolved_model, self.num_ctx)
        # Resolve temperature: use raw env if set to preserve boundary values
        temp_env = os.environ.get("PIPELINE_LOCAL_TEMPERATURE")
        if temp_env is not None:
            if temp_env == "":
                raise ValueError("empty PIPELINE_LOCAL_TEMPERATURE")
            temp_str = temp_env
        else:
            temp_str = str(_tuned_temperature(resolved_model, self.temperature))
        env["LOCAL_AGENT_NUM_CTX"] = str(num_ctx)
        env["LOCAL_AGENT_TEMPERATURE"] = temp_str
        env["PIPELINE_TRANSPORT_NUM_CTX"]     = str(num_ctx)
        env["PIPELINE_TRANSPORT_TEMPERATURE"] = temp_str
        # PIPELINE_LOCAL_MAX_STEPS is the real, plist-honored step-cap knob



        # for the dispatch agent. Re-read it on every dispatch so launchd /
        # shell edits to the env actually take effect instead of being
        # silently shadowed by the value captured at OllamaDriver.__init__
        # time. Fall back to that captured value when the env var is unset,
        # so existing callers that rely on the constructor default are not
        # broken.
        max_steps = int(
            os.environ.get("PIPELINE_LOCAL_MAX_STEPS", str(self.max_steps))
        )
        env["PIPELINE_TRANSPORT_MAX_STEPS"] = str(max_steps)
        env["LOCAL_AGENT_MAX_STEPS"] = str(max_steps)
        # PIPELINE_LOCAL_MAX_STEPS above) so an env edit takes effect without
        # restarting the MCP server. Only the exact tokens "true"/"false" opt
        # in; anything else leaves LOCAL_AGENT_THINK unset and local_agent.py
        # omits the `think` key from the /api/chat body entirely (no-op for
        # non-Qwen3 models like devstral/gpt-oss/qwen3-coder). See
        # scripts/local_agent.py THINK/_ollama_payload and test_backend.py.
        think = os.environ.get("PIPELINE_LOCAL_THINK", "").strip().lower()
        if think in ("true", "false"):
            env["LOCAL_AGENT_THINK"] = think
        if oracle_mode:
            env["LOCAL_AGENT_ACCEPTANCE"] = json.dumps(acceptance)
            env["LOCAL_AGENT_MODE"] = "oracle"
        # L1 (REVIEWER_ESCALATION_PLAN.md): on a CI-fail-rework redispatch,
        # raise the agent's done-bar to full-suite-green. Only set when the
        # caller explicitly opts in (a CI-triggered rework); cold-start
        # dispatchs leave it unset so the oracle-green bar is unchanged.
        if rework_full_suite:
            env["LOCAL_AGENT_REWORK_FULL_SUITE"] = "1"
        # Always persist the transcript so a later rework redispatch can
        # resume the prior message history instead of rebuilding a cold-start
        # prompt. The path is deterministic and lives inside the worktree (cwd)
        # so reworks — which reuse the existing worktree — find the same file.
        env["LOCAL_AGENT_TRANSCRIPT_PATH"] = str(Path(cwd) / ".agent_transcript.json")
        # Resume path: when set (a rework redispatch with an existing
        # transcript), local_agent.py loads the prior transcript and appends
        # resume_append_content (the reviewer's feedback) as a user message
        # instead of cold-starting from LOCAL_AGENT_TASK.
        if resume_transcript_path is not None:
            env["LOCAL_AGENT_RESUME_TRANSCRIPT_PATH"] = str(resume_transcript_path)
        if resume_append_content is not None:
            env["LOCAL_AGENT_RESUME_APPEND_CONTENT"] = resume_append_content
        argv = [str(self._VENV_PYTHON), str(agent_script)]
        with open(log_path, "a" if append else "w") as log_file:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log_file, stderr=log_file)
        return AgentHandle(pid=proc.pid, model=resolved_model)

    def usage_probe_text(self) -> str:
        raise NotImplementedError(
            "OllamaDriver has no usage/cost concept - its resource gate is "
            "resource_status() (Ollama reachability), not a /cost probe."
        )

    def resource_status(self) -> dict:
        """Local backend has no usage/cost limit to respect, so the gate is
        (1) whether the local inference server is up, and (2) T13: whether
        the host has enough free memory to actually run a dispatch on it -
        reachability alone doesn't mean there's headroom to complete one (see
        the qwen3-coder:30b session where free memory dropped to ~70MB and
        macOS silently killed backgrounded processes). (The concurrency
        ceiling is enforced separately by advance_pipeline via
        MAX_CONCURRENT_AGENTS.) This is what unlocks overnight autonomy
        decoupled from Claude's weekly limit: as long as the server is
        reachable and the host has headroom, local dispatch keeps running.
        Delegates to self.provider (never raises: an unimplemented stub
        provider reports "not ok" here rather than propagating
        NotImplementedError out of a method whose contract is to always
        return an {ok, reason} dict). Reachability is checked first and
        short-circuits the memory check entirely - an unreachable server
        can't dispatch regardless of memory, and that failure is the more
        actionable one to report.

        The floor itself is per-provider (`PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_
        <PROVIDER>`, e.g. `_MLX`, falling back to the generic
        `PIPELINE_LOCAL_MIN_FREE_MEMORY_MB` when unset): Ollama can evict a
        model under pressure, so headroom above the generic floor is a real
        safety margin against runaway VRAM swapping, but mlx_lm.server pins
        one model's full footprint for its entire process lifetime with
        nothing to evict - once a model large enough to leave under 2048mb
        free is loaded, free memory never recovers, and the generic floor
        gates dispatch permanently (not transiently) for as long as the
        server runs (observed live 2026-07-14: free memory settled at
        ~1000-1900mb for the whole session with a 16GB model resident,
        never once clearing 2048mb). advance_pipeline's memory-pressure
        exception (RELIABILITY_PLAN.md Mode 18/T18) deliberately never
        interrupts an in-progress story on this gate on the theory that the
        pressure is transient and will clear - true for a one-time cold-load
        spike, false for MLX's steady state, so an ungated MLX floor would
        silently paralyze all future dispatch on this plan. The override
        does not change the generic floor or any other provider's default.

        For Ollama there is a third, ORTHOGONAL check after the floor: the
        configured model's weights against TOTAL physical RAM, refusing a
        model too large to serve on this host at all (see the check's own
        comment for why it is not folded into the free-memory floor, and for
        the live calibration behind the default fraction). Like every other
        component here it fails open - an unresolvable tag, a cloud-served
        tag with no local footprint, or an unreadable total-RAM figure all
        leave dispatch permitted.
        """
        try:
            ok, reason = self.provider.reachable(self.endpoint)
        except NotImplementedError as e:
            return {"ok": False, "reason": str(e)}
        if not ok:
            return {"ok": ok, "reason": reason}
        free_mb = self._free_memory_mb()
        if free_mb is not None:
            provider_env = f"PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_{self.provider.name.upper()}"
            floor_mb = int(os.environ.get(
                provider_env,
                os.environ.get("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048"),
            ))
            if free_mb < floor_mb:
                return {
                    "ok": False,
                    "reason": f"insufficient free memory ({free_mb}mb < {floor_mb}mb floor)",
                }
        # Model-too-big-for-this-host gate. Deliberately ORTHOGONAL to the
        # free-memory floor above rather than folded into it: an earlier
        # attempt required `free >= floor + weights`, which paralyzed
        # dispatch outright (measured live on a 24576mb host: free 10837mb
        # against a 15202mb requirement, so every tick reported not-ok and
        # no local story could ever dispatch) - the same failure the floor's
        # own docstring warns about. Free memory is the wrong denominator on
        # macOS, which compresses and evicts under pressure: a 13GB model
        # demonstrably loads with well under 13GB "available".
        #
        # Weights against TOTAL physical RAM is the stable discriminator,
        # calibrated on live evidence: gpt-oss:20b (13154mb, 53.5% of
        # 24576mb) is the validated local workhorse, while devstral:24b
        # (~15GB, ~61%) 500-storms on every request (1-3 minutes per
        # failure) and cannot serve at all. The default fraction sits
        # between them, leaving the validated model ~6 points of margin -
        # deliberately biased toward never gating a model that works, since
        # a persistently-unservable model is ALSO caught downstream by the
        # infra-failure streak escalation (defense in depth, and the far
        # less damaging place to be wrong).
        #
        # Ollama-only: LM Studio JIT-loads with no equivalent listing, and
        # MLX pins one model for its process lifetime (already covered by
        # its provider-scoped floor override).
        if self.provider.name == "ollama":
            model_tag = os.environ.get("PIPELINE_LOCAL_MODEL_DEFAULT", _LOCAL_DEFAULT_MODEL)
            weights_mb = _ollama_model_weights_mb(self.endpoint, model_tag)
            total_mb = _total_memory_mb()
            max_fraction = float(os.environ.get(
                "PIPELINE_LOCAL_MAX_MODEL_RAM_FRACTION", "0.60"))
            # `weights_mb` of 0 is a cloud-served tag (no local footprint);
            # None is an unresolvable tag or a failed probe. Both fail open.
            if weights_mb and total_mb and weights_mb > total_mb * max_fraction:
                return {
                    "ok": False,
                    "reason": (
                        f"model {model_tag} too large for this host "
                        f"({weights_mb}mb weights > {max_fraction:.0%} of "
                        f"{total_mb}mb total RAM); it will thrash rather than "
                        f"serve - pick a smaller model or raise "
                        f"PIPELINE_LOCAL_MAX_MODEL_RAM_FRACTION"
                    ),
                }
        return {"ok": ok, "reason": reason}

    def _free_memory_mb(self) -> int | None:
        """Best-effort available-memory read via macOS's vm_stat, in MB.

        Sums free + inactive + purgeable pages, not free alone: inactive and
        purgeable pages are readily reclaimable under real pressure - the
        same accounting macOS's own memory-pressure tooling uses - so a
        free-only read materially understates real headroom.
        RELIABILITY_PLAN.md's T12 (2026-07-13) observed the strict-free
        floor tripping every dispatch tick while a resident model held pages
        that were never actually scarce, stalling dispatch for hours.
        Missing inactive/purgeable fields (an unexpected vm_stat output
        shape) degrade that term to 0 rather than aborting the whole read -
        free alone is still a valid, if less generous, answer.

        Returns None (never raises) on any subprocess/parse failure - a
        non-macOS host without vm_stat, an unexpected output shape, or a
        timeout - so resource_status()'s memory-floor check fails open
        ("can't determine memory" is not the same as "low memory") rather
        than blocking dispatch on a platform where the check can't run.
        """
        try:
            result = subprocess.run(  # noqa: PLW1510 (check=False would break test fakes with fixed signatures; see backend.py test suite)
                ["vm_stat"], capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return None
            page_size_match = re.search(r"page size of (\d+) bytes", result.stdout)
            free_pages_match = re.search(r"Pages free:\s+(\d+)\.", result.stdout)
            if not page_size_match or not free_pages_match:
                return None
            page_size = int(page_size_match.group(1))
            free_pages = int(free_pages_match.group(1))
            inactive_match = re.search(r"Pages inactive:\s+(\d+)\.", result.stdout)
            purgeable_match = re.search(r"Pages purgeable:\s+(\d+)\.", result.stdout)
            inactive_pages = int(inactive_match.group(1)) if inactive_match else 0
            purgeable_pages = int(purgeable_match.group(1)) if purgeable_match else 0
            available_pages = free_pages + inactive_pages + purgeable_pages
            return (available_pages * page_size) // (1024 * 1024)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None


def _total_memory_mb() -> int | None:
    """Total physical RAM in MB via macOS's `sysctl hw.memsize`.

    Used by `resource_status()`'s model-size gate as the denominator for
    "is this model simply too big for this machine" - a stable figure, unlike
    free memory, which fluctuates with whatever else is running.

    Returns None (never raises) on any subprocess/parse failure, including a
    non-macOS host without this sysctl key - the caller then skips the gate
    rather than guessing.
    """
    try:
        result = subprocess.run(  # noqa: PLW1510 (check=False would break test fakes with fixed signatures; see _ollama_loaded_models)
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        return int(result.stdout.strip()) // (1024 * 1024)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# Cache for _ollama_model_weights_mb, keyed (endpoint, model_tag). A tag's
# on-disk size is effectively immutable, and resource_status() runs on every
# scheduler tick, so re-probing /api/tags each time is pure waste. Staleness
# tradeoff: re-pulling the SAME tag with a different quantization would keep
# the old figure until the process restarts. That is acceptable for a
# fail-open advisory gate and is the normal cost of caching here; None
# results are deliberately NOT cached so a transient probe failure (or a
# model pulled after startup) is retried on the next tick.
_OLLAMA_MODEL_WEIGHTS_CACHE: dict[tuple[str, str], int] = {}


def _ollama_model_weights_mb(endpoint: str, model_tag: str) -> int | None:
    """Best-effort on-disk weight size (MB) for a specific Ollama model tag,
    read from `/api/tags` (every locally-pulled model, each with a `size` in
    bytes - a close proxy for its resident footprint once loaded, since
    Ollama's GGUF/safetensors weights are already quantized on disk).

    Used by `resource_status()`'s model-size gate. Ollama-specific: LM Studio
    JIT-loads with no equivalent listing, and mlx_lm.server pins one model for
    the server's whole lifetime (already covered by its own provider-scoped
    floor override) - never called for another provider.

    A cloud-served tag (e.g. `glm-5.2:cloud`) legitimately reports size 0 and
    is returned as 0, not None: it has no local footprint to gate on.

    Returns None (never raises) on any network/parse failure or when the tag
    isn't in the local model list - the caller then skips the gate, matching
    every other component's fail-open contract.
    """
    cached = _OLLAMA_MODEL_WEIGHTS_CACHE.get((endpoint, model_tag))
    if cached is not None:
        return cached
    try:
        resp = httpx.get(f"{endpoint}/api/tags", timeout=5)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    for entry in payload.get("models", []) or []:
        name = entry.get("name") or entry.get("model")
        if name != model_tag:
            continue
        size = entry.get("size")
        if isinstance(size, (int, float)):
            mb = int(size // (1024 * 1024))
            _OLLAMA_MODEL_WEIGHTS_CACHE[(endpoint, model_tag)] = mb
            return mb
    return None


def _ollama_loaded_models(endpoint: str) -> set[str]:
    """Return the set of model names currently loaded in Ollama's memory.

    Used by `dispatch_story` to warn when a multi-model concurrent dispatch
    is about to force Ollama to swap a different model into VRAM. Always
    checks Ollama specifically (not self.provider / PIPELINE_LOCAL_PROVIDER):
    this is a VRAM-swap warning, a concept specific to Ollama's one-process-
    many-models memory model (LM Studio JIT-loads instead, and mlx_lm.server
    is one model per process with nothing to swap) - not a leftover from
    dispatch() itself being non-provider-aware (see
    MODEL_PROVIDER_ABSTRACTION_PLAN.md S3).

    Network / parse failures are swallowed: the function is a
    observability hook, not a gate. Returning an empty set is fine; the
    caller will then see "nothing loaded" and skip the mismatch warning
    (a same-model dispatch is safe regardless of what's loaded).
    """
    return inference_providers.OllamaProvider().loaded_models(endpoint)


def _ollama_serving_parallelism() -> int | None:
    """Detect Ollama's actual serving parallelism at runtime.

    Ollama spawns one ``llama-server`` runner process per loaded model and
    passes it ``-np N`` (the ``OLLAMA_NUM_PARALLEL`` value the server was
    started with). ``N`` is the number of concurrent decode slots on that
    runner: with ``N=1`` a second in-flight request queues behind the
    first, and with ``MAX_CONCURRENT_AGENTS>1`` the pipeline will dispatch
    a second agent that then blocks on that queue until the 180s
    read-silence timeout fires ("LLM call failed: timed out" - Mode 2).

    The value is read from the live process table rather than trusted to
    stay in sync with ``OLLAMA_NUM_PARALLEL``: that env var is set via
    ``launchctl setenv`` and is silently dropped whenever Ollama.app
    auto-updates and relaunches (observed 2026-07-25, v0.32.4). A runtime
    probe is the only signal that survives an upgrade the user didn't
    initiate.

    Returns the smallest ``-np`` across all running ``llama-server``
    processes (the binding constraint on concurrent dispatch when multiple
    models are loaded), or ``None`` if no runner is running, ``ps`` fails,
    or no ``-np`` flag can be parsed. ``None`` means "unknown" - callers
    must NOT treat it as zero, which would false-warn on every first
    dispatch; they skip the warning instead. This is an observability
    hook, never a gate: any failure returns ``None`` rather than raising.
    """
    try:
        result = subprocess.run(  # noqa: PLW1510 (check=False would break test fakes with fixed signatures; see _ollama_loaded_models)
            ["ps", "-axo", "pid,command"], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # `-np N` is whitespace-delimited; anchor on a leading boundary so we
    # don't match a longer flag like `-npX` or a substring of another arg.
    np_re = re.compile(r"(?:^|\s)-np\s+(\d+)(?:\s|$)")
    smallest: int | None = None
    for line in result.stdout.splitlines():
        if "llama-server" not in line:
            continue
        m = np_re.search(line)
        if not m:
            continue
        val = int(m.group(1))
        if smallest is None or val < smallest:
            smallest = val
    return smallest


# Registry of available drivers by config name. Register new drivers here -
# orchestration code never changes.
#
# "local" is a permanent back-compat alias (env-resolved via
# PIPELINE_LOCAL_PROVIDER, default ollama) - manifests persist
# "backend": "local" for already-dispatched stories, so it must never be
# removed. "ollama"/"lmstudio"/"mlx" (RELIABILITY_PLAN.md T16) let a role
# name the actual local transport directly instead of the generic "local",
# which incorrectly implies "runs on this machine" when Ollama/LM Studio can
# equally proxy :cloud-tagged models - and lets different roles pin
# different local providers, since PIPELINE_LOCAL_PROVIDER is process-wide.
_DRIVERS: dict[str, type | Callable[[], OllamaDriver]] = {
    "claude": ClaudeCliDriver,
    "local": OllamaDriver,
    "ollama": functools.partial(OllamaDriver, provider_name="ollama"),
    "lmstudio": functools.partial(OllamaDriver, provider_name="lmstudio"),
    "mlx": functools.partial(OllamaDriver, provider_name="mlx"),
}


def get_backend(role: str | None = None, *, name: str | None = None) -> Backend:
    """Resolve the active backend.

    role=None returns ClaudeCliDriver unconditionally - used only by the
    Claude-specific usage probe (its `/cost` gate is Claude-only).

    For everything else, pass the role being executed: "dispatch", "review",
    or "overlord". Each is independently routable via PIPELINE_BACKEND_<ROLE>
    (e.g. PIPELINE_BACKEND_OVERLORD=local). Defaults to "claude".

    name= is a per-call override (e.g. a per-story backend choice stored in
    the manifest). When given it skips the env lookup entirely, except for the
    special value "auto" which is not a real driver and must be resolved by the
    caller before calling get_backend.

    "local" (OllamaDriver) implements complete() — both overlord-style
    single-shot prompts and review-style read-only tool loops (runs tests +
    reads, then submit_review) — and dispatch() (a native-tool-calling write
    agent loop, scripts/local_agent.py, run as a subprocess). So all three
    roles (overlord, review, dispatch) can be routed local via
    PIPELINE_BACKEND_<ROLE>=local.
    """
    if role is None:
        return ClaudeCliDriver()
    resolved = (name or "").strip().lower() if name else None
    if not resolved:
        env_var = f"PIPELINE_BACKEND_{role.upper()}"
        resolved = os.environ.get(env_var, "claude").strip().lower()
    if resolved == "auto":
        raise ValueError(
            "get_backend received name='auto', which is not a concrete driver. "
            "The caller must resolve 'auto' to 'local' or 'claude' via "
            "_route_dispatch_backend() before calling get_backend()."
        )
    driver_cls = _DRIVERS.get(resolved)
    if driver_cls is None:
        env_var = f"PIPELINE_BACKEND_{role.upper()}" if role else "(no role)"
        raise NotImplementedError(
            f"{env_var}={resolved!r} has no registered driver. Only "
            f"{sorted(_DRIVERS)} are implemented today."
        )
    return driver_cls()
