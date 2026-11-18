"""The settings layer is a role's provenance source (hpc-07).

``config_provenance.resolve_role_provenance`` labels which layer supplied a
role's winning provider/model. A plan's ``role_routing`` setting is one of
those sources: the routing reaches ``app.role_registry.resolve_role`` (so the
reported provider/model is the one that would actually be used) and the label
becomes ``settings:<layer>``, naming the settings layer that supplied the
value. Every other source name - ``plan_role_config``,
``model_registry.json``, ``env:PIPELINE_BACKEND_<ROLE>``, ``default``,
``caller_fallback``, ``unset`` - is unchanged; the rest of the suite pins
them.

``effective_role_config_with_routing`` is the whole-config seam a caller uses
once it has resolved the setting (value plus source) for its scope.

The settings Store is never read: this story's scope is the pure resolution,
so its inputs arrive as the resolved ``(value, source)`` pair the Store
returns. The model registry is a stub file; the live one is never read.
"""

from __future__ import annotations

import json

import pytest

from pipeline import (
    ci,
    config_provenance,
    server,
)

NAMESPACE = "default"
TEAM = "alpha"
REPO = ""
ROUTING = {"review": {"provider": "ollama", "model": "gpt-oss"}}


def _registry(roles=None):
    """A model registry stub: two providers, no role pins unless asked."""
    return {
        "roles": roles or {},
        "providers": {
            "claude": {"models": {"sonnet": {"tag": "claude-sonnet-5-5"}}},
            "ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}}},
        },
    }


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


@pytest.fixture(autouse=True)
def model_registry(tmp_path, monkeypatch):
    """Point the model registry at a stub file; the live one is never read."""
    path = tmp_path / "model_registry.json"
    path.write_text(json.dumps(_registry()))
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(path))
    monkeypatch.delenv("PIPELINE_SETTINGS_PLUGIN", raising=False)
    return path


@pytest.fixture
def store(monkeypatch):
    """Install a Store stub, so no test can read the live one."""

    def _install(values=None):
        rows = {}
        if values is not None:
            rows[("team", "alpha", "")] = {"values": values, "allow_loosen": []}
        monkeypatch.setattr(server, "_store", _FakeStore(rows))
        return rows

    return _install


def _setting(value, source):
    """The ``effective_setting`` result shape for one scope's routing."""
    return {"key": "role_routing", "value": value, "source": source}


def _resolve(**kwargs):
    return config_provenance.resolve_role_provenance("review", **kwargs)


def _review_entry(setting):
    """The review role's entry in the whole-config provenance view."""
    entries = config_provenance.effective_role_config_with_routing(
        setting, registry=_registry(), environ={}
    )
    return next(entry for entry in entries if entry["role"] == "review")


# ---------------------------------------------------------------------------
# One test per source category.
# ---------------------------------------------------------------------------


def test_the_settings_layer_is_the_source_when_it_supplies_the_role():
    """The new category: the settings layer supplied the winning values."""
    result = _resolve(
        registry=_registry(),
        environ={},
        layer_routing=ROUTING,
        layer_routing_source="team",
    )

    assert (result["provider"], result["model"]) == ("ollama", "gpt-oss:20b")
    assert result["provider_source"] == "settings:team"
    assert result["model_source"] == "settings:team"
    assert result["error"] is None


def test_plan_role_config_is_still_its_own_source():
    result = _resolve(
        registry=_registry(),
        environ={},
        plan_role_config={"review": {"provider": "claude", "model": "sonnet"}},
    )

    assert result["provider_source"] == "plan_role_config"
    assert result["model_source"] == "plan_role_config"


def test_model_registry_is_still_its_own_source():
    result = _resolve(
        registry=_registry(roles={"review": {"provider": "claude", "model": "sonnet"}}),
        environ={},
    )

    assert result["provider_source"] == "model_registry.json"
    assert result["model_source"] == "model_registry.json"


def test_env_var_is_still_its_own_source():
    result = _resolve(
        registry=_registry(),
        environ={"PIPELINE_BACKEND_REVIEW": "claude"},
        model_fallback="sonnet",
    )

    assert result["provider_source"] == "env:PIPELINE_BACKEND_REVIEW"
    assert result["model_source"] == "caller_fallback"


def test_default_provider_is_still_its_own_source():
    result = _resolve(registry=_registry(), environ={})

    assert result["provider_source"] == "default"
    assert result["model_source"] == "unset"


def test_caller_fallback_is_still_its_own_source():
    result = _resolve(registry=_registry(), environ={}, model_fallback="sonnet")

    assert result["provider_source"] == "default"
    assert result["model_source"] == "caller_fallback"


# ---------------------------------------------------------------------------
# The two negatives: the layer must not label what it did not supply.
# ---------------------------------------------------------------------------


def test_routing_for_another_role_adds_no_settings_label():
    """A routing entry for a different role changes neither the values nor
    the source names - the pre-existing labels survive untouched."""
    result = _resolve(
        registry=_registry(),
        environ={},
        layer_routing={"dispatch": {"provider": "ollama", "model": "gpt-oss"}},
        layer_routing_source="team",
    )

    assert result["provider_source"] == "default"
    assert result["model_source"] == "unset"


