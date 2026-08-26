"""Tests for the backend driver registry and OllamaDriver: role routing, top-level explicit provider names, and OllamaDriver.complete()/_chat() retry.

Split out of test_backend.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._backend_helpers.
"""
import pytest

from app import backend as b
from tests.unit._backend_helpers import (  # noqa: F401
    _clear_model_weights_cache,
    _envelope,
    _FakeResponse,
    _FakeStatusResponse,
)


def test_get_backend_no_role_returns_claude_driver():
    assert isinstance(b.get_backend(), b.ClaudeCliDriver)


@pytest.mark.parametrize("role", ["dispatch", "review", "overlord"])
def test_get_backend_defaults_each_role_to_claude(role, monkeypatch):
    monkeypatch.delenv(f"PIPELINE_BACKEND_{role.upper()}", raising=False)
    assert isinstance(b.get_backend(role), b.ClaudeCliDriver)


@pytest.mark.parametrize("role", ["dispatch", "review", "overlord"])
def test_get_backend_honors_explicit_claude_selection(role, monkeypatch):
    monkeypatch.setenv(f"PIPELINE_BACKEND_{role.upper()}", "claude")
    assert isinstance(b.get_backend(role), b.ClaudeCliDriver)


def test_get_backend_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "CLAUDE")
    assert isinstance(b.get_backend("dispatch"), b.ClaudeCliDriver)


def test_get_backend_roles_are_independently_routable(monkeypatch):
    """Selecting a different driver for one role must not affect the others -
    this is the whole point of per-role routing."""
    monkeypatch.setenv("PIPELINE_BACKEND_OVERLORD", "local")
    assert isinstance(b.get_backend("review"), b.ClaudeCliDriver)
    assert isinstance(b.get_backend("dispatch"), b.ClaudeCliDriver)
    assert isinstance(b.get_backend("overlord"), b.OllamaDriver)


def test_get_backend_unknown_driver_name_raises_not_implemented(monkeypatch):
    monkeypatch.setenv("PIPELINE_BACKEND_REVIEW", "some-typo'd-name")
    with pytest.raises(NotImplementedError, match="PIPELINE_BACKEND_REVIEW"):
        b.get_backend("review")


def test_get_backend_name_override_skips_env(monkeypatch):
    """Explicit name= overrides the env var entirely."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    assert isinstance(b.get_backend("dispatch", name="local"), b.OllamaDriver)


def test_get_backend_auto_raises_with_helpful_message():
    """'auto' must never reach get_backend — callers must resolve it first."""
    with pytest.raises(ValueError, match="auto"):
        b.get_backend("dispatch", name="auto")


# ---------- T16: explicit provider names as top-level backend names ----------
@pytest.mark.parametrize("name,provider_cls", [
    ("ollama", "OllamaProvider"),
    ("lmstudio", "LMStudioProvider"),
    ("mlx", "MLXProvider"),
])
def test_get_backend_explicit_provider_name_pins_that_provider(name, provider_cls, monkeypatch):
    # Regardless of PIPELINE_LOCAL_PROVIDER, naming the provider directly as
    # the backend must resolve to exactly that provider - this is what lets
    # PIPELINE_BACKEND_DISPATCH=lmstudio work without also setting
    # PIPELINE_LOCAL_PROVIDER=lmstudio.
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "ollama")
    driver = b.get_backend("dispatch", name=name)
    assert isinstance(driver, b.OllamaDriver)
    assert isinstance(driver.provider, getattr(b.inference_providers, provider_cls))


def test_get_backend_local_alias_still_resolves_via_env(monkeypatch):
    # "local" stays a permanent back-compat alias - it must keep resolving via
    # PIPELINE_LOCAL_PROVIDER exactly as before these explicit names existed
    # (manifests persist "backend": "local" for already-dispatched stories).
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "mlx")
    driver = b.get_backend("dispatch", name="local")
    assert isinstance(driver, b.OllamaDriver)
    assert isinstance(driver.provider, b.inference_providers.MLXProvider)


# ---------- OllamaDriver.complete() ----------


def test_complete_posts_to_native_chat_endpoint_and_returns_content(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    captured = {}

    def _fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"message": {"content": "RULING: x"}})

    monkeypatch.setattr(b.httpx, "post", _fake_post)

    driver = b.OllamaDriver()
    result = driver.complete("do the thing", system="be careful", model="opus")

    assert result == "RULING: x"
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["json"]["messages"] == [
        {"role": "system", "content": "be careful"},
        {"role": "user", "content": "do the thing"},
    ]


def test_complete_pins_num_ctx_to_avoid_cpu_gpu_split(monkeypatch):
    """Ollama's default context (131072) made devstral:24b's KV cache blow
    past 24GB unified memory, forcing a CPU/GPU split that timed out a single
    completion at 600s. num_ctx must always be set explicitly."""
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "8192")
    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    b.OllamaDriver().complete("p", model="opus")

    assert captured["options"]["num_ctx"] == 8192


def test_complete_resolves_tier_to_configured_local_model(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_OPUS", "qwen2.5-coder:32b")
    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    b.OllamaDriver().complete("p", model="opus")

    assert captured["model"] == "qwen2.5-coder:32b"


def test_resolve_local_model_passes_through_a_concrete_tag_unchanged(monkeypatch):
    """A value containing ':' (Ollama's tag separator, e.g. 'devstral:24b')
    is already a concrete model tag, not a tier name ('sonnet'/'opus'/
    'haiku') - it must be returned as-is rather than looked up in
    _LOCAL_TIER_ENV (where it would never match and silently fall back to
    PIPELINE_LOCAL_MODEL_DEFAULT, discarding the caller's explicit choice).
    This is what lets _run_reviewer route review to a different concrete
    model than dispatch's tier resolution would give it."""
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "gpt-oss:20b")
    assert b._resolve_local_model("devstral:24b") == "devstral:24b"


def test_resolve_local_model_provider_scoped_env_var_wins_over_generic(monkeypatch):
    """Two roles on different local providers must not silently share one
    tier->model mapping: PIPELINE_LOCAL_MODEL_MLX_SONNET (provider-scoped)
    must be checked before the provider-agnostic PIPELINE_LOCAL_MODEL_SONNET,
    so an mlx-routed role and an ollama-routed role can independently resolve
    the same tier name to two different concrete models."""
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_SONNET", "devstral:24b")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_MLX_SONNET", "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit")
    assert b._resolve_local_model("sonnet", provider="mlx") == (
        "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"
    )
    assert b._resolve_local_model("sonnet", provider="ollama") == "devstral:24b"


