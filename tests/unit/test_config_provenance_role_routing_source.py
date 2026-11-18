"""A role's provider/model source can be the settings layer (hpc-07).

``resolve_role_provenance`` labels which layer supplied the winning
provider/model. The plan's ``role_routing`` setting is now one of those
sources: ``resolve_role`` receives it (so the reported provider/model is the
one that would actually be used) and the label becomes ``settings:<layer>``,
naming the settings layer that supplied the value. Every other source name is
unchanged - the rest of the suite pins them.

The settings Store is stubbed (one fake ``records()`` reader); the real
resolver, registry validation and ``role_routing_scope`` read run for real.
"""

from __future__ import annotations

import json

import pytest

from pipeline import (
    config_provenance,
    role_routing_provenance,
    role_routing_scope,
    server,
)

NAMESPACE = "default"
TEAM = "alpha"
REPO = ""
MODEL_REGISTRY = {
    "providers": {
        "claude": {"models": {"sonnet": {"tag": "claude-sonnet-5-5"}}},
        "ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b", "class": "on-device"}}},
    },
    "roles": {},
}
ROUTING = {"review": {"provider": "ollama", "model": "gpt-oss"}}


class _FakeRecords:
    """The Store's ``get_settings`` boundary, stubbed for one context."""

    def __init__(self, rows):
        self._rows = rows

    def get_settings(self, namespace, scope, scope_id, repo):
        return self._rows.get((scope, scope_id, repo))


class _FakeStore:
    def __init__(self, rows):
        self._records = _FakeRecords(rows)

    def records(self):
        return self._records


class _Context:
    """The settings context ``role_routing_scope`` reads from ``ci``."""

    namespace = NAMESPACE
    team = TEAM
    repo = REPO


@pytest.fixture(autouse=True)
def model_registry(tmp_path, monkeypatch):
    path = tmp_path / "model_registry.json"
    path.write_text(json.dumps(MODEL_REGISTRY))
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(path))
    monkeypatch.delenv("PIPELINE_SETTINGS_PLUGIN", raising=False)
    return path


@pytest.fixture
def store(monkeypatch):
    """Replace the Store with one that answers the team layer of this context."""

    def _install(values=None, scope="team", scope_id=TEAM, repo=REPO):
        rows = {}
        if values is not None:
            rows[(scope, scope_id, repo)] = {"values": values, "allow_loosen": []}
        monkeypatch.setattr(server, "_store", _FakeStore(rows))
        return rows

    return _install


@pytest.fixture
def plan_context(monkeypatch):
    """Point ``role_routing_scope``'s plan read at this story's context."""
    monkeypatch.setattr(role_routing_scope, "_context_for_plan", lambda plan: _Context())


def _resolve(**kwargs):
    return config_provenance.resolve_role_provenance("review", **kwargs)


def _registry_with(roles=None, providers=None):
    return {
        "roles": roles or {},
        "providers": providers
        or {
            "claude": {"models": {"sonnet": {"tag": "claude-sonnet-5-5"}}},
            "ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}}},
        },
    }


# ---------------------------------------------------------------------------
# One test per source category.
# ---------------------------------------------------------------------------


def test_source_is_settings_colon_layer_when_the_layer_supplies_the_role():
    result = _resolve(
        registry=_registry_with(),
        environ={},
        layer_routing=ROUTING,
        layer_routing_source="team",
    )

    assert (result["provider"], result["model"]) == ("ollama", "gpt-oss:20b")
    assert result["provider_source"] == "settings:team"
    assert result["model_source"] == "settings:team"
    assert result["error"] is None


def test_source_is_settings_colon_the_layer_that_actually_supplied_it():
    result = _resolve(
        registry=_registry_with(),
        environ={},
        layer_routing=ROUTING,
        layer_routing_source="team_repo",
    )

    assert result["provider_source"] == "settings:team_repo"
    assert result["model_source"] == "settings:team_repo"


def test_source_is_plan_role_config_when_the_plan_names_the_role():
    result = _resolve(
        plan_role_config={"review": {"provider": "claude", "model": "sonnet"}},
        registry=_registry_with(),
        environ={},
        layer_routing=ROUTING,
        layer_routing_source="team",
    )

    assert result["provider"] == "claude"
    assert result["provider_source"] == "plan_role_config"
    assert result["model_source"] == "plan_role_config"


def test_source_is_the_registry_when_no_layer_routing_is_set():
    result = _resolve(
        registry=_registry_with(
            roles={"review": {"provider": "claude", "model": "sonnet"}}
        ),
        environ={},
    )

    assert result["provider_source"] == "model_registry.json"
    assert result["model_source"] == "model_registry.json"


