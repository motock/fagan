"""Local inference server wire-protocol abstraction.

backend.py's OllamaDriver owns the harness mechanics that every local
inference server shares regardless of vendor: the native-tool-calling
dispatch loop, the read-only review loop, checkpoint plumbing, and the
per-call cost sidecar. The only thing that actually differs between Ollama,
LM Studio, MLX (mlx_lm.server), vLLM, and llama.cpp-server is the wire
protocol to the inference server itself - so that (and only that) is what
this module isolates.

Selection is via PIPELINE_LOCAL_PROVIDER (default "ollama"): "ollama", "mlx",
and "lmstudio" are all working implementations - MLXProvider's and
LMStudioProvider's wire formats were each captured live against a running
server (see their docstrings), not assumed from the OpenAI spec alone.

httpx is a shared singleton module (sys.modules["httpx"]): a test that
monkeypatches backend.httpx.post/get also intercepts calls made from here,
since both modules hold a reference to the exact same module object. No
special test wiring is needed for the two to share mocks.
"""
from __future__ import annotations

import math
import os
from typing import Protocol

import httpx

# The 2026-09-02 scheduler freeze (daemon pid 1609 blocked ~40 min in
# sock_recv on localhost:11434) was caused by an unbounded read somewhere in
# the scheduler-side chat/complete path. Every outbound chat/complete httpx
# call now carries a finite timeout resolved from this env var at call time
# (never cached at import: a test that sets the env after import must see the
# new value on the very next call). Unset, malformed, or non-positive values
# fall back to the hardcoded 600s default - never to None, which httpx treats
# as "no timeout" and which is exactly the incident shape.
ROLE_CALL_TIMEOUT_ENV = "PIPELINE_ROLE_CALL_TIMEOUT_SECONDS"
_ROLE_CALL_TIMEOUT_DEFAULT_S = 600.0