def test_resolve_local_model_falls_back_to_generic_tier_env_when_provider_scoped_unset(
    monkeypatch,
):
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_MLX_SONNET", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_SONNET", "devstral:24b")
    assert b._resolve_local_model("sonnet", provider="mlx") == "devstral:24b"


def test_resolve_local_model_default_provider_arg_is_ollama():
    """Existing callers that don't pass provider= (pre-dating this change)
    must resolve identically to before - the parameter defaults to 'ollama',
    the historical implicit assumption."""
    import inspect
    assert inspect.signature(b._resolve_local_model).parameters["provider"].default == "ollama"


def test_complete_threads_provider_name_into_local_model_resolution(monkeypatch):
    """OllamaDriver(provider_name='mlx').complete() must resolve tiers via
    the mlx-scoped env var, not the ollama-scoped/generic one - regression
    test for the tier-collision bug (both providers sharing one global
    PIPELINE_LOCAL_MODEL_OPUS)."""
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_OPUS", "devstral:24b")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_MLX_OPUS", "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit")
    captured = {}

    class _FakeProvider:
        name = "mlx"

        def chat(self, messages, *, model, **kwargs):
            captured["model"] = model
            return {"message": {"content": "ok"}}

    driver = b.OllamaDriver(provider_name="mlx")
    monkeypatch.setattr(driver, "provider", _FakeProvider())

    driver.complete("p", model="opus")

    assert captured["model"] == "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"


def test_complete_falls_back_to_devstral_default_for_unmapped_tier(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_SONNET", raising=False)
    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    b.OllamaDriver().complete("p", model="sonnet")

    assert captured["model"] == "devstral:24b"


def test_complete_raises_clear_runtime_error_when_endpoint_unreachable(monkeypatch):
    def _boom(url, json, timeout):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "post", _boom)

    with pytest.raises(RuntimeError, match="unreachable"):
        b.OllamaDriver().complete("p", model="opus")