def test_source_is_the_env_var_for_an_empty_registry():
    result = _resolve(
        registry=_registry_with(),
        environ={"PIPELINE_BACKEND_REVIEW": "ollama"},
        model_fallback="gpt-oss",
    )

    assert result["provider_source"] == "env:PIPELINE_BACKEND_REVIEW"
    assert result["restart_required"] is True


def test_source_is_default_and_caller_fallback_with_nothing_supplied():
    result = _resolve(
        registry=_registry_with(
            providers={"claude": {"models": {"haiku": {"tag": "claude-haiku-5"}}}}
        ),
        environ={},
        model_fallback="haiku",
    )

    assert result["provider_source"] == "default"
    assert result["model_source"] == "caller_fallback"


# ---------------------------------------------------------------------------
# The two negatives.
# ---------------------------------------------------------------------------


def test_layer_routing_does_not_override_a_plan_role_config_provider():
    """The negative of the new label: a higher layer keeps its own source, and
    the settings mapping never wins over the plan."""
    result = _resolve(
        plan_role_config={"review": {"provider": "claude"}},
        registry=_registry_with(),
        environ={},
        layer_routing=ROUTING,
        layer_routing_source="team",
        model_fallback="sonnet",
    )

    assert result["provider"] == "claude"
    assert result["provider_source"] == "plan_role_config"
    assert result["model_source"] == "caller_fallback"


def test_layer_routing_for_another_role_adds_no_label():
    """A routing entry for a different role changes neither the values nor the
    source names - the pre-existing labels survive untouched."""
    result = _resolve(
        registry=_registry_with(),
        environ={},
        layer_routing={"dispatch": {"provider": "ollama", "model": "gpt-oss"}},
        layer_routing_source="team",
    )

    assert result["provider_source"] == "default"
    assert result["model_source"] == "unset"


# ---------------------------------------------------------------------------
# The settings read itself: the Store is stubbed, the resolver is real.
# ---------------------------------------------------------------------------


def test_plan_layer_routing_reports_the_layer_it_came_from(store, plan_context):
    store({"role_routing": ROUTING})

    setting = role_routing_scope.layer_routing_setting_for_plan("some-plan")

    assert setting["value"] == ROUTING
    assert setting["source"] == "team"


def test_effective_plan_role_config_labels_the_settings_layer(store, plan_context):
    store({"role_routing": ROUTING})

    entries = role_routing_provenance.effective_plan_role_config(
        "some-plan", registry=_registry_with(), environ={}
    )
    review = next(entry for entry in entries if entry["role"] == "review")

    assert (review["provider"], review["model"]) == ("ollama", "gpt-oss:20b")
    assert review["provider_source"] == "settings:team"
    assert review["model_source"] == "settings:team"


def test_effective_plan_role_config_without_the_setting_keeps_old_labels(
    store, plan_context
):
    store({})

    entries = role_routing_provenance.effective_plan_role_config(
        "some-plan", registry=_registry_with(), environ={}
    )
    review = next(entry for entry in entries if entry["role"] == "review")

    assert review["provider_source"] == "default"
    assert review["model_source"] == "unset"


def test_a_failing_store_read_raises_nothing_and_labels_nothing(
    monkeypatch, plan_context
):
    class _Broken:
        def records(self):
            raise RuntimeError("store is down")

    monkeypatch.setattr(server, "_store", _Broken())

    assert role_routing_scope.layer_routing_setting_for_plan("some-plan") == {}
    entries = role_routing_provenance.effective_plan_role_config(
        "some-plan", registry=_registry_with(), environ={}
    )
    assert {entry["provider_source"] for entry in entries} == {"default"}


# ---------------------------------------------------------------------------
# The operator view passes the plan's setting into the provenance resolution.
# ---------------------------------------------------------------------------


def test_get_effective_config_reports_the_plans_settings_layer(
    store, plan_context, monkeypatch, tmp_path
):
    from pipeline import ci, persistence

    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(persistence, "PLAN_DIR", plans)
    store({"role_routing": ROUTING})

    result = ci._get_effective_config_impl("some-plan")
    review = next(entry for entry in result["roles"] if entry["role"] == "review")

    assert (review["provider"], review["model"]) == ("ollama", "gpt-oss:20b")
    assert review["provider_source"] == "settings:team"
    assert review["model_source"] == "settings:team"


def test_get_effective_config_without_a_plan_reads_no_routing():
    """The no-argument view is untouched: no plan, no settings read, and every
    pre-existing source label survives."""
    from pipeline import ci

    result = ci._get_effective_config_impl()
    sources = {entry["provider_source"] for entry in result["roles"]}
    labelled = {source for source in sources if isinstance(source, str)}

    assert not any(source.startswith("settings:") for source in labelled)
    assert "settings" not in labelled
