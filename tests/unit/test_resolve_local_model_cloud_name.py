"""otp-02: a plain-named Ollama cloud model must survive _resolve_local_model.

_resolve_local_model() treated every bare word that was not a declared tag and
not a tier name as a tier name, so an undeclared cloud model such as
"glm-5.3-flash" was silently replaced by PIPELINE_LOCAL_MODEL_DEFAULT. A
plain name is kept when role_registry.is_allowed_undeclared_cloud_model
accepts it (provider ollama, well-formed name, remote endpoint). Tier names
are never kept as model names. These tests use a fixture registry, never the
checked-in model_registry.json.
"""
import json

import pytest

from app.ollama_prompt_utils import _resolve_local_model

DEFAULT_MODEL = "devstral:24b"
REMOTE_ENDPOINT = "https://ollama.com"

EMPTY_REGISTRY = {"providers": {"ollama": {"models": {}}}}


@pytest.fixture()
def registry_file(tmp_path, monkeypatch):
    path = tmp_path / "model_registry.json"
    path.write_text(json.dumps(EMPTY_REGISTRY), encoding="utf-8")
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(path))
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", DEFAULT_MODEL)
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", REMOTE_ENDPOINT)
    return path


def test_plain_cloud_model_name_returned_unchanged(registry_file):
    assert _resolve_local_model("glm-5.3-flash", "ollama") == "glm-5.3-flash"


def test_second_plain_cloud_model_name_returned_unchanged(registry_file):
    assert _resolve_local_model("kimi-k3", "ollama") == "kimi-k3"


@pytest.mark.parametrize("tier", ["sonnet", "Haiku", "OPUS"])
def test_tier_name_on_cloud_endpoint_is_never_a_model_name(
    registry_file, monkeypatch, tier
):
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_SONNET", "sonnet:test")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_HAIKU", "haiku:test")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_OPUS", "opus:test")
    assert _resolve_local_model(tier, "ollama") == {
        "sonnet": "sonnet:test",
        "haiku": "haiku:test",
        "opus": "opus:test",
    }[tier.lower()]


def test_tier_name_without_tier_variable_falls_back_to_default(registry_file):
    assert _resolve_local_model("Sonnet", "ollama") == DEFAULT_MODEL


def test_plain_name_falls_back_to_default_without_remote_endpoint(
    registry_file, monkeypatch
):
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", raising=False)
    assert _resolve_local_model("glm-5.3-flash", "ollama") == DEFAULT_MODEL


def test_plain_name_falls_back_to_default_on_loopback_endpoint(
    registry_file, monkeypatch
):
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", "http://localhost:11434")
    assert _resolve_local_model("glm-5.3-flash", "ollama") == DEFAULT_MODEL


def test_plain_name_on_lmstudio_falls_back_to_default(registry_file):
    assert _resolve_local_model("glm-5.3-flash", "lmstudio") == DEFAULT_MODEL


@pytest.mark.parametrize("name", ["has space", "-leading-dash", ""])
def test_malformed_name_falls_back_to_default(registry_file, name):
    assert _resolve_local_model(name, "ollama") == DEFAULT_MODEL


def test_unreadable_registry_falls_back_to_default(tmp_path, monkeypatch):
    path = tmp_path / "model_registry.json"
    path.write_text('{"providers": {not-json', encoding="utf-8")
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(path))
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", DEFAULT_MODEL)
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", REMOTE_ENDPOINT)

    assert _resolve_local_model("glm-5.3-flash", "ollama") == DEFAULT_MODEL


def test_tagged_name_returned_unchanged_on_cloud_endpoint(registry_file):
    assert _resolve_local_model("gpt-oss:120b", "ollama") == "gpt-oss:120b"


def test_vendor_slash_name_returned_unchanged_on_cloud_endpoint(registry_file):
    assert _resolve_local_model("openai/gpt-5-mini", "ollama") == "openai/gpt-5-mini"
