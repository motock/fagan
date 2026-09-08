from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from app.backend_types import AgentHandle
from app.harness import (
    HarnessRequest,
    aider_binary_available,
    get_harness,
    resolve_harness_name,
)
from pipeline import execution

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
        bare: bool = False, allowed_tools: str | None = None, cwd: str | None = None,
        max_tokens: int | None = None, cell_dir: str | None = None,
        role: str = "complete",
    ) -> str:
        cmd = ["claude", "-p", prompt, "--model", model]
        if bare:
            cmd += ["--bare"]
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
            cell_dir=cell_dir, role=role,
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
        request = HarnessRequest(prompt=prompt, system=system, model=model,
                                 cwd=str(cwd),
                                 options={"allowed_tools": allowed_tools})
        name = resolve_harness_name("claude")
        if name == "aider":
            # PIPELINE_AGENT_HARNESS=aider: honor the operator's selection,
            # but only when the aider binary actually resolves on PATH.
            # Fail closed BEFORE any spawn/worktree side effect — never fall
            # back to the native 'claude' harness, which would run a
            # completely different agent than the one selected.
            available, reason = aider_binary_available()
            if not available:
                raise RuntimeError(
                    "PIPELINE_AGENT_HARNESS=aider is set, but the aider binary "
                    f"is not available: {reason}"
                )
            # Spawn aider exactly the way AiderHarness builds it, mirroring
            # the native spawn below: same spawn_harness call shape, same
            # return value. HarnessCommand.env is ADDITIONAL-vars-only (see
            # its docstring), and spawn_local forwards env straight to
            # subprocess.Popen(env=...), which REPLACES the child
            # environment — so merge it over os.environ here, exactly like
            # OllamaDriver.dispatch does, or the aider child would be
            # spawned with an empty environment (no PATH/HOME). On this
            # dispatch path command.env is usually {} (dispatch never
            # supplies provider keys), but the merge is what keeps the
            # child's inherited environment intact either way.
            command = get_harness(name).build_agent_command(request)
            handle = execution.spawn_harness(
                command.argv, cwd=cwd, log_path=log_path, append=append,
                env={**os.environ, **command.env},
            )
            handle.model = model
            return handle
        if name != "claude":
            raise NotImplementedError(
                f"PIPELINE_AGENT_HARNESS={name!r}: ClaudeCliDriver only implements "
                f"the 'claude' harness; a cross-harness selection is a configuration error, not a silent fallback"
            )
        cmd = get_harness(name).build_agent_command(request).argv
        handle = execution.spawn_harness(
            cmd, cwd=cwd, log_path=log_path, append=append,
            env=_first_party_claude_env(),
        )
        handle.model = model
        return handle

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
