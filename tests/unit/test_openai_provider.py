"""The direct OpenAI provider: wire format, credentials, refusals, registration.

The provider sends the harness transcript to OpenAI's Responses API with a
per-namespace API key. Every test here stubs the HTTP boundary
(``httpx.post``) and the credential seam (``resolve_for_injection``): no test
touches the network or the process environment's real credentials.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import (
    backend,
    backend_ollama,
    credentials,
    inference_providers,
    openai_provider,
)
from app.credentials import CredentialMissing, CredentialRef
from app.inference_providers import RateLimitedError
from app.openai_provider import OpenAIProvider
from pipeline import strength_class
from pipeline.actor import bind_namespace

API_KEY = "sk-test-SECRET-8a7c"
OTHER_KEY = "sk-test-OTHER-5b2d"
MODEL = "gpt-6.1-terra"
URL = "https://api.openai.com/v1/responses"

_PAYLOAD = {
    "status": "completed",
    "output": [
        {
            "type": "message",
            "content": [{"type": "output_text", "text": "openai says hi"}],
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": "{\"q\": \"x\"}",
        },
    ],
    "usage": {"input_tokens": 3, "output_tokens": 5},
}


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("not json", self.text, 0)
        return self._payload


@pytest.fixture
def wire(monkeypatch):
    """Stub the credential seam and the HTTP boundary; record every request."""
    calls: list[dict] = []
    state = {
        "key": API_KEY,
        "missing": False,
        "status": 200,
        "payload": dict(_PAYLOAD),
        "text": "",
        "error": None,
        "refs": [],
    }

    def fake_resolve_for_injection(ref, **kwargs):
        state["refs"].append(ref)
        if state["missing"]:
            raise CredentialMissing("credential_missing")
        return state["key"]

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if state["error"] is not None:
            raise state["error"]
        if state["status"] == 200:
            return _FakeResponse(200, payload=state["payload"])
        return _FakeResponse(state["status"], payload=state.get("error_payload"), text=state["text"])

    monkeypatch.setattr(openai_provider, "resolve_for_injection", fake_resolve_for_injection)
    monkeypatch.setattr(openai_provider.httpx, "post", fake_post)
    state["calls"] = calls
    return state


def _chat(provider=None, **overrides):
    kwargs = {"model": MODEL, "num_ctx": 4096, "temperature": 0.25}
    kwargs.update(overrides)
    provider = provider or OpenAIProvider()
    return provider.chat([{"role": "user", "content": "hello"}], **kwargs)


# ---- identity and registration ------------------------------------------------


def test_openai_provider_is_named_openai_with_a_fixed_default_endpoint() -> None:
    provider = OpenAIProvider()
    assert provider.name == "openai"
    assert provider.default_endpoint == "https://api.openai.com"
    assert openai_provider.OPENAI_PROVIDER_NAME == "openai"
    assert openai_provider.OPENAI_BASE_URL == "https://api.openai.com"
    assert openai_provider.OPENAI_RESPONSES_PATH == "/v1/responses"


def test_openai_is_registered_as_a_local_provider() -> None:
    provider = inference_providers.get_local_provider("openai")
    assert isinstance(provider, OpenAIProvider)


def test_openai_is_registered_as_a_backend_driver_with_a_fixed_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:9999")
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT_OPENAI", "http://localhost:8888")

    driver = backend.get_backend("dispatch", name="openai")

    assert isinstance(driver, backend_ollama.OllamaDriver)
    assert driver.provider.name == "openai"
    assert driver.endpoint == "https://api.openai.com"


def test_openai_is_registered_as_a_hosted_provider() -> None:
    assert "openai" in backend_ollama._HOSTED_PROVIDERS


def test_openai_route_is_classified_as_a_capable_provider() -> None:
    assert strength_class.strength_class("openai", MODEL) == strength_class.CAPABLE
    assert strength_class.strength_class("openai", None) == strength_class.CAPABLE


def test_unknown_provider_is_still_refused_by_the_registry() -> None:
    with pytest.raises(ValueError, match="Unknown PIPELINE_LOCAL_PROVIDER"):
        inference_providers.get_local_provider("openai-typo")


# ---- the wire format ----------------------------------------------------------


def test_chat_posts_to_the_fixed_responses_url_with_the_bearer_key(wire) -> None:
    _chat()

    assert len(wire["calls"]) == 1
    call = wire["calls"][0]
    assert call["url"] == URL
    assert call["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["follow_redirects"] is False


def test_chat_body_carries_no_sampling_or_token_limit_keys(wire) -> None:
    _chat(temperature=0.9, num_ctx=131072)

    body = wire["calls"][0]["json"]
    assert body["model"] == MODEL
    assert body["store"] is False
    for forbidden in ("temperature", "max_tokens", "max_completion_tokens"):
        assert forbidden not in body
    assert "num_ctx" not in json.dumps(body)


def test_chat_maps_the_envelope_content_and_tool_calls(wire) -> None:
    result = _chat()

    message = result["message"]
    assert message["role"] == "assistant"
    assert message["content"] == "openai says hi"
    assert message["tool_calls"][0]["id"] == "call_1"
    assert message["tool_calls"][0]["function"]["name"] == "lookup"
    assert result["prompt_eval_count"] == 3
    assert result["eval_count"] == 5


def test_chat_sends_tool_definitions_when_given(wire) -> None:
    tools = [{
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "find",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    _chat(tools=tools)

    assert wire["calls"][0]["json"]["tools"][0]["name"] == "lookup"


def test_chat_ignores_the_endpoint_argument(wire) -> None:
    _chat(endpoint="https://evil.example")

    assert wire["calls"][0]["url"] == URL


def test_chat_ignores_a_local_endpoint_environment(wire, monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:9999")

    _chat(endpoint="http://localhost:9999")

    assert wire["calls"][0]["url"] == URL


# ---- thinking -----------------------------------------------------------------


def test_think_true_sends_reasoning_effort_medium(wire) -> None:
    _chat(think=True)

    assert wire["calls"][0]["json"]["reasoning"] == {"effort": "medium"}


def test_think_string_is_sent_as_the_reasoning_effort(wire) -> None:
    _chat(think="high")

    assert wire["calls"][0]["json"]["reasoning"] == {"effort": "high"}


@pytest.mark.parametrize("think", [None, False])
def test_think_off_sends_no_reasoning_key(wire, think) -> None:
    _chat(think=think)

    assert "reasoning" not in wire["calls"][0]["json"]


def test_think_with_an_unknown_effort_is_refused_before_any_request(wire) -> None:
    with pytest.raises(ValueError, match="unknown reasoning_effort"):
        _chat(think="turbo")

    assert wire["calls"] == []


# ---- credentials --------------------------------------------------------------


def test_the_key_is_resolved_per_call_for_the_bound_namespace(wire) -> None:
    with bind_namespace("acme/web"):
        _chat()

    ref = wire["refs"][0]
    assert ref == CredentialRef("model", "openai_api_key", namespace="acme/web")


def test_two_calls_with_two_keys_send_two_different_headers(wire) -> None:
    provider = OpenAIProvider()

    _chat(provider)
    wire["key"] = OTHER_KEY
    _chat(provider)

    first, second = wire["calls"]
    assert first["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert second["headers"]["Authorization"] == f"Bearer {OTHER_KEY}"


def test_a_blank_key_is_refused_before_any_request(wire) -> None:
    wire["key"] = "   "

    with pytest.raises(ValueError, match="refusing to call OpenAI unauthenticated"):
        _chat()

    assert wire["calls"] == []


def test_a_none_key_is_refused_before_any_request(wire) -> None:
    wire["key"] = None

    with pytest.raises(ValueError, match="empty value"):
        _chat()

    assert wire["calls"] == []


def test_a_missing_credential_propagates_and_sends_nothing(wire) -> None:
    wire["missing"] = True

    with pytest.raises(CredentialMissing):
        _chat()

    assert wire["calls"] == []


# ---- refusals and errors ------------------------------------------------------


def test_a_429_raises_a_rate_limited_error_naming_the_model(wire) -> None:
    wire["status"] = 429

    with pytest.raises(RateLimitedError, match="429") as info:
        _chat()

    assert MODEL in str(info.value)


def test_a_401_error_names_the_status_and_never_echoes_the_key(wire) -> None:
    wire["status"] = 401
    wire["error_payload"] = {"error": {"code": "invalid_api_key", "message": "sk-test-SECRET"}}
    wire["text"] = "Incorrect API key provided: sk-test-SECRET"

    with pytest.raises(RuntimeError) as info:
        _chat()

    message = str(info.value)
    assert "401" in message
    assert "invalid_api_key" in message
    assert "sk-test-SECRET" not in message


def test_a_500_with_a_non_json_body_reports_error_unknown(wire) -> None:
    wire["status"] = 500
    wire["text"] = "<html>upstream exploded sk-test-SECRET</html>"

    with pytest.raises(RuntimeError) as info:
        _chat()

    message = str(info.value)
    assert "500" in message
    assert "error=unknown" in message
    assert "sk-test-SECRET" not in message
    assert "upstream exploded" not in message


def test_a_transport_failure_is_reported_without_the_key(wire) -> None:
    wire["error"] = httpx.ConnectError("connection refused for sk-test-SECRET")

    with pytest.raises(RuntimeError, match="OpenAI endpoint unreachable: ConnectError") as info:
        _chat()

    assert API_KEY not in str(info.value)
    assert "sk-test-SECRET" not in str(info.value)
    assert isinstance(info.value.__cause__, httpx.ConnectError)


# ---- reachability -------------------------------------------------------------


def test_reachable_is_true_with_a_key_and_sends_nothing(wire) -> None:
    assert OpenAIProvider().reachable("https://api.openai.com") == (True, "")
    assert wire["calls"] == []


def test_reachable_reports_a_missing_credential_without_raising(wire) -> None:
    wire["missing"] = True

    ok, reason = OpenAIProvider().reachable("")

    assert ok is False
    assert reason.startswith("OpenAI API key not usable: ")


def test_reachable_reports_a_blank_key_without_raising(wire) -> None:
    wire["key"] = ""

    ok, reason = OpenAIProvider().reachable("")

    assert ok is False
    assert "OpenAI API key not usable" in reason


def test_loaded_models_probes_nothing(wire) -> None:
    assert OpenAIProvider().loaded_models("https://api.openai.com") == set()
    assert wire["calls"] == []


# ---- the credential seam itself ----------------------------------------------


def test_openai_key_resolves_from_openai_api_key_in_the_default_namespace(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)

    value = credentials.resolve_for_injection(CredentialRef("model", "openai_api_key"))

    assert value == API_KEY


def test_openai_key_is_not_read_from_the_environment_for_another_namespace(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)

    with pytest.raises(CredentialMissing):
        credentials.resolve_for_injection(
            CredentialRef("model", "openai_api_key", namespace="acme/web")
        )
