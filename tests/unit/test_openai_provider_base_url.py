"""PIPELINE_OPENAI_BASE_URL: the operator-only host override for the OpenAI provider.

The host is fixed unless the operator sets the variable for the whole
deployment. These tests stub the HTTP boundary (``httpx.post``) and the
credential seam (``resolve_for_injection``): nothing touches the network or
the real credentials.
"""

from __future__ import annotations

import logging

import pytest

from app import backend_ollama, openai_provider
from app.openai_provider import OpenAIProvider

OVERRIDE_VAR = "PIPELINE_OPENAI_BASE_URL"
FIXED_BASE = "https://api.openai.com"
API_KEY = "sk-test-SECRET-9c1e"
MODEL = "gpt-6.1-terra"
OVERRIDE = "https://ollama.internal:8443"
OVERRIDE_EVENT = "openai_base_url_overridden"

_PAYLOAD = {
    "status": "completed",
    "output": [
        {
            "type": "message",
            "content": [{"type": "output_text", "text": "ok"}],
        },
    ],
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


class _FakeResponse:
    status_code = 200

    def json(self):
        return _PAYLOAD


@pytest.fixture
def wire(monkeypatch):
    """Clean environment, stubbed key and transport; returns the recorded calls."""
    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _FakeResponse()

    monkeypatch.setattr(
        openai_provider, "resolve_for_injection", lambda ref, **kwargs: API_KEY
    )
    monkeypatch.setattr(openai_provider.httpx, "post", fake_post)
    monkeypatch.setattr(openai_provider, "_override_warned", False, raising=False)
    monkeypatch.delenv(OVERRIDE_VAR, raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT_OPENAI", raising=False)
    return calls


def _chat(provider=None, **overrides):
    kwargs = {"model": MODEL, "num_ctx": 4096, "temperature": 0.25}
    kwargs.update(overrides)
    provider = provider or OpenAIProvider()
    return provider.chat([{"role": "user", "content": "hello"}], **kwargs)


def _responses_url(base: str) -> str:
    return base + "/v1/responses"


# ---- positive: the host the request goes to ----------------------------------


def test_should_send_to_the_fixed_host_when_the_override_is_unset(wire) -> None:
    _chat()

    assert wire[0]["url"] == _responses_url(FIXED_BASE)


def test_should_send_to_the_fixed_host_when_the_override_is_empty(wire, monkeypatch) -> None:
    monkeypatch.setenv(OVERRIDE_VAR, "")

    _chat()

    assert wire[0]["url"] == _responses_url(FIXED_BASE)


def test_should_send_to_a_valid_https_override(wire, monkeypatch) -> None:
    monkeypatch.setenv(OVERRIDE_VAR, OVERRIDE)

    _chat()

    assert wire[0]["url"] == _responses_url(OVERRIDE)


def test_should_report_a_valid_override_as_the_default_endpoint(monkeypatch) -> None:
    monkeypatch.setenv(OVERRIDE_VAR, OVERRIDE)

    assert OpenAIProvider().default_endpoint == OVERRIDE


def test_should_report_the_fixed_host_as_the_default_endpoint_when_unset(monkeypatch) -> None:
    monkeypatch.delenv(OVERRIDE_VAR, raising=False)

    assert OpenAIProvider().default_endpoint == FIXED_BASE


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
    ],
)
def test_should_accept_http_only_for_a_loopback_host(wire, monkeypatch, value) -> None:
    monkeypatch.setenv(OVERRIDE_VAR, value)

    _chat()

    assert wire[0]["url"] == _responses_url(value)


# ---- negative: a bad override fails before any request -----------------------

_INVALID_VALUES = [
    pytest.param("http://ollama.internal:11434", id="http-non-loopback"),
    pytest.param("http://localhost.evil.example:11434", id="http-loopback-lookalike"),
    pytest.param("https://ollama.internal/v1", id="path"),
    pytest.param("https://ollama.internal/", id="trailing-slash"),
    pytest.param("https://ollama.internal?key=1", id="query"),
    pytest.param("https://ollama.internal#frag", id="fragment"),
    pytest.param("https://user:pw-SECRETPW@ollama.internal", id="userinfo"),
    pytest.param("https://ollama.internal@evil.example", id="userinfo-at-sign"),
    pytest.param(" https://ollama.internal", id="leading-whitespace"),
    pytest.param("https://ollama.internal ", id="trailing-whitespace"),
    pytest.param("https://ollama internal", id="inner-whitespace"),
    pytest.param(" ", id="whitespace-only"),
    pytest.param("ftp://ollama.internal", id="ftp-scheme"),
    pytest.param("ollama.internal", id="bare-hostname"),
    pytest.param("ollama.internal:11434", id="host-and-port-no-scheme"),
    pytest.param("https://", id="no-host"),
    pytest.param("https://ollama.internal:notaport", id="non-numeric-port"),
    pytest.param("https://ollama.internal:99999", id="port-out-of-range"),
]


