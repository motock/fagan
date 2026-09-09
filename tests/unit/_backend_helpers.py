"""Shared fixtures/helpers for the OllamaDriver/backend test suite, split
across test_backend_*.py files (originally one 3,829-line test_backend.py)
to keep each file under the project's line-count target.
"""
import json

import pytest

from app import backend as b
from app import backend_ollama as bo


def _envelope(message_body: dict) -> dict:
    return {"message": message_body, "model": "test"}


@pytest.fixture(autouse=True)
def _clear_model_weights_cache():
    """_ollama_model_weights_mb memoizes per (endpoint, tag) so the scheduler
    doesn't re-probe /api/tags every tick. Without clearing it between tests,
    one test's mocked /api/tags payload silently answers another's lookup for
    the same tag - exactly the cross-test-leak class this repo has been bitten
    by before. Autouse so no individual test has to remember."""
    b._OLLAMA_MODEL_WEIGHTS_CACHE.clear()
    yield
    b._OLLAMA_MODEL_WEIGHTS_CACHE.clear()


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        # Default to a healthy status; _FakeStatusResponse overrides for
        # the 429 short-circuit path that _chat now checks before
        # raise_for_status().
        self.status_code = 200

    def raise_for_status(self):
        if self.status_code >= 400:
            raise b.httpx.HTTPStatusError(
                f"{self.status_code} simulated", request=None, response=self
            )

    def json(self):
        return self._payload


_LIVE_CHARS_PER_TOKEN = 2.35


def _review_trim_spy(monkeypatch):
    """Record every (max_chars) the loop asks _trim_review_transcript for,
    while still applying the real trim."""
    budgets = []
    real = b._trim_review_transcript

    def _spy(messages, max_chars):
        budgets.append(max_chars)
        return real(messages, max_chars)

    monkeypatch.setattr(bo, "_trim_review_transcript", _spy)
    return budgets


def _fake_review_chat(tool_turns=3, tool_output_chars=900):
    """Build a _chat fake whose prompt_eval_count is ALWAYS physically
    consistent with what it was actually sent (messages + tools schema, at
    _LIVE_CHARS_PER_TOKEN) - never a forced value. Whether the loop's trim
    threshold trips is therefore controlled purely by num_ctx vs how much
    content has accumulated, exactly as in production. Emits `tool_turns`
    tool-calling turns, then submits a verdict."""
    turns = {"n": 0}
    chunk = "z" * tool_output_chars

    def _fake_chat(messages, model, tools=None):
        turns["n"] += 1
        if turns["n"] > tool_turns:
            return _envelope({"tool_calls": [{"function": {
                "name": "submit_review",
                "arguments": {"verdict": "APPROVE", "summary": "ok"}}}]})
        sent = (sum(b._review_msg_chars(m) for m in messages)
                + len(json.dumps(b.OllamaDriver._REVIEW_TOOLS)))
        return {"message": {"tool_calls": [{"function": {
                    "name": "bash", "arguments": {"command": f"echo {chunk}"}}}]},
                "prompt_eval_count": int(sent / _LIVE_CHARS_PER_TOKEN)}

    return _fake_chat


def _vm_stat_output(free=0, inactive=0, purgeable=0, page_size=16384, include_inactive=True, include_purgeable=True):
    lines = [f"Mach Virtual Memory Statistics: (page size of {page_size} bytes)"]
    lines.append(f"Pages free:                                    {free}.")
    lines.append("Pages active:                                 100.")
    if include_inactive:
        lines.append(f"Pages inactive:                               {inactive}.")
    if include_purgeable:
        lines.append(f"Pages purgeable:                               {purgeable}.")
    return "\n".join(lines) + "\n"


class _FakeCompletedProcess:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _fake_vm_stat_run(stdout):
    def _run(cmd, capture_output, text, timeout):
        return _FakeCompletedProcess(stdout)
    return _run


_PROVIDER_REDIRECT_ENV_SAMPLE = {
    "ANTHROPIC_BASE_URL": "https://evil.example.com",
    "ANTHROPIC_AUTH_TOKEN": "not-a-real-token",
    "ANTHROPIC_API_KEY": "not-a-real-key",
    "ANTHROPIC_MODEL": "some-other-vendor-model",
    "ANTHROPIC_SMALL_FAST_MODEL": "some-other-vendor-model-fast",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "some-other-vendor-model",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "some-other-vendor-model",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "some-other-vendor-model-fast",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
}


def _set_provider_redirect_env(monkeypatch):
    for var, value in _PROVIDER_REDIRECT_ENV_SAMPLE.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("MY_HARMLESS_TEST_VAR", "keep-me")


