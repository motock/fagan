"""Survivor: LM Studio's wire format must not change.

The LM Studio provider's request body and response mapping are about to be
extracted into a module-level helper shared with the gateway provider. This
file pins the *behaviour* of that mapping, so the extraction cannot quietly
change what goes on the wire or what comes back to the driver.
"""

from __future__ import annotations

import httpx
import pytest

from app import inference_providers


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        return self

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def posted(monkeypatch):
    """Capture the requests LM Studio would send."""
    calls: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse(
            200,
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "lmstudio says hi"}}
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def test_lmstudio_maps_requests_identically(posted) -> None:
    provider = inference_providers.LMStudioProvider()
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    result = provider.chat(
        [{"role": "user", "content": "hello"}],
        model="gemma-4-e4b",
        num_ctx=2048,
        temperature=0.25,
        tools=tools,
    )

    url, kwargs = posted[0]
    assert url == "http://localhost:1234/v1/chat/completions"
    assert kwargs["json"] == {
        "model": "gemma-4-e4b",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "temperature": 0.25,
        "max_tokens": 2048,
        "tools": tools,
    }
    assert result["message"] == {"role": "assistant", "content": "lmstudio says hi"}
    assert result["prompt_eval_count"] == 11
    assert result["eval_count"] == 7


def test_lmstudio_omits_tools_when_there_are_none(posted) -> None:
    provider = inference_providers.LMStudioProvider()
    provider.chat(
        [{"role": "user", "content": "hello"}],
        model="gemma-4-e4b",
        num_ctx=2048,
        temperature=0.25,
    )

    _url, kwargs = posted[0]
    assert "tools" not in kwargs["json"]


def test_lmstudio_reports_a_rate_limited_server(posted, monkeypatch) -> None:
    def fake_post(url, **kwargs):
        return _FakeResponse(429, {})

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = inference_providers.LMStudioProvider()

    with pytest.raises(inference_providers.RateLimitedError):
        provider.chat(
            [{"role": "user", "content": "hello"}],
            model="gemma-4-e4b",
            num_ctx=2048,
            temperature=0.25,
        )


def test_lmstudio_defaults_a_missing_usage_block_to_zero(posted, monkeypatch) -> None:
    def fake_post(url, **kwargs):
        return _FakeResponse(
            200, {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = inference_providers.LMStudioProvider()

    result = provider.chat(
        [{"role": "user", "content": "hello"}],
        model="gemma-4-e4b",
        num_ctx=2048,
        temperature=0.25,
    )

    assert result["prompt_eval_count"] == 0
    assert result["eval_count"] == 0