@pytest.mark.parametrize("value", _INVALID_VALUES)
def test_should_refuse_an_invalid_override_before_sending_anything(
    wire, monkeypatch, value
) -> None:
    monkeypatch.setenv(OVERRIDE_VAR, value)

    with pytest.raises(ValueError, match=OVERRIDE_VAR):
        _chat()

    assert wire == []


@pytest.mark.parametrize("value", _INVALID_VALUES)
def test_should_not_echo_an_invalid_override_in_the_error(
    wire, monkeypatch, value
) -> None:
    monkeypatch.setenv(OVERRIDE_VAR, value)

    with pytest.raises(ValueError) as excinfo:
        _chat()

    # The non-blank part is what could leak; a whitespace-only value has
    # none, and its spaces would match the message's own text.
    secret = value.strip()
    if secret:
        assert secret not in str(excinfo.value)
    assert "SECRETPW" not in str(excinfo.value)


def test_should_not_refuse_an_empty_override_as_invalid(wire, monkeypatch) -> None:
    monkeypatch.setenv(OVERRIDE_VAR, "")

    _chat()

    assert len(wire) == 1


# ---- the endpoint argument and other endpoint variables cannot move the host -


@pytest.mark.parametrize("override", [None, OVERRIDE])
def test_should_ignore_the_endpoint_argument(wire, monkeypatch, override) -> None:
    if override is not None:
        monkeypatch.setenv(OVERRIDE_VAR, override)
    expected = override or FIXED_BASE

    _chat(endpoint="http://evil.example:9999")

    assert wire[0]["url"] == _responses_url(expected)


@pytest.mark.parametrize("override", [None, OVERRIDE])
def test_should_ignore_pipeline_local_endpoint(wire, monkeypatch, override) -> None:
    if override is not None:
        monkeypatch.setenv(OVERRIDE_VAR, override)
    expected = override or FIXED_BASE
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://evil.example:9999")

    _chat()

    assert wire[0]["url"] == _responses_url(expected)


@pytest.mark.parametrize("override", [None, OVERRIDE])
def test_should_ignore_pipeline_local_endpoint_openai(wire, monkeypatch, override) -> None:
    if override is not None:
        monkeypatch.setenv(OVERRIDE_VAR, override)
    expected = override or FIXED_BASE
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT_OPENAI", "http://evil.example:9999")

    _chat()

    assert wire[0]["url"] == _responses_url(expected)


@pytest.mark.parametrize("override", [None, OVERRIDE])
def test_should_ignore_a_driver_level_endpoint(wire, monkeypatch, override) -> None:
    if override is not None:
        monkeypatch.setenv(OVERRIDE_VAR, override)
    expected = override or FIXED_BASE
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://evil.example:9999")
    driver = backend_ollama.OllamaDriver(provider_name="openai")
    driver.endpoint = "http://evil.example:9999"

    _chat(provider=driver.provider, endpoint=driver.endpoint)

    assert wire[0]["url"] == _responses_url(expected)


# ---- the operator warning ----------------------------------------------------


def test_should_log_the_override_warning_once_across_two_calls(
    wire, monkeypatch, caplog
) -> None:
    monkeypatch.setenv(OVERRIDE_VAR, OVERRIDE)
    caplog.set_level(logging.WARNING)

    _chat()
    _chat()

    events = [r for r in caplog.records if getattr(r, "event", None) == OVERRIDE_EVENT]
    assert len(events) == 1
    assert events[0].host == "ollama.internal"


def test_should_log_only_the_host_in_the_override_warning(
    wire, monkeypatch, caplog
) -> None:
    monkeypatch.setenv(OVERRIDE_VAR, OVERRIDE)
    caplog.set_level(logging.WARNING)

    _chat()

    event = next(r for r in caplog.records if getattr(r, "event", None) == OVERRIDE_EVENT)
    assert "8443" not in event.getMessage()
    assert "https://" not in event.getMessage()


def test_should_not_log_the_override_warning_when_unset(wire, caplog) -> None:
    caplog.set_level(logging.WARNING)

    _chat()

    assert not [r for r in caplog.records if getattr(r, "event", None) == OVERRIDE_EVENT]


def test_should_not_log_an_invalid_override_value(wire, monkeypatch, caplog) -> None:
    monkeypatch.setenv(OVERRIDE_VAR, "https://user:pw-SECRETPW@ollama.internal")
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ValueError):
        _chat()

    assert "SECRETPW" not in caplog.text