# ---------- OllamaDriver._chat() retry-with-backoff for transient httpx failures ----------
# These cover the retry loop added to _chat(): transient connect errors and
# 5xx responses are retried (with linear backoff), 4xx raises immediately, and
# a 429 (RateLimitedError) is never caught by the loop and propagates on the
# first attempt so pipeline/server.py's `except backend.RateLimitedError`
# deferral contract is preserved. b.time.sleep is mocked to a no-op so the
# tests run fast (the established pattern in tests/unit/test_local_agent.py).
def test_chat_retries_transient_connect_error_then_succeeds(monkeypatch):
    """A transient httpx.TransportError (ConnectError) on the first _chat
    attempt must be retried and the second attempt's content returned."""
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _fake_post(url, json, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise b.httpx.ConnectError("connection refused")
        return _FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr(b.httpx, "post", _fake_post)

    result = b.OllamaDriver().complete("p", model="gpt-oss:20b")
    assert result == "ok"
    assert calls["n"] == 2


def test_chat_retries_5xx_then_succeeds(monkeypatch):
    """A 5xx HTTPStatusError on the first _chat attempt must be retried and
    the second attempt's content returned."""
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _fake_post(url, json, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeStatusResponse(503)
        return _FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr(b.httpx, "post", _fake_post)

    result = b.OllamaDriver().complete("p", model="gpt-oss:20b")
    assert result == "ok"
    assert calls["n"] == 2


def test_chat_does_not_retry_4xx(monkeypatch):
    """A 4xx HTTPStatusError must NOT be retried -- retrying a bad request is
    pointless. complete() must raise the existing RuntimeError wrap and post
    must be called exactly once."""
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _fake_post(url, json, timeout):
        calls["n"] += 1
        return _FakeStatusResponse(400)

    monkeypatch.setattr(b.httpx, "post", _fake_post)

    with pytest.raises(RuntimeError, match="unreachable"):
        b.OllamaDriver().complete("p", model="gpt-oss:20b")
    assert calls["n"] == 1


def test_chat_raises_after_exhausting_all_attempts(monkeypatch):
    """Exhausting all retry attempts must still raise the SAME
    RuntimeError('unreachable') contract callers depend on, and post must be
    called exactly chat_max_attempts times (3 here)."""
    monkeypatch.setenv("PIPELINE_LOCAL_CHAT_MAX_ATTEMPTS", "3")
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _boom(url, json, timeout):
        calls["n"] += 1
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "post", _boom)

    with pytest.raises(RuntimeError, match="unreachable"):
        b.OllamaDriver().complete("p", model="gpt-oss:20b")
    assert calls["n"] == 3


def test_chat_rate_limited_still_propagates_on_first_attempt(monkeypatch):
    """A 429 (RateLimitedError) must NEVER be caught by the new retry loop --
    not even once -- so pipeline/server.py's `except backend.RateLimitedError`
    deferral branch fires instead of burning a rework cycle. complete() must
    raise RateLimitedError (not a generic RuntimeError) and post must be
    called exactly once."""
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def _fake_post(url, json, timeout):
        calls["n"] += 1
        return _FakeStatusResponse(429)

    monkeypatch.setattr(b.httpx, "post", _fake_post)

    with pytest.raises(b.RateLimitedError):
        b.OllamaDriver().complete("p", model="gpt-oss:20b")
    assert calls["n"] == 1


def test_chat_retry_attrs_read_in_process_env_vars(monkeypatch):
    """OllamaDriver must expose chat_max_attempts / chat_retry_backoff as
    instance attributes read in-process from PIPELINE_LOCAL_CHAT_MAX_ATTEMPTS
    / PIPELINE_LOCAL_CHAT_RETRY_BACKOFF (NOT the LOCAL_AGENT_* subprocess
    vars -- conflating the two namespaces is the alias-trap bug class)."""
    monkeypatch.setenv("PIPELINE_LOCAL_CHAT_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("PIPELINE_LOCAL_CHAT_RETRY_BACKOFF", "1.5")
    driver = b.OllamaDriver()
    assert driver.chat_max_attempts == 5
    assert driver.chat_retry_backoff == 1.5


def test_chat_retry_attrs_default_when_env_unset(monkeypatch):
    """Defaults: chat_max_attempts=3, chat_retry_backoff=2.0 when the env
    vars are unset."""
    monkeypatch.delenv("PIPELINE_LOCAL_CHAT_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_CHAT_RETRY_BACKOFF", raising=False)
    driver = b.OllamaDriver()
    assert driver.chat_max_attempts == 3
    assert driver.chat_retry_backoff == 2.0


def test_backend_imports_time_module():
    """backend.py must `import time` (stdlib) for the retry backoff sleep."""
    assert hasattr(b, "time"), "backend.py must import the time module"


def test_backend_uses_distinct_chat_retry_env_var_names(monkeypatch):
    """The new retry knobs must read PIPELINE_LOCAL_CHAT_* (in-process), NOT
    the LOCAL_AGENT_CHAT_* subprocess vars -- setting LOCAL_AGENT_CHAT_* must
    NOT influence OllamaDriver's in-process retry config."""
    monkeypatch.delenv("PIPELINE_LOCAL_CHAT_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_CHAT_RETRY_BACKOFF", raising=False)
    monkeypatch.setenv("LOCAL_AGENT_CHAT_MAX_ATTEMPTS", "9")
    monkeypatch.setenv("LOCAL_AGENT_CHAT_RETRY_BACKOFF", "9")
    driver = b.OllamaDriver()
    assert driver.chat_max_attempts == 3  # default, NOT 9
    assert driver.chat_retry_backoff == 2.0  # default, NOT 9


def test_usage_probe_text_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        b.OllamaDriver().usage_probe_text()


