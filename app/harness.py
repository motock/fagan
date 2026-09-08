"""Agent-harness seam: value types, protocol, and the harness registry.

The AGENT HARNESS axis' counterpart to app/backend.py's Backend seam. This
module owns:

- ``HarnessRequest`` — the resolved inputs a caller hands a harness.
- ``HarnessCommand`` — the argv plus ADDITIONAL env a harness wants executed.
- ``AgentHarness`` — the protocol every harness adapter satisfies.
- ``register_harness`` / ``get_harness`` and the cumulative ``_HARNESSES``
  registry, keyed by names normalized with ``.strip().lower()``.

Fail-closed: ``get_harness`` raises ValueError for unknown names and never
substitutes a default harness — the same loud-failure posture as
``get_backend()`` for unknown drivers and the PIPELINE_EXEC_DISPATCH
fail-closed pattern. The registry starts EMPTY; adapters (the 'claude',
'local', and 'aider' harnesses) register themselves into it, and it is
never cleared or reset.

Import hygiene: ONLY the stdlib is imported here (dataclasses/typing).
Adapters import this module — never the reverse — so this file must not
import any app.*, pipeline.*, or scripts.* module (no import cycles, ever).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "AgentHarness",
    "AiderHarness",
    "ClaudeCliHarness",
    "HarnessCommand",
    "HarnessRequest",
    "LocalAgentHarness",
    "get_harness",
    "register_harness",
    "resolve_harness_name",
]


@dataclass(frozen=True)
class HarnessRequest:
    """Everything a harness needs to build and run one agent command.

    ``acceptance`` optionally carries the acceptance commands the run will be
    graded against. ``options`` carries harness-specific resolved values
    (e.g. ``python_executable`` / ``agent_script``) supplied by the calling
    driver; a harness reads the keys it knows and ignores the rest.
    """

    prompt: str
    system: str | None
    model: str
    cwd: str
    acceptance: list[str] | None = None
    options: dict | None = None


@dataclass(frozen=True)
class HarnessCommand:
    """The process a harness wants executed.

    ``env`` holds ONLY the ADDITIONAL environment variables this harness
    requires; the caller merges them over its own inherited environment.
    """

    argv: list[str]
    env: dict


class AgentHarness(Protocol):
    """A harness adapter: resolves a request into an executable command."""

    def build_agent_command(self, request: HarnessRequest) -> HarnessCommand:
        """Build the argv/env for ``request``. Pure: no I/O, no subprocess."""
        ...


# Cumulative registry of harness adapters, keyed by normalized name. Starts
# empty on purpose: there is no default harness, and unknown names fail
# closed. Later stories register 'claude' and 'local' here at import time.
_HARNESSES: dict[str, type] = {}


def register_harness(name: str, cls: type) -> None:
    """Register harness ``cls`` under ``name`` (normalized .strip().lower()).

    Re-registering the identical class is an idempotent no-op. Registering a
    DIFFERENT class under an existing name raises ValueError and leaves the
    original binding intact — the registry is never silently rebound.
    """
    key = name.strip().lower()
    if key in _HARNESSES and _HARNESSES[key] is not cls:
        raise ValueError(
            f"harness {key!r} is already registered to "
            f"{_HARNESSES[key].__name__!r}; refusing to rebind to "
            f"{cls.__name__!r}"
        )
    _HARNESSES[key] = cls


def get_harness(name: str) -> AgentHarness:
    """Return a fresh instance of the harness registered under ``name``.

    Fail-closed: an unknown name raises ValueError listing the registered
    names; there is no default harness and a failed lookup never mutates the
    registry. The class is constructed with no arguments on every call —
    harnesses are stateless adapters, not cached singletons.
    """
    key = name.strip().lower()
    if key not in _HARNESSES:
        registered = ", ".join(sorted(_HARNESSES)) or "(none)"
        raise ValueError(f"unknown harness {name!r}; registered: {registered}")
    return _HARNESSES[key]()


def resolve_harness_name(default: str) -> str:
    """Resolve the harness name from ``PIPELINE_AGENT_HARNESS``, or ``default``.

    Fail-closed: unset or whitespace-only is treated as "not selected" and
    returns ``default`` unchanged; any other value is normalized with
    ``.strip().lower()`` and must already be a registered harness name, or
    this raises ValueError naming the env var, the offending value, and the
    registered names. Never silently falls back to ``default`` on a typo'd
    or unregistered value.
    """
    raw = os.environ.get("PIPELINE_AGENT_HARNESS", "")
    resolved = raw.strip().lower()
    if not resolved:
        return default
    if resolved in _HARNESSES:
        return resolved
    raise ValueError(
        f"PIPELINE_AGENT_HARNESS={resolved!r} is not a registered harness; "
        f"registered: {sorted(_HARNESSES)}"
    )


class ClaudeCliHarness:
    """Harness adapter for the first-party Anthropic `claude` CLI.

    Builds EXACTLY the argv ClaudeCliDriver.dispatch has always spawned for
    an agent run — byte-identical construction, moved here so the driver
    delegates command building to the harness seam. Stateless and pure: no
    I/O, no subprocess, no cached per-call state.

    Env is deliberately NOT this adapter's business: it returns
    ``HarnessCommand.env == {}`` and the claude subprocess environment stays
    owned by the driver's ``_first_party_claude_env()`` (provider-redirect
    stripping). ``allowed_tools`` rides in ``request.options`` rather than
    being a HarnessRequest field.
    """

    def build_agent_command(self, request: HarnessRequest) -> HarnessCommand:
        """Build the `claude -p` argv for ``request``."""
        # stream-json (+ the verbose it requires) makes claude emit an event
        # immediately on startup and one per tool call, instead of buffering
        # everything until the final answer. check_story_status's "0 bytes
        # after exit -> failed launch" check depends on that: without
        # streaming, a long-running-but-legitimate agent looks identical to
        # one that never started.
        argv = ["claude", "-p", request.prompt, "--model", request.model,
                "--output-format", "stream-json", "--verbose"]
        if request.system:
            argv += ["--append-system-prompt", request.system]
        allowed_tools = (request.options or {}).get("allowed_tools")
        if allowed_tools:
            argv += ["--allowedTools", allowed_tools]
        # This is a one-shot headless subprocess with no external harness to
        # ever revisit a scheduled wakeup - ScheduleWakeup's "the harness
        # re-invokes you later" contract is meaningless here and, if the
        # agent defers to it and ends its turn, the process just exits and
        # any pending background task is orphaned/killed with no commit ever
        # landing (observed live 2026-08-07, 4 identical rework parks on
        # story 4bfcc3b4). Block it outright rather than relying on the
        # prompt alone.
        argv += ["--disallowedTools", "ScheduleWakeup"]
        return HarnessCommand(argv=argv, env={})


register_harness("claude", ClaudeCliHarness)


class LocalAgentHarness:
    """Harness adapter for the local-model agent (scripts/local_agent.py).

    Builds EXACTLY the argv and the six base ``LOCAL_AGENT_*`` env keys
    OllamaDriver.dispatch has always spawned for a local agent run —
    byte-identical construction, moved here so the driver delegates command
    building to the harness seam. Stateless and pure: no I/O, no subprocess,
    no cached per-call state.

    Env is deliberately MINIMAL: only the six base keys every local-agent
    spawn needs (model/system/task/endpoint/timeout/provider). The driver
    keeps owning everything dispatch-specific — num_ctx/temperature/max_steps,
    think, oracle acceptance/mode, rework, transcript/resume — and merges
    those over ``command.env`` afterward, so a stale ``LOCAL_AGENT_*`` value
    exported in the inherited environment still loses to the harness value
    (the merge order ``{**os.environ, **command.env}`` is the caller's job).
    """

    def build_agent_command(self, request: HarnessRequest) -> HarnessCommand:
        """Build the local-agent argv + base env for ``request``."""
        options = request.options or {}
        argv = [options["python_executable"], options["agent_script"]]
        env = {
            "LOCAL_AGENT_MODEL": options["model"],
            "LOCAL_AGENT_SYSTEM": options["system"],
            "LOCAL_AGENT_TASK": options["task"],
            "LOCAL_AGENT_ENDPOINT": options["endpoint"],
            "LOCAL_AGENT_TIMEOUT": options["timeout"],
            "LOCAL_AGENT_PROVIDER": options["provider"],
        }
        return HarnessCommand(argv=argv, env=env)


register_harness("local", LocalAgentHarness)


class AiderHarness:
    """Harness adapter for the third-party `aider` CLI (aider-chat 0.86.2).

    Stateless and pure: no I/O, no subprocess, no cached per-call state —
    every command is derived from the request alone, and a rejected call
    leaves nothing behind. The single source of truth for every flag emitted
    below is docs/specs/AIDER_HARNESS.md (the aider --help capture and its
    contract table); no flag is invented here.

    Documented compromises (both are the spec's own Gap verdicts, not
    silent drops):

    - ``request.system``: Aider has NO system-prompt flag (the capture has no
      such option; its system prompts are fixed per edit-format), so the
      system text is PREPENDED to the prompt inside the one ``--message``
      element, separated by a blank line. The separator is emitted only when
      system text exists — never unconditionally.
    - ``request.acceptance``: IGNORED. Aider has no oracle concept; its
      ``--test``/``--test-cmd`` flags are a single self-run command that also
      fixes failures (mutating the worktree), not pipeline-graded acceptance.
      Per the spec, acceptance stays with the pipeline, which runs it after
      aider exits.

    Unknown-but-nonempty model strings pass through UNCHANGED: this class is
    a pure argv builder, and validating vendor model names is not its job
    (the registry-name normalization ``.strip().lower()`` is deliberately
    NOT applied to the model — ``"GLM-4"`` must not become ``"glm-4"``).

    API keys are NEVER placed in argv (argv is world-readable via ``ps`` for
    the process's whole lifetime). A key supplied in ``request.options``
    (``openai_api_key`` / ``anthropic_api_key``, mirroring aider's
    ``--openai-api-key`` / ``--anthropic-api-key`` flags) is emitted only in
    ``HarnessCommand.env`` under the provider-native variable litellm
    actually consults (docs/specs/AIDER_HARNESS.md, Environment). ``env``
    carries ONLY those additional variables — never a copy of ``os.environ``
    (the caller merges).
    """

    def build_agent_command(self, request: HarnessRequest) -> HarnessCommand:
        """Build the non-interactive `aider` argv (+ key env) for ``request``."""
        # Validate FIRST, before any argv exists: a whitespace-only prompt
        # would otherwise launch aider with no instruction at all (a non-empty
        # "   " string is truthy, so `if not request.prompt` alone would let
        # it through).
        if not (request.prompt or "").strip():
            raise ValueError(
                f"HarnessRequest.prompt is empty or whitespace-only "
                f"({request.prompt!r}); aider would be launched with no "
                f"instruction"
            )
        if not (request.model or "").strip():
            raise ValueError(
                f"HarnessRequest.model is empty or whitespace-only "
                f"({request.model!r}); aider would fall back to its own "
                f"default model instead of the requested one"
            )
        # Options are read only via `.get` — every key here is optional, so
        # `request.options=None` must yield no key and no crash (the
        # near-miss `request.options["openai_api_key"]` raises TypeError on
        # None). Unknown keys are ignored by contract.
        options = request.options or {}
        openai_key = options.get("openai_api_key")
        anthropic_key = options.get("anthropic_api_key")
        # system handling: the spec's contract table resolves the
        # system/convention-file row as "NO equivalent" — there is no
        # system-prompt flag to use — so the prepend path below is the
        # documented compromise. The separator appears only when system text
        # exists (system=None must leave the message element exactly the
        # prompt, not "\n\n<prompt>").
        if request.system:
            message = f"{request.system}\n\n{request.prompt}"
        else:
            message = request.prompt
        # request.acceptance is deliberately IGNORED: Aider has no oracle
        # concept (see class docstring) — acceptance stays with the pipeline.
        # Flag order mirrors the spec's own probe invocation
        # (`aider --model M --message x --yes-always --no-auto-commits
        # --no-dirty-commits --no-pretty --no-stream`); --no-fancy-input is
        # the contract table's row for the non-interactive child's raw-mode
        # terminal handling.
        argv = [
            "aider",
            "--model", request.model,
            "--message", message,
            "--yes-always",
            "--no-auto-commits",
            "--no-dirty-commits",
            "--no-pretty",
            "--no-stream",
            "--no-fancy-input",
        ]
        # env carries ONLY the additional variables this harness requires —
        # never a copy of os.environ (the caller merges over its own env).
        env: dict = {}
        if openai_key:
            env["OPENAI_API_KEY"] = openai_key
        if anthropic_key:
            env["ANTHROPIC_API_KEY"] = anthropic_key
        return HarnessCommand(argv=argv, env=env)


register_harness("aider", AiderHarness)