def test_an_unset_setting_adds_neither_routing_nor_a_label():
    """No routing at all (the key resolved to its safe default) leaves every
    source name exactly as it was."""
    for routing in (None, {}):
        result = _resolve(
            registry=_registry(),
            environ={},
            layer_routing=routing,
            layer_routing_source="team",
        )

        assert result["provider_source"] == "default"
        assert result["model_source"] == "unset"


# ---------------------------------------------------------------------------
# Which layer is named, and what the layer may supply.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layer", ["org", "team", "team_repo"])
def test_the_label_names_the_layer_that_supplied_the_value(layer):
    result = _resolve(
        registry=_registry(),
        environ={},
        layer_routing=ROUTING,
        layer_routing_source=layer,
    )

    assert result["provider_source"] == f"settings:{layer}"
    assert result["model_source"] == f"settings:{layer}"


def test_the_plan_beats_the_layer_and_keeps_its_own_label():
    """The plan's role_config outranks the settings layer, so the layer does
    not get to name the source."""
    result = _resolve(
        registry=_registry(),
        environ={},
        plan_role_config={"review": {"provider": "claude"}},
        layer_routing=ROUTING,
        layer_routing_source="team",
        model_fallback="sonnet",
    )

    assert result["provider"] == "claude"
    assert result["provider_source"] == "plan_role_config"
    assert result["model_source"] == "caller_fallback"


def test_a_layer_model_is_not_honoured_for_another_provider():
    """The layer's model belongs to the layer's own provider: with the plan
    pinning a different provider, the layer supplies no model."""
    result = _resolve(
        registry=_registry(),
        environ={},
        plan_role_config={"review": {"provider": "claude"}},
        layer_routing=ROUTING,
        layer_routing_source="team",
        model_fallback="sonnet",
    )

    assert result["model"] == "sonnet"
    assert result["model_source"] == "caller_fallback"


def test_a_layer_entry_without_a_provider_routes_nothing():
    """A per-role entry that carries no provider supplies neither the
    provider nor a label for it."""
    result = _resolve(
        registry=_registry(),
        environ={},
        layer_routing={"review": {}},
        layer_routing_source="team",
    )

    assert result["provider_source"] == "default"
    assert result["model_source"] == "unset"


def test_a_layer_that_supplies_only_the_model_names_the_layer_for_it():
    """The layer's model alone is honoured (its provider is unspecified), and
    the model source names the layer."""
    result = _resolve(
        registry=_registry(),
        environ={},
        layer_routing={"review": {"model": "sonnet"}},
        layer_routing_source="team",
    )

    assert result["provider"] == "claude"
    assert result["provider_source"] == "default"
    assert result["model"] == "claude-sonnet-5-5"
    assert result["model_source"] == "settings:team"


def test_a_layer_that_supplies_only_the_provider_names_the_layer_for_it():
    """A provider with no model anywhere is a reportable diagnostic state:
    the provider source still names the layer."""
    result = _resolve(
        registry=_registry(),
        environ={},
        layer_routing={"review": {"provider": "ollama"}},
        layer_routing_source="team",
    )

    assert result["provider"] == "ollama"
    assert result["provider_source"] == "settings:team"
    assert result["model"] is None
    assert result["model_source"] == "unset"
    assert result["error"]


def test_an_undeclared_layer_model_is_reported_as_an_error():
    """A layer that names a model the provider does not declare resolves to
    the pre-existing error shape - no sources, no crash."""
    result = _resolve(
        registry=_registry(),
        environ={},
        layer_routing={"review": {"provider": "ollama", "model": "not-declared"}},
        layer_routing_source="team",
    )

    assert result["error"]
    assert result["provider"] is None
    assert result["model"] is None
    assert result["provider_source"] is None
    assert result["model_source"] is None


# ---------------------------------------------------------------------------
# The whole-config seam: the same labelling for every role at once.
# ---------------------------------------------------------------------------


def test_the_whole_config_view_labels_the_settings_layer():
    review = _review_entry(_setting(ROUTING, "team"))

    assert (review["provider"], review["model"]) == ("ollama", "gpt-oss:20b")
    assert review["provider_source"] == "settings:team"
    assert review["model_source"] == "settings:team"


def test_the_whole_config_view_keeps_the_old_labels_without_a_setting():
    """An unset setting (no resolution, or one carrying no value) labels
    nothing."""
    for setting in ({}, _setting({}, "safe_default")):
        review = _review_entry(setting)

        assert review["provider_source"] == "default"
        assert review["model_source"] == "unset"


def test_the_operator_view_without_a_plan_names_no_layer(store):
    """Without a plan there is no settings scope: no source is named."""
    store({"role_routing": ROUTING})

    result = ci._get_effective_config_impl()
    sources = {
        entry["provider_source"]
        for entry in result["roles"]
        if isinstance(entry["provider_source"], str)
    }

    assert not any(source.startswith("settings:") for source in sources)
    assert "settings" not in sources


def test_the_reported_provider_is_the_one_that_would_actually_be_used():
    """Reporting only: the values reported are the ones the canonical
    resolver returns for the same inputs."""
    from app import role_registry

    result = _resolve(
        registry=_registry(),
        environ={},
        layer_routing=ROUTING,
        layer_routing_source="team",
    )
    expected = role_registry.resolve_role(
        "review",
        registry=_registry(),
        environ={},
        layer_routing=ROUTING,
    )

    assert (result["provider"], result["model"]) == (
        expected.provider,
        expected.model,
    )
