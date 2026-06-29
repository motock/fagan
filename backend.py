"""LLM backend abstraction — the seam between orchestration and execution.

pipeline_mcp_server.py owns the *why* (state machine, gating, decisions). This
module owns the *how* (spawning a model to do the work). ClaudeCliDriver wraps
today's `claude` CLI subprocess calls with identical mechanics to what it
replaced; a future local-model driver implements the same Backend protocol so
the orchestrator never depends on which backend runs a given role.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx


@dataclass
class AgentHandle:
    """A non-blocking agentic run (dispatch/review-style), identified by pid."""
    pid: int


class Backend(Protocol):
    def complete(
        self, prompt: str, *, system: str | None, model: str,
        allowed_tools: str | None = None, cwd: str | None = None,
    ) -> str:
        """Run a blocking, single invocation and return its captured output."""
        ...

    def dispatch(
        self, prompt: str, *, system: str | None, model: str,
        allowed_tools: str | None, cwd: Path, log_path: Path, append: bool,
    ) -> AgentHandle:
        """Spawn a non-blocking agentic run, streaming output to log_path."""
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


class ClaudeCliDriver:
    """Backend driver wrapping the `claude` CLI."""

    def complete(
        self, prompt: str, *, system: str | None = None, model: str,
        allowed_tools: str | None = None, cwd: str | None = None,
    ) -> str:
        cmd = ["claude", "-p", prompt, "--model", model]
        if system:
            cmd += ["--append-system-prompt", system]
        if allowed_tools:
            cmd += ["--allowedTools", allowed_tools]
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        return proc.stdout

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
        log_file = open(log_path, "a" if append else "w")
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=log_file, stderr=log_file)
        log_file.close()
        return AgentHandle(pid=proc.pid)

    def usage_probe_text(self) -> str:
        proc = subprocess.run(
            ["claude", "-p", "/cost", "--output-format", "json"],
            capture_output=True, text=True, check=True,
        )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Usage probe returned invalid JSON: {e}") from e
        return payload.get("result", "")

    def resource_status(self) -> dict:
        """Claude's gate is the poller-fed, hysteresis-stabilized usage state
        (see pipeline_mcp_server.check_usage / _usage_gate), not a live /cost
        probe — reading the cached `paused` flag here is cheap and reflects the
        same decision the poller already made. Imported locally because the
        orchestrator imports this module (a top-level import would cycle); by
        call time pipeline_mcp_server is fully loaded. Failing open (ok) on
        missing/garbled state matches check_usage's own fail-open behavior.
        """
        import pipeline_mcp_server as _p  # local: avoids an import cycle
        paused = bool(_p._read_usage_state().get("paused", False))
        return {"ok": not paused, "reason": "Claude usage gate tripped" if paused else ""}


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


def _resolve_local_model(tier: str) -> str:
    default = os.environ.get("PIPELINE_LOCAL_MODEL_DEFAULT", _LOCAL_DEFAULT_MODEL)
    env_var = _LOCAL_TIER_ENV.get(tier.lower())
    return os.environ.get(env_var, default) if env_var else default


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
        import pipeline_mcp_server as _p  # local: avoid import cycle at module load
        import shlex
        cmd = args.get("command", "")
        try:
            argv0 = shlex.split(cmd)[0] if cmd.strip() else ""
        except ValueError:
            argv0 = ""
        if argv0 and _p._is_heavy([argv0]):
            with _p._heavy_lock():
                pr = subprocess.run(cmd, shell=True, cwd=cwd,
                                    capture_output=True, text=True)
        else:
            pr = subprocess.run(cmd, shell=True, cwd=cwd,
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

    def __init__(self) -> None:
        self.endpoint = os.environ.get(
            "PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434",
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
    ) -> str:
        # Review-style call: allowed_tools includes Bash and a worktree cwd is
        # given (see _run_reviewer). The model must actually run the tests and
        # read files, then emit a VERDICT — so run a blocking read-only tool
        # loop, mirroring how ClaudeCliDriver.complete() transparently runs a
        # tool loop when `claude -p` is given allowed_tools+cwd. Overlord-style
        # calls (allowed_tools="Read", no cwd) fall through to single-shot.
        if cwd is not None and allowed_tools and "Bash" in allowed_tools.split(","):
            return self._review_loop(prompt, system=system, model=model, cwd=cwd)

        resolved_model = _resolve_local_model(model)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            payload = self._chat(messages, resolved_model)
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"Local backend at {self.endpoint} (model={resolved_model}) "
                f"is unreachable or errored: {e}"
            ) from e
        return payload["content"]

    def _chat(self, messages: list, model: str, tools: list | None = None) -> dict:
        body = {
            "model": model, "messages": messages, "stream": False,
            "options": {"num_ctx": self.num_ctx, "temperature": self.temperature},
        }
        if tools:
            body["tools"] = tools
        resp = httpx.post(f"{self.endpoint}/api/chat", json=body, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()["message"]

    # Read-only tools for the review loop. No create/edit — review must not
    # modify the tree (the "review does not merge / does not edit" guarantee).
    # bash is needed to run the test suite; like the Claude reviewer's
    # Bash+Read, it is not sandboxed, but it runs in the isolated worktree.
    # submit_review is the explicit terminator (like dispatch's `done`): far
    # more reliable for a weak local model than scanning free prose for a
    # VERDICT line, which in testing it failed to emit cleanly.
    _REVIEW_TOOLS = [
        {"type": "function", "function": {
            "name": "bash", "description": "Run a bash command in the worktree (run tests, git diff/log, etc.).",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
        {"type": "function", "function": {
            "name": "view_file", "description": "Show a file's contents with line numbers.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
        {"type": "function", "function": {
            "name": "submit_review", "description": "Submit your final review verdict. Call this once, after running the tests and inspecting the changes.",
            "parameters": {"type": "object", "properties": {
                "verdict": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES"]},
                "summary": {"type": "string", "description": "brief justification"},
                "pr_title": {"type": "string", "description": "PR title (if APPROVE)"},
                "pr_body": {"type": "string", "description": "PR body (if APPROVE)"}},
                "required": ["verdict"]}}},
    ]

    def _review_loop(self, prompt: str, *, system: str | None, model: str, cwd: str) -> str:
        """Blocking read-only agentic loop for review. The model runs tests and
        reads files via tools, then calls submit_review with its verdict. Returns
        a `VERDICT: ...` text block (so pipeline_mcp_server._parse_verdict, shared
        with the Claude path, scans it unchanged). Uses the same tolerant parsing
        as dispatch, plus key-based tool inference, since the local model
        intermittently emits tool calls as text and drops the tool name."""
        resolved_model = _resolve_local_model(model)
        preamble = (
            "You are reviewing code in the current directory, READ-ONLY. First use "
            "the bash tool to run the test suite and inspect the changes (e.g. "
            "`git diff`, `git log -p -1`), and view_file to read files — do not edit "
            "anything. Then call submit_review exactly once with verdict APPROVE or "
            "REQUEST_CHANGES (REQUEST_CHANGES if the tests fail). Always call a tool; "
            "do not answer in prose."
        )
        system_content = preamble + ("\n\n" + system if system else "")
        messages = [{"role": "system", "content": system_content},
                    {"role": "user", "content": prompt}]
        nudged = False
        final_nudged = False
        last_prose = ""
        for i in range(self.review_max_steps):
            # The local model investigates thoroughly but rarely converges to
            # the submit_review terminator on its own. It will call it when
            # pushed (the same model calls `done` in dispatch mode), so when the
            # step budget is nearly spent without a verdict, nudge once to press
            # for a verdict. Without this the loop exhausts into UNKNOWN, the
            # orchestrator records changes_requested with empty feedback, and
            # the redispatched agent re-runs blind -> review/rework loop -> park.
            if (self.review_max_steps - i) <= 5 and not final_nudged:
                messages.append({"role": "user", "content":
                    "You have few review steps left. Stop investigating and call "
                    "submit_review now with verdict APPROVE or REQUEST_CHANGES "
                    "(or end your reply with a 'VERDICT: APPROVE' or "
                    "'VERDICT: REQUEST_CHANGES' line)."})
                final_nudged = True
            try:
                m = self._chat(messages, resolved_model, tools=self._REVIEW_TOOLS)
            except httpx.HTTPError as e:
                raise RuntimeError(
                    f"Local backend at {self.endpoint} (model={resolved_model}) "
                    f"is unreachable or errored during review: {e}"
                ) from e
            messages.append(m)
            if m.get("content"):
                last_prose = m["content"]
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
                fn = tc["function"]["name"]
                args = tc["function"]["arguments"]
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if fn == "submit_review":
                    verdict = str(args.get("verdict", "")).upper()
                    if verdict not in ("APPROVE", "REQUEST_CHANGES"):
                        verdict = "REQUEST_CHANGES"  # malformed -> safe default
                    body = args.get("pr_body") or args.get("summary", "")
                    title = args.get("pr_title", "")
                    return f"VERDICT: {verdict}\n\n{title}\n{body}".strip()
                messages.append({"role": "tool", "content": _run_readonly_tool(fn, args, Path(cwd))})
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
            return lines[-1]
        return ""

    # The local agent loop lives in a standalone script so it can run as a
    # pollable subprocess; run it with this project's venv python (which has
    # httpx and can import pipeline_mcp_server for in-process checkpointing).
    _AGENT_SCRIPT = Path(__file__).resolve().parent / "scripts" / "local_agent.py"
    _AGENT_SCRIPT_ORACLE = Path(__file__).resolve().parent / "scripts" / "local_agent_oracle.py"
    _VENV_PYTHON = Path(__file__).resolve().parent / ".venv" / "bin" / "python3"

    def dispatch(
        self, prompt: str, *, system: str | None = None, model: str,
        allowed_tools: str | None = None, cwd: Path, log_path: Path, append: bool,
        acceptance: list[str] | None = None,
    ) -> AgentHandle:
        if allowed_tools and not ({"Edit", "Write"} & set(allowed_tools.split(","))):
            raise NotImplementedError(
                f"OllamaDriver.dispatch is a writing/coding harness and cannot "
                f"honor read-only allowed_tools={allowed_tools!r} (it would let "
                f"the agent edit files anyway). Keep this role on claude until a "
                f"read-only local tool set is implemented and verified."
            )
        resolved_model = _resolve_local_model(model)
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
            "LOCAL_AGENT_NUM_CTX": str(self.num_ctx),
            "LOCAL_AGENT_TIMEOUT": str(self.dispatch_timeout),
            "LOCAL_AGENT_MAX_STEPS": str(self.max_steps),
            "LOCAL_AGENT_TEMPERATURE": str(self.temperature),
        }
        if oracle_mode:
            env["LOCAL_AGENT_ACCEPTANCE"] = json.dumps(acceptance)
            env["LOCAL_AGENT_MODE"] = "oracle"
        argv = [str(self._VENV_PYTHON), str(agent_script)]
        log_file = open(log_path, "a" if append else "w")
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log_file, stderr=log_file)
        log_file.close()
        return AgentHandle(pid=proc.pid)

    def usage_probe_text(self) -> str:
        raise NotImplementedError(
            "OllamaDriver has no usage/cost concept - its resource gate is "
            "resource_status() (Ollama reachability), not a /cost probe."
        )

    def resource_status(self) -> dict:
        """Local backend has no usage/cost limit to respect, so the only gate
        is whether the Ollama endpoint is up. (The concurrency ceiling is
        enforced separately by advance_pipeline via MAX_CONCURRENT_AGENTS.)
        This is what unlocks overnight autonomy decoupled from Claude's weekly
        limit: as long as Ollama is reachable, local dispatch keeps running.
        """
        try:
            resp = httpx.get(f"{self.endpoint}/api/tags", timeout=10)
            resp.raise_for_status()
            return {"ok": True, "reason": ""}
        except httpx.HTTPError as e:
            return {"ok": False, "reason": f"Ollama endpoint {self.endpoint} unreachable: {e}"}


# Registry of available drivers by config name. Register new drivers here -
# orchestration code never changes.
_DRIVERS: dict[str, type] = {
    "claude": ClaudeCliDriver,
    "local": OllamaDriver,
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