def resolve_role_call_timeout() -> float:
    """Resolve the per-attempt chat/complete HTTP timeout in seconds.

    Read at call time from PIPELINE_ROLE_CALL_TIMEOUT_SECONDS (float seconds;
    fractional values like "0.2" are valid). Falls back to the hardcoded 600
    default when the variable is unset, blank, unparseable, non-positive, or
    non-finite - the resolver never returns None, zero, or inf, so every
    caller that passes its result to httpx gets a bounded read.
    """
    raw = os.environ.get(ROLE_CALL_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return _ROLE_CALL_TIMEOUT_DEFAULT_S
    try:
        value = float(raw)
    except ValueError:
        return _ROLE_CALL_TIMEOUT_DEFAULT_S
    if not math.isfinite(value) or value <= 0:
        return _ROLE_CALL_TIMEOUT_DEFAULT_S
    return value


class RateLimitedError(RuntimeError):
    """Raised by a local inference provider when the chat endpoint returns
    429. Distinct from the generic RuntimeError that wraps other httpx
    errors (network failures, 5xx, etc.) so the orchestrator can route a
    transient rate-limit to the same deferral path used for Claude's usage
    pause, instead of misclassifying it as an inconclusive review and
    burning the rework budget. Re-exported as backend.RateLimitedError for
    existing callers/tests.
    """


class LocalInferenceProvider(Protocol):
    """The seam between OllamaDriver's harness mechanics and whatever local
    inference server actually serves the model. See the module docstring."""

    name: str
    default_endpoint: str

    def chat(
        self, messages: list, *, model: str, num_ctx: int, temperature: float,
        tools: list | None = None, endpoint: str | None = None,
        timeout: float | None = None, think: bool | str | None = None,
    ) -> dict:
        """Blocking, non-streaming chat completion. Returns Ollama's native
        envelope shape - {"message": {"role", "content", "tool_calls"?},
        "prompt_eval_count", "eval_count"} - regardless of the backend's own
        wire format, so OllamaDriver's existing complete()/_review_loop code
        (which reads envelope["message"] and envelope.get("prompt_eval_count"/
        "eval_count")) works unchanged no matter which provider is active.
        A provider whose native format differs (e.g. MLXProvider's OpenAI-
        shaped choices[0].message/usage) translates into this shape itself.

        think, when not None, is forwarded as Ollama's top-level "think"
        request field (bool to toggle reasoning on/off, or a level string -
        "low"/"medium"/"high"/"max" - for models that support graded
        reasoning effort; live-validated against gemma4:12b-mlx, which 400s
        on any other string). Omitted from the request when None so a
        provider/model with no opinion on reasoning gets an unchanged body.
        Only OllamaProvider acts on it - LMStudioProvider/MLXProvider accept
        the kwarg for signature parity but their wire protocols have no
        equivalent field, so it's a silent no-op there."""
        ...

    def loaded_models(self, endpoint: str) -> set[str]:
        """Model names currently loaded in the server's memory, or an empty
        set if the server has no such concept (e.g. one-model-per-process
        servers like mlx_lm.server) or the probe fails - never raises."""
        ...

    def reachable(self, endpoint: str) -> tuple[bool, str]:
        """(ok, reason) - whether the server is up. Never raises; backs
        OllamaDriver.resource_status()."""
        ...


class OllamaProvider:
    """Ollama's native /api/chat endpoint (not the OpenAI-compatible /v1
    surface - only the native API accepts `options.num_ctx`, which matters
    in practice: see OllamaDriver's docstring in backend.py)."""

    name = "ollama"
    default_endpoint = "http://localhost:11434"

    def chat(
        self, messages: list, *, model: str, num_ctx: int, temperature: float,
        tools: list | None = None, endpoint: str | None = None,
        timeout: float | None = None, think: bool | str | None = None,
    ) -> dict:
        endpoint = (endpoint or self.default_endpoint).rstrip("/")
        body = {
            "model": model, "messages": messages, "stream": False,
            "options": {"num_ctx": num_ctx, "temperature": temperature},
        }
        if tools:
            body["tools"] = tools
        if think is not None:
            body["think"] = think
        # None (defaulted or explicit) is coerced to the resolved role-call
        # timeout - never an unbounded read on the wire (2026-09-02 freeze).
        wire_timeout = (
            timeout if timeout is not None else resolve_role_call_timeout()
        )
        resp = httpx.post(f"{endpoint}/api/chat", json=body, timeout=wire_timeout)
        # Detect 429 before raise_for_status() converts it to a generic
        # HTTPError - Ollama-cloud (and any upstream proxy) rate-limits per
        # host, so a 429 is a transient "defer and retry" signal, not a
        # real backend failure.
        if resp.status_code == 429:
            raise RateLimitedError(
                f"Local backend at {endpoint} (model={model}) "
                f"returned 429 (rate limited)"
            )
        resp.raise_for_status()
        return resp.json()

    def loaded_models(self, endpoint: str) -> set[str]:
        try:
            resp = httpx.get(f"{endpoint}/api/ps", timeout=5)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError):
            return set()
        out: set[str] = set()
        for entry in payload.get("models", []) or []:
            name = entry.get("name") or entry.get("model")
            if name:
                out.add(name)
        return out

    def reachable(self, endpoint: str) -> tuple[bool, str]:
        try:
            resp = httpx.get(f"{endpoint}/api/tags", timeout=10)
            resp.raise_for_status()
            return True, ""
        except httpx.HTTPError as e:
            return False, f"Ollama endpoint {endpoint} unreachable: {e}"


