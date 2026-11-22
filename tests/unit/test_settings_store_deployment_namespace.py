"""The deployment settings layer is one record, read from the default namespace.

``set_setting`` writes the ``deployment`` layer under the default namespace and
that is the only supported write path. Every team, in every namespace, must
therefore read that one record. Org, team and team-repo records stay in the
caller's namespace.

The record store is the database boundary, so it is a test double: the
production Postgres store holds every namespace, but ``InMemoryRecordStore``
refuses every namespace except the default one. ``_MultiNamespaceStore``
overrides only that namespace check; versioning, validation and copies stay
the real in-memory implementation. The resolver and the settings store run for
real.
"""

from __future__ import annotations

import pytest

from pipeline import server
from pipeline.ci import context_from_manifest
from pipeline.policy_settings import SETTINGS_ERROR_KEY, reset_policy_settings_cache
from pipeline.settings_store import effective_setting, read_layers
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import DEFAULT_NAMESPACE, validate_namespace

TENANT = "acme/web"
OTHER = "other/team"
REPO_A = "github.com/acme/app-a"
ENV = "PIPELINE_SETTINGS_PLUGIN"


class _MultiNamespaceStore(InMemoryRecordStore):
    """An in-memory record store that holds every valid namespace."""

    def _draft_namespace(self, namespace: str) -> None:
        validate_namespace(namespace)


class _DefaultReadFailsStore(_MultiNamespaceStore):
    """A record store whose settings read of the default namespace raises."""

    def get_settings(self, namespace, scope, scope_id, repo):
        if namespace == DEFAULT_NAMESPACE:
            raise RuntimeError("store unavailable")
        return super().get_settings(namespace, scope, scope_id, repo)


class _Store:
    """A store stand-in whose record view is one record store."""

    def __init__(self, records: InMemoryRecordStore) -> None:
        self._records = records

    def records(self) -> InMemoryRecordStore:
        return self._records


@pytest.fixture(autouse=True)
def _no_settings_plugin(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    reset_policy_settings_cache()
    yield
    reset_policy_settings_cache()


@pytest.fixture
def records(monkeypatch) -> InMemoryRecordStore:
    store = _MultiNamespaceStore()
    monkeypatch.setattr(server, "_store", _Store(store))
    return store


def _put(records, namespace, scope, scope_id="", repo="", values=None):
    return records.put_settings(
        namespace,
        scope,
        scope_id,
        repo,
        values or {},
        [],
        "ops",
        None,
    )


def _ask(key, namespace=TENANT, team="web", repo="", org="acme"):
    return effective_setting(key, namespace=namespace, team=team, repo=repo, org=org)


# --- the deployment record is read for a tenant namespace ---


def test_should_apply_default_namespace_deployment_record_to_tenant_team(records):
    _put(records, DEFAULT_NAMESPACE, "deployment", values={"test_scope": "scoped"})

    result = _ask("test_scope")

    assert (result["value"], result["source"]) == ("scoped", "deployment")


def test_should_return_deployment_layer_values_for_tenant_namespace(records):
    _put(
        records,
        DEFAULT_NAMESPACE,
        "deployment",
        values={"test_scope": "scoped", "merge_ci_gate": True},
    )

    layers = read_layers(TENANT, team="web", repo="", org="acme")

    assert layers["deployment"]["test_scope"] == "scoped"
    assert layers["deployment"]["merge_ci_gate"] is True


def test_should_still_read_deployment_record_for_default_namespace(records):
    _put(records, DEFAULT_NAMESPACE, "deployment", values={"test_scope": "scoped"})

    result = _ask("test_scope", namespace=DEFAULT_NAMESPACE, org="")

    assert (result["value"], result["source"]) == ("scoped", "deployment")


def test_should_keep_deployment_ceiling_against_tenant_team_loosening(records):
    _put(records, DEFAULT_NAMESPACE, "deployment", values={"merge_ci_gate": True})
    _put(records, TENANT, "team", "web", values={"merge_ci_gate": False})

    result = _ask("merge_ci_gate")

    assert (result["value"], result["source"]) == (True, "deployment")
    assert result["lock_rejections"] == [
        {"layer": "team", "requested": False, "by": "deployment"}
    ]


# --- a tenant namespace cannot supply its own deployment layer ---


def test_should_ignore_deployment_record_written_under_tenant_namespace(records):
    _put(records, TENANT, "deployment", values={"test_scope": "scoped"})

    result = _ask("test_scope")

    assert result["source"] == "safe_default"


def test_should_not_read_tenant_deployment_record_into_layers(records):
    _put(records, TENANT, "deployment", values={"test_scope": "scoped"})

    layers = read_layers(TENANT, team="web", repo="", org="acme")

    assert "deployment" not in layers


# --- team layers stay in their own namespace ---


def test_should_read_team_record_for_its_own_namespace(records):
    _put(records, TENANT, "team", "web", values={"merge_ci_gate": False})

    layers = read_layers(TENANT, team="web", repo="", org="acme")

    assert layers["team"]["merge_ci_gate"] is False


def test_should_not_read_tenant_team_record_for_default_namespace(records):
    _put(records, TENANT, "team", "web", values={"merge_ci_gate": False})

    layers = read_layers(DEFAULT_NAMESPACE, team="web", repo="", org="")

    assert "team" not in layers


def test_should_not_read_tenant_team_record_for_other_namespace(records):
    _put(records, TENANT, "team", "web", values={"merge_ci_gate": False})

    layers = read_layers(OTHER, team="web", repo="", org="")

    assert "team" not in layers


# --- a read failure stays a settings error marker ---


def test_should_return_settings_error_marker_when_default_namespace_read_fails(
    monkeypatch,
):
    monkeypatch.setattr(server, "_store", _Store(_DefaultReadFailsStore()))

    layers = read_layers(TENANT, team="web", repo="", org="acme")

    assert layers == {SETTINGS_ERROR_KEY: "settings_store_read_failed"}


# --- nothing recorded anywhere ---


def test_should_resolve_to_safe_default_when_no_record_exists_anywhere(records):
    result = _ask("test_scope")

    assert (result["value"], result["source"]) == ("full", "safe_default")


# --- the merge-gate context reads the same layer ---


def test_should_apply_default_namespace_deployment_to_manifest_tenant(records):
    _put(records, DEFAULT_NAMESPACE, "deployment", values={"merge_ci_gate": True})
    manifest = {
        "repo_url": f"https://{REPO_A}",
        "namespace": TENANT,
        "team": "web",
    }

    result = context_from_manifest(manifest).resolve("merge_ci_gate")

    assert (result["value"], result["source"]) == (True, "deployment")