class _UnimplementedFakeProvider:
    """Stands in for a not-yet-built provider (all three registered
    providers - ollama/mlx/lmstudio - are real implementations now), so the
    two contracts below can still be regression-tested without depending on
    any specific provider being unimplemented."""

    def chat(self, *a, **k):
        raise NotImplementedError("fake stub provider")

    def reachable(self, endpoint):
        raise NotImplementedError("fake stub provider")


class _FakePopenResult:
    def __init__(self, pid):
        self.pid = pid


class _FakeStatusResponse:
    """A response that surfaces a real status_code so _chat's 429 short-circuit fires."""
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise b.httpx.HTTPStatusError(
                f"{self.status_code} simulated", request=None, response=self
            )


class _FakePsResponse:
    def __init__(self, models):
        self._payload = {"models": models}
        self.status_code = 200

    def raise_for_status(self):
        if self.status_code >= 400:
            raise b.httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=self
            )

    def json(self):
        return self._payload


_REAL_LLAMA_SERVER_LINE = (
    "56470 /Applications/Ollama.app/Contents/Resources/llama-server "
    "--model /Users/jessecarroll/.ollama/models/blobs/sha256-e7b273f9636059a6 "
    "--port 59546 --host 127.0.0.1 --no-webui --offline -c 262144 -np 2 "
    "--log-verbosity 4 --no-log-prefix --no-jinja --chat-template chatml "
    "--no-mmap --flash-attn auto -b 512 -ub 512 --context-shift --keep 4"
)


def _fake_ps_run(stdout, returncode=0):
    def _run(cmd, capture_output=True, text=True, timeout=None):
        return _FakeCompletedProcess(stdout, returncode=returncode)
    return _run


_TRANSPORT_VARS = (
    ("LOCAL_AGENT_MAX_STEPS", "PIPELINE_LOCAL_MAX_STEPS"),
    ("LOCAL_AGENT_NUM_CTX", "PIPELINE_LOCAL_NUM_CTX"),
    ("LOCAL_AGENT_TEMPERATURE", "PIPELINE_LOCAL_TEMPERATURE"),
)


@pytest.fixture
def _clean_transport_env(monkeypatch):
    """Ensure none of the three LOCAL_AGENT_* transport vars leak in or out
    across a transport-warning test, and reload backend to a clean state on
    teardown."""
    for wrong, _correct in _TRANSPORT_VARS:
        monkeypatch.delenv(wrong, raising=False)
    yield
    # Teardown: wipe any vars the test set and reload backend so the module
    # is back to its normal (no-warning) state for subsequent tests.
    for wrong, _correct in _TRANSPORT_VARS:
        monkeypatch.delenv(wrong, raising=False)
    import importlib

    importlib.reload(b)


def _reload_backend():
    import importlib

    importlib.reload(b)


def _claude_usage_payload():
    """A minimal but realistic claude --output-format json envelope that
    exercises the structured (cell_dir) path of ClaudeCliDriver.complete()."""
    return {
        "result": "the answer",
        "model": "claude-sonnet-4-5-20260101",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "total_cost_usd": 0.01,
        "duration_ms": 123,
    }


def _claude_complete_monkeypatch(monkeypatch, payload):
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )


def _ollama_complete_driver(monkeypatch, envelope):
    """Build an OllamaDriver wired to a fake provider whose .chat() returns
    the given full /api/chat envelope (with prompt_eval_count/eval_count
    usage fields), mirroring the single-shot complete() path."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    class _FakeProvider:
        name = "ollama"

        def chat(self, messages, *, model, **kwargs):
            return envelope

    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "provider", _FakeProvider())
    return driver


def _ollama_usage_envelope():
    """A full /api/chat envelope carrying the usage fields
    OllamaDriver.complete() reads (prompt_eval_count/eval_count/
    total_duration) for the sidecar record."""
    return {
        "message": {"content": "RULING: ok"},
        "prompt_eval_count": 7,
        "eval_count": 3,
        "total_duration": 123456789,
    }



__all__ = [
    "_LIVE_CHARS_PER_TOKEN",
    "_PROVIDER_REDIRECT_ENV_SAMPLE",
    "_REAL_LLAMA_SERVER_LINE",
    "_TRANSPORT_VARS",
    "_FakeCompletedProcess",
    "_FakePopenResult",
    "_FakePsResponse",
    "_FakeResponse",
    "_FakeStatusResponse",
    "_UnimplementedFakeProvider",
    "_claude_complete_monkeypatch",
    "_claude_usage_payload",
    "_clean_transport_env",
    "_clear_model_weights_cache",
    "_envelope",
    "_fake_ps_run",
    "_fake_review_chat",
    "_fake_vm_stat_run",
    "_ollama_complete_driver",
    "_ollama_usage_envelope",
    "_reload_backend",
    "_review_trim_spy",
    "_set_provider_redirect_env",
    "_vm_stat_output",
]