class LMStudioProvider:
    """LM Studio's OpenAI-compatible /v1/chat/completions endpoint.

    Request/response shapes below are captured live (2026-07-10) against LM
    Studio's `lms` server serving google/gemma-4-e4b, not assumed from the
    OpenAI spec alone:

    - Non-streaming response: {"choices": [{"message": {"role": "assistant",
      "content": str, "reasoning_content": str, "tool_calls": [{"type":
      "function", "id": <string>, "function": {"name": str, "arguments":
      <JSON-encoded string>}}]}}], "usage": {"prompt_tokens": int,
      "completion_tokens": int, "total_tokens": int, ...}}.
      tool_calls[].function.arguments is a JSON-encoded STRING (same as
      MLX/OpenAI) - backend.py's tool-calling loop already handles a string
      arguments field (json.loads), so it needs no extra parsing here.
      Unlike MLX's server (which always returns null), tool_calls[].id is a
      real non-null string here - neither detail matters to the caller,
      which only reads .function.name/.arguments.
    - Per-request `temperature` is honored. There is no per-request
      context-window control like Ollama's options.num_ctx (context length
      is fixed by the loaded model, reported as loaded_context_length by
      /api/v0/models) - `num_ctx` is instead sent as `max_tokens` (an
      output-length budget, not a true equivalent), consistent with
      MLXProvider's approach.
    - Loaded-model state comes from LM Studio's own /api/v0/models endpoint
      (a `state` field per model: "loaded"/"not-loaded"), not Ollama's
      /api/ps - a model can be downloaded but not currently loaded into
      memory, and LM Studio JIT-loads a model on its first request (observed
      live: ~30s for a 4B model) rather than requiring it pre-loaded like
      Ollama expects.
    """

    name = "lmstudio"
    default_endpoint = "http://localhost:1234"

    def chat(
        self, messages: list, *, model: str, num_ctx: int, temperature: float,
        tools: list | None = None, endpoint: str | None = None,
        timeout: float | None = None, think: bool | str | None = None,
    ) -> dict:
        # think accepted for signature parity with LocalInferenceProvider -
        # LM Studio's OpenAI-compatible endpoint has no equivalent field, so
        # it's intentionally unused here.
        endpoint = (endpoint or self.default_endpoint).rstrip("/")
        body = {
            "model": model, "messages": messages, "stream": False,
            "temperature": temperature,
            "max_tokens": num_ctx,
        }
        if tools:
            body["tools"] = tools
        # None (defaulted or explicit) is coerced to the resolved role-call
        # timeout - never an unbounded read on the wire (2026-09-02 freeze).
        wire_timeout = (
            timeout if timeout is not None else resolve_role_call_timeout()
        )
        resp = httpx.post(
            f"{endpoint}/v1/chat/completions", json=body, timeout=wire_timeout)
        if resp.status_code == 429:
            raise RateLimitedError(
                f"Local backend at {endpoint} (model={model}) "
                f"returned 429 (rate limited)"
            )
        resp.raise_for_status()
        payload = resp.json()
        message = payload["choices"][0]["message"]
        usage = payload.get("usage") or {}
        return {
            "message": message,
            "prompt_eval_count": usage.get("prompt_tokens", 0),
            "eval_count": usage.get("completion_tokens", 0),
        }

    def loaded_models(self, endpoint: str) -> set[str]:
        try:
            resp = httpx.get(f"{endpoint}/api/v0/models", timeout=5)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError):
            return set()
        return {
            entry["id"] for entry in payload.get("data", []) or []
            if entry.get("state") == "loaded"
        }

    def reachable(self, endpoint: str) -> tuple[bool, str]:
        try:
            resp = httpx.get(f"{endpoint}/v1/models", timeout=10)
            resp.raise_for_status()
            return True, ""
        except httpx.HTTPError as e:
            return False, f"LM Studio endpoint {endpoint} unreachable: {e}"


