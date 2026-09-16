"""Unit tests for ``pipeline.provider_choice.build_choice_model``.

Contract under test (PURE logic only - no prompting, printing, file writes
or network access)::

    build_choice_model(effective_config: dict, registry: dict) -> list[dict]

``effective_config`` is the dict returned by
``PipelineService.get_effective_config()``: it carries a ``"roles"`` list of
per-role entries shaped like
``pipeline.config_provenance.resolve_role_provenance`` (``role``,
``provider``, ``model``, ``provider_source``, ``model_source``,
``restart_required``, ``error``).

``registry`` is the dict returned by ``app.role_registry.load_registry()``::

    {"providers": {name: {"models": {logical_name: {...}}}}, "roles": {...}}

Every test below passes SYNTHETIC fixtures for both arguments. Per
``.claude/rules/testing-config-gates.md`` nothing here asserts against
whatever this machine currently has configured, and nothing here calls
``load_registry()`` / ``get_effective_config()`` - the function under test
must never call them itself either.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from pipeline.config_provenance import PIPELINE_ROLES


def build_choice_model(effective_config, registry):
    """Lazy proxy for ``pipeline.provider_choice.build_choice_model``.

    The import happens inside the call rather than at module import time so
    this suite stays collectible - and ``ruff check`` stays clean - before the
    implementation module exists.  Until then every test below fails with a
    plain ``ModuleNotFoundError`` for ``pipeline.provider_choice``, which is
    the intended red state.
    """
    from pipeline.provider_choice import build_choice_model as _impl

    return _impl(effective_config, registry)


# --------------------------------------------------------------------------
# Synthetic fixtures
# --------------------------------------------------------------------------


def _entry(
    role,
    *,
    provider=None,
    model=None,
    provider_source=None,
    model_source=None,
    error=None,
):
    """One per-role entry in the shape ``get_effective_config()`` emits."""
    return {
        "role": role,
        "provider": provider,
        "model": model,
        "provider_source": provider_source,
        "model_source": model_source,
        "restart_required": False,
        "error": error,
    }


def _effective_config(entries):
    """The full ``get_effective_config()`` dict, with synthetic role entries."""
    return {
        "ok": True,
        "roles": list(entries),
        "env": [],
        "ignored_env_vars": [],
        "sources": {},
    }


def _all_role_entries():
    """One distinct entry per role, so any index/alignment bug is visible."""
    return [
        _entry(
            role,
            provider=f"prov-{role}",
            model=f"model-{role}",
            provider_source="model_registry.json",
            model_source="model_registry.json",
        )
        for role in PIPELINE_ROLES
    ]


def _registry(providers=None, roles=None):
    reg: dict = {}
    if providers is not None:
        reg["providers"] = providers
    if roles is not None:
        reg["roles"] = roles
    return reg


def _two_provider_registry():
    """Providers/models deliberately declared out of sorted order."""
    return _registry(
        providers={
            "zeta": {"models": {"b-model": {}, "a-model": {}}},
            "alpha": {"models": {"z-model": {}, "a-model": {}}},
        }
    )


def _expected_pairs():
    return [
        {"provider": "alpha", "model": "a-model"},
        {"provider": "alpha", "model": "z-model"},
        {"provider": "zeta", "model": "a-model"},
        {"provider": "zeta", "model": "b-model"},
    ]


def _by_role(result):
    return {entry["role"]: entry for entry in result}


def _error_strings(node):
    """Every non-None ``error`` value anywhere inside ``node``."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "error" and value is not None:
                found.append(value)
            found.extend(_error_strings(value))
    elif isinstance(node, (list, tuple)):
        for item in node:
            found.extend(_error_strings(item))
    return found


def _module_source() -> str:
    path = Path(__file__).resolve().parents[2] / "pipeline" / "provider_choice.py"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Shape / ordering of the returned list
# --------------------------------------------------------------------------


def test_returns_one_entry_per_role_in_pipeline_roles_order():
    result = build_choice_model(_effective_config(_all_role_entries()), _two_provider_registry())

    assert isinstance(result, list)
    assert [entry["role"] for entry in result] == list(PIPELINE_ROLES)
    assert len(result) == len(PIPELINE_ROLES)


def test_every_pipeline_role_appears_exactly_once():
    result = build_choice_model(_effective_config(_all_role_entries()), _two_provider_registry())

    roles = [entry["role"] for entry in result]
    assert sorted(roles) == sorted(PIPELINE_ROLES)
    assert len(set(roles)) == len(roles)
    # The nine roles the brief names, as a fixed anchor.
    assert set(roles) == {
        "overlord",
        "planner",
        "dispatch",
        "review",
        "decompose",
        "test_author",
        "diagnosis",
        "security",
        "chat",
    }


# --------------------------------------------------------------------------
# 'current' - provider / model / provenance source
# --------------------------------------------------------------------------


def test_current_reflects_effective_config_provider_model_and_source():
    result = build_choice_model(_effective_config(_all_role_entries()), _two_provider_registry())

    for entry in result:
        role = entry["role"]
        current = entry["current"]
        assert current["provider"] == f"prov-{role}"
        assert current["model"] == f"model-{role}"
        assert current["source"] == "model_registry.json"


