"""Tests for pipeline/setup/providers.py (SETUP-05).

Covers the local-registry bootstrap, the preset -> per-role choice mapping,
and apply_choices' environment scoping / per-role error isolation.
"""

import json
import os

import pytest

from pipeline.config_provenance import PIPELINE_ROLES
from pipeline.setup import providers as setup_providers

COMMITTED = {
    "providers": {
        "claude": {
            "models": {
                "sonnet": {"tag": "sonnet"},
                "opus": {"tag": "opus"},
            }
        },
        "ollama": {
            "models": {
                "gpt-oss": {"tag": "gpt-oss:20b"},
                "glm": {"tag": "glm-5.3-flash:cloud"},
            }
        },
    },
    # Both of these must be dropped from the local registry: they are the
    # operator's own routing, not the shipped provider catalog.
    "roles": {"overlord": {"provider": "claude", "model": "sonnet"}},
    "routing": {"overlord": "claude"},
}

LOCAL_NAME = "model_registry.local.json"
COMMITTED_NAME = "model_registry.json"


def _snapshot(path):
    """(bytes, mtime_ns) of `path` — the never-write-the-committed-file guard."""
    stat = path.stat()
    return path.read_bytes(), stat.st_mtime_ns


@pytest.fixture
def repo(tmp_path):
    """A temp repo root holding a committed registry with providers+roles."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / COMMITTED_NAME).write_text(json.dumps(COMMITTED, indent=2), encoding="utf-8")
    return root


def _write_local(root, payload):
    path = root / LOCAL_NAME
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# ensure_local_registry
# --------------------------------------------------------------------------


def test_ensure_local_registry_keeps_only_providers(repo):
    committed_before = _snapshot(repo / COMMITTED_NAME)

    target = setup_providers.ensure_local_registry(repo)

    assert target == repo / LOCAL_NAME
    data = json.loads(target.read_text(encoding="utf-8"))
    assert set(data) == {"providers"}
    assert data["providers"] == COMMITTED["providers"]
    assert "roles" not in data
    assert "routing" not in data
    # The committed file is never written.
    assert _snapshot(repo / COMMITTED_NAME) == committed_before


def test_ensure_local_registry_leaves_no_temp_files(repo):
    setup_providers.ensure_local_registry(repo)

    assert sorted(p.name for p in repo.iterdir()) == [COMMITTED_NAME, LOCAL_NAME]


def test_ensure_local_registry_preserves_existing_local_file(repo):
    existing = _write_local(
        repo,
        {
            "providers": {"claude": {"models": {"sonnet": {"tag": "sonnet"}}}},
            "roles": {"review": {"provider": "claude", "model": "sonnet"}},
        },
    )
    before = _snapshot(existing)
    committed_before = _snapshot(repo / COMMITTED_NAME)

    target = setup_providers.ensure_local_registry(repo)

    assert target == existing
    assert _snapshot(existing) == before
    assert _snapshot(repo / COMMITTED_NAME) == committed_before


def test_ensure_local_registry_malformed_local_raises_and_changes_nothing(repo):
    existing = repo / LOCAL_NAME
    existing.write_text("{not json", encoding="utf-8")
    before = _snapshot(existing)
    committed_before = _snapshot(repo / COMMITTED_NAME)

    with pytest.raises(ValueError, match=LOCAL_NAME):
        setup_providers.ensure_local_registry(repo)

    assert _snapshot(existing) == before
    assert _snapshot(repo / COMMITTED_NAME) == committed_before


def test_ensure_local_registry_is_idempotent(repo):
    first = setup_providers.ensure_local_registry(repo)
    before = _snapshot(first)

    second = setup_providers.ensure_local_registry(repo)

    assert second == first
    assert _snapshot(first) == before


# --------------------------------------------------------------------------
# preset_choices
# --------------------------------------------------------------------------


def test_preset_claude_maps_every_role(repo):
    registry = json.loads((repo / COMMITTED_NAME).read_text(encoding="utf-8"))

    choices = setup_providers.preset_choices("claude", registry)

    assert [c["role"] for c in choices] == list(PIPELINE_ROLES)
    assert len(choices) == len(PIPELINE_ROLES) == 9
    assert all(c["provider"] == "claude" and c["model"] == "sonnet" for c in choices)


def test_preset_ollama_local_picks_first_non_cloud_model(repo):
    registry = json.loads((repo / COMMITTED_NAME).read_text(encoding="utf-8"))

    choices = setup_providers.preset_choices("ollama-local", registry)

    assert [c["role"] for c in choices] == list(PIPELINE_ROLES)
    assert all(c["provider"] == "ollama" and c["model"] == "gpt-oss" for c in choices)


def test_preset_ollama_cloud_picks_first_cloud_model(repo):
    registry = json.loads((repo / COMMITTED_NAME).read_text(encoding="utf-8"))

    choices = setup_providers.preset_choices("ollama-cloud", registry)

    assert [c["role"] for c in choices] == list(PIPELINE_ROLES)
    assert all(c["provider"] == "ollama" and c["model"] == "glm" for c in choices)


def test_preset_custom_is_empty(repo):
    registry = json.loads((repo / COMMITTED_NAME).read_text(encoding="utf-8"))

    assert setup_providers.preset_choices("custom", registry) == []


def test_preset_ollama_cloud_without_cloud_model_raises():
    registry = {
        "providers": {"ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}}}}
    }

    with pytest.raises(ValueError, match="ollama-cloud"):
        setup_providers.preset_choices("ollama-cloud", registry)


def test_preset_ollama_local_without_local_model_raises():
    registry = {
        "providers": {"ollama": {"models": {"glm": {"tag": "glm-5.3-flash:cloud"}}}}
    }

    with pytest.raises(ValueError, match="ollama-local"):
        setup_providers.preset_choices("ollama-local", registry)


def test_preset_claude_without_declared_model_raises():
    registry = {"providers": {"claude": {"models": {"opus": {"tag": "opus"}}}}}

    with pytest.raises(ValueError, match="claude") as excinfo:
        setup_providers.preset_choices("claude", registry)

    assert "sonnet" in str(excinfo.value)


def test_preset_claude_without_declared_provider_raises():
    registry = {"providers": {"ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}}}}}

    with pytest.raises(ValueError, match="claude") as excinfo:
        setup_providers.preset_choices("claude", registry)

    assert "claude" in str(excinfo.value)


def test_unknown_preset_raises(repo):
    registry = json.loads((repo / COMMITTED_NAME).read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="nope"):
        setup_providers.preset_choices("nope", registry)


# --------------------------------------------------------------------------
# apply_choices
# --------------------------------------------------------------------------


def _choices(*pairs):
    return [
        {"role": role, "provider": provider, "model": model}
        for role, provider, model in pairs
    ]


def test_apply_choices_calls_setter_in_order_and_restores_env(repo):
    local = setup_providers.ensure_local_registry(repo)
    committed_before = _snapshot(repo / COMMITTED_NAME)
    calls = []
    seen_env = []
    environ = {}

    def fake_setter(role, provider, model):
        calls.append((role, provider, model))
        seen_env.append(environ.get("PIPELINE_MODEL_REGISTRY_PATH"))
        return {"ok": True}

    choices = _choices(
        ("overlord", "claude", "sonnet"),
        ("review", "ollama", "gpt-oss"),
    )

    results = setup_providers.apply_choices(
        choices, registry_path=local, environ=environ, setter=fake_setter
    )

    assert calls == [("overlord", "claude", "sonnet"), ("review", "ollama", "gpt-oss")]
    assert seen_env == [str(local), str(local)]
    assert "PIPELINE_MODEL_REGISTRY_PATH" not in environ
    assert results == [
        {
            "role": "overlord",
            "provider": "claude",
            "model": "sonnet",
            "ok": True,
            "error": None,
        },
        {
            "role": "review",
            "provider": "ollama",
            "model": "gpt-oss",
            "ok": True,
            "error": None,
        },
    ]
    assert _snapshot(repo / COMMITTED_NAME) == committed_before


def test_apply_choices_restores_preexisting_env_value(repo):
    local = setup_providers.ensure_local_registry(repo)
    environ = {"PIPELINE_MODEL_REGISTRY_PATH": "/prior/registry.json"}

    setup_providers.apply_choices(
        _choices(("overlord", "claude", "sonnet")),
        registry_path=local,
        environ=environ,
        setter=lambda role, provider, model: {"ok": True},
    )

    assert environ["PIPELINE_MODEL_REGISTRY_PATH"] == "/prior/registry.json"


def test_apply_choices_restores_env_when_setter_raises(repo):
    local = setup_providers.ensure_local_registry(repo)
    environ = {"PIPELINE_MODEL_REGISTRY_PATH": "/prior/registry.json"}

    def boom(role, provider, model):
        raise RuntimeError("kaboom")

    setup_providers.apply_choices(
        _choices(("overlord", "claude", "sonnet")),
        registry_path=local,
        environ=environ,
        setter=boom,
    )

    assert environ["PIPELINE_MODEL_REGISTRY_PATH"] == "/prior/registry.json"


def test_apply_choices_empty_list_calls_setter_zero_times(repo):
    local = setup_providers.ensure_local_registry(repo)
    calls = []

    results = setup_providers.apply_choices(
        [],
        registry_path=local,
        environ={},
        setter=lambda *args: calls.append(args) or {"ok": True},
    )

    assert results == []
    assert calls == []


def test_apply_choices_unknown_role_is_recorded_and_others_applied(repo):
    local = setup_providers.ensure_local_registry(repo)
    calls = []

    results = setup_providers.apply_choices(
        _choices(
            ("not-a-role", "claude", "sonnet"),
            ("review", "claude", "sonnet"),
        ),
        registry_path=local,
        environ={},
        setter=lambda role, provider, model: calls.append(role) or {"ok": True},
    )

    assert calls == ["review"]
    assert results[0]["ok"] is False
    assert "not-a-role" in results[0]["error"]
    assert results[1]["ok"] is True


def test_apply_choices_empty_provider_or_model_is_recorded(repo):
    local = setup_providers.ensure_local_registry(repo)
    calls = []

    results = setup_providers.apply_choices(
        _choices(
            ("overlord", "", "sonnet"),
            ("planner", "claude", ""),
            ("review", "claude", "sonnet"),
        ),
        registry_path=local,
        environ={},
        setter=lambda role, provider, model: calls.append(role) or {"ok": True},
    )

    assert calls == ["review"]
    assert [r["ok"] for r in results] == [False, False, True]
    assert results[0]["error"]
    assert results[1]["error"]


def test_apply_choices_undeclared_model_fails_but_others_apply(repo, monkeypatch):
    local = setup_providers.ensure_local_registry(repo)
    committed_before = _snapshot(repo / COMMITTED_NAME)
    # The real setter resolves its target through os.environ, so scope the
    # default environ mapping (not a throwaway dict) at the local registry.
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(local))

    results = setup_providers.apply_choices(
        _choices(
            ("overlord", "claude", "no-such-model"),
            ("review", "claude", "sonnet"),
        ),
        registry_path=local,
    )

    assert results[0]["ok"] is False
    assert "no-such-model" in results[0]["error"]
    assert results[1]["ok"] is True
    written = json.loads(local.read_text(encoding="utf-8"))
    assert written["roles"]["review"] == {"provider": "claude", "model": "sonnet"}
    assert "overlord" not in written["roles"]
    assert _snapshot(repo / COMMITTED_NAME) == committed_before


def test_apply_choices_setter_failure_is_recorded_not_raised(repo):
    local = setup_providers.ensure_local_registry(repo)

    def boom(role, provider, model):
        raise RuntimeError("kaboom")

    results = setup_providers.apply_choices(
        _choices(("overlord", "claude", "sonnet")),
        registry_path=local,
        environ={},
        setter=boom,
    )

    assert results[0]["ok"] is False
    assert "kaboom" in results[0]["error"]


def test_apply_choices_setter_ok_false_is_recorded(repo):
    local = setup_providers.ensure_local_registry(repo)

    results = setup_providers.apply_choices(
        _choices(("overlord", "claude", "sonnet")),
        registry_path=local,
        environ={},
        setter=lambda role, provider, model: {"ok": False, "error": "nope"},
    )

    assert results[0]["ok"] is False
    assert results[0]["error"] == "nope"


def test_apply_choices_twice_is_byte_identical(repo):
    local = setup_providers.ensure_local_registry(repo)
    committed_before = _snapshot(repo / COMMITTED_NAME)
    choices = setup_providers.preset_choices(
        "claude", json.loads(local.read_text(encoding="utf-8"))
    )

    first = setup_providers.apply_choices(choices, registry_path=local, environ={})
    after_first = local.read_bytes()
    second = setup_providers.apply_choices(choices, registry_path=local, environ={})

    assert all(r["ok"] for r in first)
    assert all(r["ok"] for r in second)
    assert local.read_bytes() == after_first
    assert _snapshot(repo / COMMITTED_NAME) == committed_before


def test_apply_choices_default_setter_never_writes_committed_registry(repo, monkeypatch):
    """A custom environ mapping must not hide the path from the real setter."""
    local = setup_providers.ensure_local_registry(repo)
    committed_before = _snapshot(repo / COMMITTED_NAME)
    monkeypatch.delenv("PIPELINE_MODEL_REGISTRY_PATH", raising=False)
    environ = {}

    results = setup_providers.apply_choices(
        _choices(("overlord", "claude", "sonnet")),
        registry_path=local,
        environ=environ,
    )

    assert results[0]["ok"] is True
    assert _snapshot(repo / COMMITTED_NAME) == committed_before
    assert json.loads(local.read_text(encoding="utf-8"))["roles"]["overlord"] == {
        "provider": "claude",
        "model": "sonnet",
    }
    assert "PIPELINE_MODEL_REGISTRY_PATH" not in environ
    assert "PIPELINE_MODEL_REGISTRY_PATH" not in os.environ


# --------------------------------------------------------------------------
# Integration: the real setter + app.role_registry.resolve_role
# --------------------------------------------------------------------------


def test_real_setter_makes_all_roles_resolve(repo, monkeypatch):
    from app import role_registry

    local = setup_providers.ensure_local_registry(repo)
    committed_before = _snapshot(repo / COMMITTED_NAME)
    choices = setup_providers.preset_choices(
        "claude", json.loads(local.read_text(encoding="utf-8"))
    )
    # The real setter resolves its target through os.environ, so scope the
    # default environ mapping (not a throwaway dict) at the local registry.
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(local))

    results = setup_providers.apply_choices(choices, registry_path=local)

    assert [r["ok"] for r in results] == [True] * len(PIPELINE_ROLES)
    for role in PIPELINE_ROLES:
        resolution = role_registry.resolve_role(role)
        assert resolution.provider == "claude", role
        assert resolution.model == "sonnet", role
    assert _snapshot(repo / COMMITTED_NAME) == committed_before