class MLXProvider:
    """mlx_lm.server's OpenAI-compatible /v1/chat/completions endpoint.

    Request/response shapes below are captured live (2026-07-09) against
    mlx_lm 0.28.3 + mlx-community/Qwen2.5-1.5B-Instruct-4bit, not assumed
    from the OpenAI spec alone:

    - Non-streaming response: {"choices": [{"message": {"role": "assistant",
      "content": str, "tool_calls": [{"type": "function", "function":
      {"name": str, "arguments": <JSON-encoded string>}, "id": null}]}}],
      "usage": {"prompt_tokens": int, "completion_tokens": int,
      "total_tokens": int}}. tool_calls[].function.arguments is a
      JSON-encoded STRING (OpenAI shape) - backend.py's tool-calling loop
      already handles a string arguments field (json.loads), so it needs no
      extra parsing here. tool_calls[].id is always null on this server
      version; do not rely on it.
    - Per-request `temperature` is honored. There is no per-request
      context-window control like Ollama's options.num_ctx (context is fixed
      at model-load time) - `num_ctx` is instead sent as `max_tokens` (an
      output-length budget, not a true equivalent) so a caller's context-size
      choice doesn't silently get clamped to the server's tiny 512-token
      default.
    - A model that emits malformed tool-call JSON crashes the server's
      request handler (observed live: an uncaught json.JSONDecodeError resets
      the connection) rather than returning a 4xx - this surfaces to httpx as
      a transport error, itself an httpx.HTTPError subclass, so it's already
      covered by callers' existing `except httpx.HTTPError` handling with no
      extra work here.
    - mlx_lm.server serves exactly one model per process, so unlike Ollama
      there is no VRAM-swap concept to warn about - loaded_models() returns
      an empty set without making any network call.
    - The request body never includes a "model" field. mlx_lm.server's
      ModelProvider.load() only reuses the preloaded weights when the
      request's model string maps (via an internal alias table) to the
      exact value the server was launched with (its --model CLI argument);
      any other string - including the model's own metadata id, the same
      one /v1/models reports - makes it attempt a fresh model resolution,
      which can hang indefinitely (observed live 2026-07-14: two separate
      "server never responds" incidents were actually this mismatch, not a
      broken download or a corrupt model). Since the server only ever hosts
      one model, there is nothing to select per-request - omitting "model"
      entirely makes it fall back to its preloaded default unconditionally.
      `model` is still accepted as a parameter here (used in the
      RateLimitedError message) for signature parity with the other
      providers, which do need per-request model selection.
    """

    name = "mlx"
    default_endpoint = "http://localhost:8080"

    def chat(
        self, messages: list, *, model: str, num_ctx: int, temperature: float,
        tools: list | None = None, endpoint: str | None = None,
        timeout: float | None = None, think: bool | str | None = None,
    ) -> dict:
        # think accepted for signature parity with LocalInferenceProvider -
        # mlx_lm.server's OpenAI-compatible endpoint has no equivalent
        # field, so it's intentionally unused here.
        endpoint = (endpoint or self.default_endpoint).rstrip("/")
        body = {
            "messages": messages, "stream": False,
            "temperature": temperature,
            "max_tokens": num_ctx,
        }
        if tools:
            body["tools"] = tools
        # None (defaulted or explicit) is coerced to the resolved role-call
        # timeout - never an unbounded read on the wire (2026-09-02 freeze).
        wire_timeout = (
            timeout if timeout is not None else resolve_role_call_timeout()
        )
        resp = httpx.post(
            f"{endpoint}/v1/chat/completions", json=body, timeout=wire_timeout)
        if resp.status_code == 429:
            raise RateLimitedError(
                f"Local backend at {endpoint} (model={model}) "
                f"returned 429 (rate limited)"
            )
        resp.raise_for_status()
        payload = resp.json()
        message = payload["choices"][0]["message"]
        usage = payload.get("usage") or {}
        return {
            "message": message,
            "prompt_eval_count": usage.get("prompt_tokens", 0),
            "eval_count": usage.get("completion_tokens", 0),
        }

    def loaded_models(self, endpoint: str) -> set[str]:
        return set()

    def reachable(self, endpoint: str) -> tuple[bool, str]:
        try:
            resp = httpx.get(f"{endpoint}/v1/models", timeout=10)
            resp.raise_for_status()
            return True, ""
        except httpx.HTTPError as e:
            return False, f"MLX endpoint {endpoint} unreachable: {e}"