def test_current_source_is_a_provenance_string_from_the_effective_config():
    """``source`` must be the provenance the effective config reported.

    ``resolve_role_provenance`` reports two provenance labels
    (``provider_source`` / ``model_source``); the brief only requires that
    ``source`` be *the* provenance string, so either label is accepted here -
    but it must be one of them, not an invented value.
    """
    entries = [
        _entry(
            "planner",
            provider="claude",
            model="opus",
            provider_source="env:PIPELINE_BACKEND_PLANNER",
            model_source="model_registry.json",
        )
    ]
    result = build_choice_model(_effective_config(entries), _two_provider_registry())

    current = _by_role(result)["planner"]["current"]
    source = current["source"]
    assert isinstance(source, str) and source
    # Either provenance label on its own, or a string that carries both.
    assert source in {"env:PIPELINE_BACKEND_PLANNER", "model_registry.json"} or (
        "env:PIPELINE_BACKEND_PLANNER" in source and "model_registry.json" in source
    )


def test_current_source_is_none_when_entry_carries_no_provenance():
    entries = [_entry("chat", provider="claude", model="opus")]
    result = build_choice_model(_effective_config(entries), _two_provider_registry())

    current = _by_role(result)["chat"]["current"]
    assert current["provider"] == "claude"
    assert current["model"] == "opus"
    assert current["source"] is None


# --------------------------------------------------------------------------
# 'options' - every declared provider/model pair, sorted
# --------------------------------------------------------------------------


def test_options_cover_every_provider_model_pair_sorted():
    result = build_choice_model(_effective_config(_all_role_entries()), _two_provider_registry())

    for entry in result:
        assert entry["options"] == _expected_pairs()


def test_options_are_identical_for_every_role():
    result = build_choice_model(_effective_config(_all_role_entries()), _two_provider_registry())

    option_lists = [entry["options"] for entry in result]
    assert all(options == option_lists[0] for options in option_lists)


def test_options_are_sorted_by_provider_then_model():
    result = build_choice_model(_effective_config(_all_role_entries()), _two_provider_registry())

    for entry in result:
        options = entry["options"]
        assert options == sorted(options, key=lambda o: (o["provider"], o["model"]))
        for option in options:
            assert set(option) >= {"provider", "model"}


def test_model_declared_with_falsy_value_still_contributes_an_option():
    """A declared model key counts as a pair regardless of its value."""
    registry = _registry(providers={"claude": {"models": {"opus": None, "sonnet": {}}}})
    result = build_choice_model(_effective_config(_all_role_entries()), registry)

    assert result[0]["options"] == [
        {"provider": "claude", "model": "opus"},
        {"provider": "claude", "model": "sonnet"},
    ]


def test_registry_roles_block_does_not_affect_options():
    """Only the ``providers`` block declares options, not ``roles``."""
    registry = _registry(
        providers={"claude": {"models": {"opus": {}}}},
        roles={"planner": {"provider": "ghost", "model": "phantom"}},
    )
    result = build_choice_model(_effective_config(_all_role_entries()), registry)

    assert result[0]["options"] == [{"provider": "claude", "model": "opus"}]


# --------------------------------------------------------------------------
# Negative / boundary cases
# --------------------------------------------------------------------------


def test_registry_without_providers_block_yields_empty_options():
    result = build_choice_model(_effective_config(_all_role_entries()), {})

    assert len(result) == len(PIPELINE_ROLES)
    for entry in result:
        assert entry["options"] == []


def test_registry_with_only_roles_block_yields_empty_options():
    registry = _registry(roles={"planner": {"provider": "claude", "model": "opus"}})
    result = build_choice_model(_effective_config(_all_role_entries()), registry)

    for entry in result:
        assert entry["options"] == []


def test_empty_providers_block_yields_empty_options():
    result = build_choice_model(_effective_config(_all_role_entries()), _registry(providers={}))

    for entry in result:
        assert entry["options"] == []


def test_provider_with_empty_models_dict_contributes_no_options():
    registry = _registry(
        providers={
            "claude": {"models": {}},
            "ollama": {"models": {"llama3": {}}},
        }
    )
    result = build_choice_model(_effective_config(_all_role_entries()), registry)

    assert result[0]["options"] == [{"provider": "ollama", "model": "llama3"}]


def test_provider_without_models_key_contributes_no_options():
    registry = _registry(
        providers={
            "claude": {},
            "ollama": {"models": {"llama3": {}}},
        }
    )
    result = build_choice_model(_effective_config(_all_role_entries()), registry)

    assert result[0]["options"] == [{"provider": "ollama", "model": "llama3"}]


@pytest.mark.parametrize("bad_providers", [None, []])
def test_malformed_providers_value_yields_empty_options(bad_providers):
    registry = {"providers": bad_providers}
    result = build_choice_model(_effective_config(_all_role_entries()), registry)

    assert len(result) == len(PIPELINE_ROLES)
    for entry in result:
        assert entry["options"] == []


