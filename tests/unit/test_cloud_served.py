"""Unit tests for app.cloud_served.is_cloud_served and its endpoint rules.

See hpc-03: cloud-served classification must cover the registry class and the
endpoint, not only the ``:cloud`` tag suffix.
"""

from __future__ import annotations

import pytest

from app.cloud_served import endpoint_from_env, endpoint_is_remote, is_cloud_served

# (provider, tag, endpoint, entry_class) -> expected
_TABLE = [
    # --- the historical rule survives unchanged -------------------------
    ("ollama", "glm-5.2:cloud", "", None, True),
    ("ollama", "glm-5.2:cloud", "http://localhost:11434", None, True),
    ("ollama", "minimax-m3:cloud", "https://ollama.com", None, True),
    ("ollama", "glm-5.2:CLOUD", "http://127.0.0.1:11434", None, True),
    # --- plain name on a hosted endpoint --------------------------------
    ("ollama", "glm-5.2", "https://ollama.com", None, True),
    ("ollama", "deepseek-v4-flash", "https://ollama.com/", None, True),
    ("ollama", "qwen3:8b", "ollama.com:443", None, True),
    ("ollama", "qwen3:8b", "https://gpu-box.example.com:11434", None, True),
    # --- plain name, local endpoint -------------------------------------
    ("ollama", "qwen3:8b", "", None, False),
    ("ollama", "qwen3:8b", "http://localhost:11434", None, False),
    ("ollama", "qwen3:8b", "http://LOCALHOST:11434", None, False),
    ("ollama", "qwen3:8b", "http://host.docker.internal:11434", None, False),
    ("ollama", "qwen3:8b", "http://127.0.0.1:11434", None, False),
    ("ollama", "qwen3:8b", "http://[::1]:11434", None, False),
    ("ollama", "qwen3:8b", "http://192.168.1.50:11434", None, False),
    ("ollama", "qwen3:8b", "http://10.0.0.7:11434", None, False),
    # --- registry class -------------------------------------------------
    ("ollama", "qwen3:8b", "", "capable", True),
    ("ollama", "qwen3:8b", "", "on-device", False),
    ("ollama", "qwen3:8b", "https://ollama.com", "on-device", False),
    ("ollama", "qwen3:8b", "http://localhost:11434", "capable", True),
    # an unrecognised class is ignored, the endpoint rule decides
    ("ollama", "qwen3:8b", "https://ollama.com", "sometimes", True),
    ("ollama", "qwen3:8b", "", "sometimes", False),
    # --- non-ollama providers are never classified by endpoint/class -----
    ("claude", "claude-3-5-sonnet", "https://ollama.com", None, False),
    ("litellm", "gpt-5-mini", "https://ollama.com", "capable", False),
    # ...but the ":cloud" suffix is the one rule that predates this module
    ("mlx", "glm-5.2:cloud", "http://localhost:11434", None, True),
    # --- empty/odd input degrades to False -------------------------------
    ("ollama", "", "", None, False),
    ("ollama", "qwen3:8b", "   ", None, False),
]


@pytest.mark.parametrize(
    ("provider", "tag", "endpoint", "entry_class", "expected"), _TABLE
)
def test_is_cloud_served_table(provider, tag, endpoint, entry_class, expected):
    assert (
        is_cloud_served(provider, tag, endpoint=endpoint, entry_class=entry_class)
        is expected
    )


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("", False),
        ("http://localhost:11434", False),
        ("host.docker.internal:11434", False),
        ("http://127.0.0.1:11434", False),
        ("http://172.17.0.2:11434", False),
        ("https://ollama.com", True),
        ("ollama.com", True),
        ("http://[2001:4860:4860::8888]:11434", True),
    ],
)
def test_endpoint_is_remote(endpoint, expected):
    assert endpoint_is_remote(endpoint) is expected


def test_endpoint_from_env_prefers_provider_scoped(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", "https://ollama.com")
    assert endpoint_from_env() == "https://ollama.com"


def test_endpoint_from_env_falls_back_to_shared(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "https://ollama.com")
    assert endpoint_from_env() == "https://ollama.com"


def test_endpoint_from_env_empty_when_unset(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT", raising=False)
    assert endpoint_from_env() == ""


def test_does_not_resolve_dns(monkeypatch):
    """A localhost-named host must not trigger a lookup, even if a resolver
    would answer differently - this is a classification, not a security
    control."""
    import socket

    def boom(*_args, **_kwargs):
        raise AssertionError("is_cloud_served must not resolve DNS")

    monkeypatch.setattr(socket, "gethostbyname", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert is_cloud_served("ollama", "qwen3:8b", endpoint="https://ollama.com")