class LiteLLMProvider:
    """LiteLLM's Python SDK (an OPTIONAL extra: pip install litellm) - a
    router that talks straight to whichever upstream vendor the model string
    names (e.g. "anthropic/claude-...", "openai/gpt-..."), returning the
    same OpenAI-shaped response LMStudioProvider/MLXProvider already
    translate:

    - Non-streaming response: a pydantic-ish ModelResponse whose
      choices[0].message carries .role/.content/.tool_calls and whose usage
      carries .prompt_tokens/.completion_tokens. The message is normalized
      to a plain dict via .model_dump() when available, falling back to
      dict()/attribute access - never subscripting, LiteLLM's response
      objects are not dicts.
    - litellm is imported LAZILY inside these method bodies, never at module
      top level, so this module keeps importing cleanly on a machine with no
      litellm installed (it stays an optional extra in the dependency
      policy; reachable() reports it as not-reachable instead of raising).
    - There is no local server to point at (the SDK talks straight to the
      upstream vendor), so default_endpoint is "" and a non-empty
      PIPELINE_LOCAL_ENDPOINT_LITELLM, when set, is forwarded to litellm as
      its api_base.
    - Per-request `temperature` is honored. There is no per-request
      context-window control like Ollama's options.num_ctx - `num_ctx` is
      instead sent as `max_tokens` (an output-length budget, not a true
      equivalent), consistent with LMStudioProvider/MLXProvider.
    - LiteLLM is a router, not a server: it has no loaded-model concept, so
      loaded_models() returns an empty set without any network call (same as
      MLXProvider).
    """

    name = "litellm"
    default_endpoint = ""

    def chat(
        self, messages: list, *, model: str, num_ctx: int, temperature: float,
        tools: list | None = None, endpoint: str | None = None,
        timeout: float | None = None, think: bool | str | None = None,
    ) -> dict:
        # Lazy import: litellm is an optional extra, so it must never appear
        # at module top level (reachable() below uses the same pattern).
        import litellm

        # think accepted for signature parity with LocalInferenceProvider -
        # litellm.completion() has no equivalent field, so it's intentionally
        # unused here.
        # None (defaulted or explicit) is coerced to the resolved role-call
        # timeout - never an unbounded request (2026-09-02 freeze).
        wire_timeout = (
            timeout if timeout is not None else resolve_role_call_timeout()
        )
        kwargs: dict = {
            "model": model, "messages": messages, "temperature": temperature,
            "max_tokens": num_ctx, "timeout": wire_timeout,
        }
        if tools:
            kwargs["tools"] = tools
        if endpoint:
            kwargs["api_base"] = endpoint
        # LiteLLM raises its own exception type for 429s; resolve it by NAME
        # off the lazily-imported module (never a module-scope import, which
        # would break the optional-dependency policy) and re-raise as
        # RateLimitedError so the orchestrator routes it to the deferral path
        # instead of burning the rework budget. Any other error propagates
        # unchanged - an auth error must surface, not defer forever.
        rate_limit_cls = getattr(
            getattr(litellm, "exceptions", None), "RateLimitError", None
        )
        try:
            resp = litellm.completion(**kwargs)
        except Exception as exc:
            if rate_limit_cls is not None and isinstance(exc, rate_limit_cls):
                raise RateLimitedError(
                    f"litellm rate limit for model {model}: {exc}"
                ) from exc
            raise
        message = resp.choices[0].message
        if hasattr(message, "model_dump"):
            message = message.model_dump()
        elif hasattr(message, "keys"):
            message = dict(message)
        else:
            message = {
                "role": getattr(message, "role", "assistant"),
                "content": getattr(message, "content", None),
            }
        usage = getattr(resp, "usage", None)
        return {
            "message": message,
            "prompt_eval_count": getattr(usage, "prompt_tokens", None) or 0,
            "eval_count": getattr(usage, "completion_tokens", None) or 0,
        }

    def loaded_models(self, endpoint: str) -> set[str]:
        # LiteLLM is a router, not a server: no loaded-model concept (same
        # as MLXProvider's one-model-per-process case). Never raises.
        return set()

    def reachable(self, endpoint: str) -> tuple[bool, str]:
        try:
            import litellm  # noqa: F401
        except ImportError:
            return False, (
                "litellm package not installed; install with "
                "pip install litellm"
            )
        return True, ""


_PROVIDERS: dict[str, type] = {
    "ollama": OllamaProvider,
    "lmstudio": LMStudioProvider,
    "mlx": MLXProvider,
    "litellm": LiteLLMProvider,
}


def get_local_provider(name: str | None = None) -> LocalInferenceProvider:
    """Resolve the active local inference provider.

    name= is a per-call override (e.g. a per-role pinned provider) that
    bypasses the PIPELINE_LOCAL_PROVIDER env lookup entirely, mirroring
    backend.get_backend(role, name=)'s override pattern. Omitted/None keeps
    today's behavior: resolve from PIPELINE_LOCAL_PROVIDER (default "ollama").
    "mlx" and "lmstudio" are both working implementations.
    """
    choice = (name or os.environ.get("PIPELINE_LOCAL_PROVIDER", "ollama")).strip().lower()
    provider_cls = _PROVIDERS.get(choice)
    if provider_cls is None:
        raise ValueError(
            f"Unknown PIPELINE_LOCAL_PROVIDER={choice!r}; expected one of "
            f"{sorted(_PROVIDERS)}."
        )
    return provider_cls()