def test_role_missing_from_effective_config_gets_none_current():
    entries = [e for e in _all_role_entries() if e["role"] != "chat"]
    result = build_choice_model(_effective_config(entries), _two_provider_registry())

    entry = _by_role(result)["chat"]
    assert entry["role"] == "chat"
    assert entry["current"]["provider"] is None
    assert entry["current"]["model"] is None
    assert entry["current"]["source"] is None
    assert entry["options"] == _expected_pairs()


def test_empty_effective_config_gives_none_current_for_every_role():
    result = build_choice_model(_effective_config([]), _two_provider_registry())

    assert [entry["role"] for entry in result] == list(PIPELINE_ROLES)
    for entry in result:
        assert entry["current"]["provider"] is None
        assert entry["current"]["model"] is None
        assert entry["current"]["source"] is None


def test_effective_config_without_roles_key_gives_none_current_for_every_role():
    result = build_choice_model({"ok": True}, _two_provider_registry())

    assert [entry["role"] for entry in result] == list(PIPELINE_ROLES)
    for entry in result:
        assert entry["current"]["provider"] is None
        assert entry["current"]["model"] is None
        assert entry["current"]["source"] is None


def test_role_error_is_surfaced_not_raised():
    message = "Role chat has no model configured"
    entries = [
        _entry("chat", provider="claude", model=None, provider_source="env", model_source="unset", error=message)
    ]
    result = build_choice_model(_effective_config(entries), _two_provider_registry())

    entry = _by_role(result)["chat"]
    assert message in _error_strings(entry)


def test_role_error_keeps_the_provider_it_did_resolve():
    message = "Role chat has no model configured"
    entries = [
        _entry("chat", provider="claude", model=None, provider_source="env", model_source="unset", error=message)
    ]
    result = build_choice_model(_effective_config(entries), _two_provider_registry())

    current = _by_role(result)["chat"]["current"]
    assert current["provider"] == "claude"
    assert current["model"] is None


def test_unresolvable_role_error_is_surfaced_not_raised():
    message = "Role planner has provider 'ghost' not declared in registry"
    entries = [
        _entry("planner", provider=None, model=None, provider_source=None, model_source=None, error=message)
    ]
    result = build_choice_model(_effective_config(entries), _two_provider_registry())

    entry = _by_role(result)["planner"]
    assert message in _error_strings(entry)
    assert entry["current"]["provider"] is None
    assert entry["current"]["model"] is None


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_ordering_is_deterministic_across_repeated_calls():
    config = _effective_config(_all_role_entries())
    registry = _two_provider_registry()

    first = build_choice_model(config, registry)
    second = build_choice_model(config, registry)

    assert first == second


def test_output_is_independent_of_input_insertion_order():
    registry = _two_provider_registry()
    shuffled_registry = _registry(
        providers={
            "alpha": {"models": {"z-model": {}, "a-model": {}}},
            "zeta": {"models": {"a-model": {}, "b-model": {}}},
        }
    )
    entries = _all_role_entries()
    shuffled_entries = list(reversed(entries))

    baseline = build_choice_model(_effective_config(entries), registry)
    shuffled = build_choice_model(_effective_config(shuffled_entries), shuffled_registry)

    assert shuffled == baseline


# --------------------------------------------------------------------------
# Purity - the module must not prompt, print, read config, write files or
# touch the network.
# --------------------------------------------------------------------------


def test_never_calls_load_registry_or_get_effective_config(monkeypatch):
    from app import role_registry
    from pipeline.service import PipelineService

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on a violation
        raise AssertionError("build_choice_model must not call this itself")

    monkeypatch.setattr(role_registry, "load_registry", _boom)
    monkeypatch.setattr(PipelineService, "get_effective_config", _boom)

    result = build_choice_model(_effective_config(_all_role_entries()), _two_provider_registry())

    assert [entry["role"] for entry in result] == list(PIPELINE_ROLES)


@pytest.mark.parametrize(
    "token",
    ["input(", "print(", "load_registry(", "get_effective_config("],
)
def test_module_source_has_no_forbidden_calls(token):
    assert token not in _module_source(), (
        f"pipeline/provider_choice.py must stay pure: found {token!r}"
    )


@pytest.mark.parametrize(
    "token",
    [
        "open(",
        "import subprocess",
        "import requests",
        "import urllib",
        "import socket",
        "import httpx",
    ],
)
def test_module_source_does_no_file_writes_or_network(token):
    assert token not in _module_source(), (
        f"pipeline/provider_choice.py must do no file writes/network: found {token!r}"
    )


def test_module_exposes_exactly_one_public_function():
    import pipeline.provider_choice as mod

    public = [
        name
        for name, obj in vars(mod).items()
        if not name.startswith("_")
        and inspect.isfunction(obj)
        and obj.__module__ == mod.__name__
    ]
    assert public == ["build_choice_model"]


def test_signature_takes_effective_config_and_registry():
    from pipeline.provider_choice import build_choice_model as impl

    params = list(inspect.signature(impl).parameters)
    assert params[:2] == ["effective_config", "registry"]
