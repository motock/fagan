"""SG-03: a model tag declared in the registry must survive _resolve_local_model.

_resolve_local_model() used to rewrite every bare word that was not one of
opus/sonnet/haiku to PIPELINE_LOCAL_MODEL_DEFAULT. Registry-declared tags such
as the smoke stack's "scripted-dispatch" have neither ':' nor '/', so the
driver silently ran the default model instead. These tests pin the fixed
contract against a fixture registry (never the checked-in model_registry.json).
"""
import json

import pytest

from app import role_registry
from app.ollama_prompt_utils import _is_declared_tag, _resolve_local_model

DEFAULT_MODEL = "devstral:24b"

FIXTURE_REGISTRY = {
    "providers": {
        "provider-a": {
            "models": {
                "alpha": {"tag": "alpha-tag"},
                "colon": {"tag": "plain:tag"},
            }
        },
        # "shared-tag" is declared here only - never under provider-a.
        "provider-b": {"models": {"shared": {"tag": "shared-tag"}}},
    }
}


@pytest.fixture()
def registry_file(tmp_path, monkeypatch):
    path = tmp_path / "model_registry.json"
    path.write_text(json.dumps(FIXTURE_REGISTRY), encoding="utf-8")
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(path))
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", DEFAULT_MODEL)
    return path


def test_bare_declared_tag_returned_unchanged(registry_file):
    assert _is_declared_tag("alpha-tag", "provider-a") is True
    assert _resolve_local_model("alpha-tag", "provider-a") == "alpha-tag"


def test_tag_declared_under_other_provider_falls_back(registry_file):
    """Provider scoping: a tag declared for provider-b is not provider-a's."""
    assert _is_declared_tag("shared-tag", "provider-a") is False
    assert _resolve_local_model("shared-tag", "provider-a") == DEFAULT_MODEL
    # ...while the owning provider still gets it back unchanged.
    assert _is_declared_tag("shared-tag", "provider-b") is True
    assert _resolve_local_model("shared-tag", "provider-b") == "shared-tag"


def test_undeclared_bare_word_falls_back(registry_file):
    assert _is_declared_tag("nonsense", "provider-a") is False
    assert _resolve_local_model("nonsense", "provider-a") == DEFAULT_MODEL


def test_empty_tier_falls_back(registry_file):
    assert _is_declared_tag("", "provider-a") is False
    assert _resolve_local_model("", "provider-a") == DEFAULT_MODEL


def test_tier_name_still_resolves_through_env(registry_file, monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_SONNET", "sonnet:test")
    assert _resolve_local_model("sonnet", "provider-a") == "sonnet:test"


def test_invalid_json_fails_closed_to_default(tmp_path, monkeypatch):
    path = tmp_path / "model_registry.json"
    path.write_text('{"providers": {not-json', encoding="utf-8")
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(path))
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", DEFAULT_MODEL)

    assert _is_declared_tag("alpha-tag", "provider-a") is False
    assert _resolve_local_model("alpha-tag", "provider-a") == DEFAULT_MODEL


def test_separator_tag_never_reads_registry(registry_file, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("load_registry must not be called for a concrete tag")

    monkeypatch.setattr(role_registry, "load_registry", _boom)
    assert _resolve_local_model("plain:tag", "provider-a") == "plain:tag"
    assert _resolve_local_model("vendor/model", "provider-a") == "vendor/model"
