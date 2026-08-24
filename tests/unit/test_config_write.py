"""Config WRITE surface: PipelineService.set_role_default.

Security-sensitive: a bad edit retargets a pipeline role to an arbitrary
model. We validate against the registry BEFORE writing so a typo never
reaches disk, fail closed, and write atomically only to the registry path.
"""
import json

import pytest

from pipeline.server import PipelineService

REGISTRY_PATH_ENV = "PIPELINE_MODEL_REGISTRY_PATH"


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(path))
    return path


@pytest.fixture
def seeded(registry_path):
    registry = {
        "providers": {
            "claude": {
                "models": {
                    "opus": {"tag": "opus"},
                    "sonnet": {"tag": "sonnet"},
                    "haiku": {"tag": "haiku"},
                }
            },
            "ollama": {
                "models": {
                    "glm": {"tag": "glm-5.2:cloud"},
                    "devstral": {"tag": "devstral:24b"},
                }
            },
        },
        "roles": {
            "review": {"provider": "ollama", "model": "glm"},
            "planner": {"provider": "claude", "model": "sonnet"},
        },
    }
    registry_path.write_text(json.dumps(registry, indent=2))
    return registry


def _svc() -> PipelineService:
    return PipelineService()


def _bytes(path):
    return path.read_bytes()


def test_valid_write_persists_and_reflects_in_effective_config(seeded, registry_path):
    result = _svc().set_role_default("review", "claude", "sonnet")
    assert result == {"ok": True, "role": "review", "provider": "claude", "model": "sonnet"}

    # Persisted to disk.
    on_disk = json.loads(registry_path.read_text())
    assert on_disk["roles"]["review"] == {"provider": "claude", "model": "sonnet"}

    # load_registry() reflects it.
    from app import role_registry

    reg = role_registry.load_registry()
    assert reg["roles"]["review"] == {"provider": "claude", "model": "sonnet"}

    # get_effective_config() reflects it for the review role.
    eff = _svc().get_effective_config()
    review = next(r for r in eff["roles"] if r["role"] == "review")
    assert review["provider"] == "claude"
    assert review["model"] == "sonnet"


def test_unknown_provider_rejected_file_unchanged(seeded, registry_path):
    before = _bytes(registry_path)
    result = _svc().set_role_default("review", "gibberish", "sonnet")
    assert result["ok"] is False
    assert "provider" in result["error"]
    assert _bytes(registry_path) == before


def test_unknown_model_for_known_provider_rejected_file_unchanged(seeded, registry_path):
    before = _bytes(registry_path)
    result = _svc().set_role_default("review", "claude", "nonexistent")
    assert result["ok"] is False
    assert "model" in result["error"]
    assert _bytes(registry_path) == before


def test_unknown_role_rejected(seeded, registry_path):
    before = _bytes(registry_path)
    result = _svc().set_role_default("notarole", "claude", "sonnet")
    assert result["ok"] is False
    assert "role" in result["error"]
    assert _bytes(registry_path) == before


@pytest.mark.parametrize(
    "provider,model",
    [
        (None, "sonnet"),
        ("claude", None),
        ("", "sonnet"),
        ("claude", ""),
    ],
)
def test_none_or_empty_provider_or_model_rejected(seeded, registry_path, provider, model):
    before = _bytes(registry_path)
    result = _svc().set_role_default("review", provider, model)
    assert result["ok"] is False
    assert _bytes(registry_path) == before


def test_rejected_call_leaves_file_byte_identical(seeded, registry_path):
    before = _bytes(registry_path)
    _svc().set_role_default("review", "gibberish", "sonnet")
    _svc().set_role_default("review", "claude", "nonexistent")
    _svc().set_role_default("notarole", "claude", "sonnet")
    _svc().set_role_default("review", None, "sonnet")
    assert _bytes(registry_path) == before


def test_valid_write_preserves_other_roles_and_providers_verbatim(seeded, registry_path):
    before = json.loads(registry_path.read_text())
    _svc().set_role_default("review", "claude", "sonnet")
    on_disk = json.loads(registry_path.read_text())

    # Other roles preserved verbatim.
    assert on_disk["roles"]["planner"] == before["roles"]["planner"]
    # All providers preserved verbatim.
    assert on_disk["providers"] == before["providers"]
    # Only the target role changed.
    assert on_disk["roles"]["review"] == {"provider": "claude", "model": "sonnet"}
