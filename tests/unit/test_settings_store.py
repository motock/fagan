"""Effective tenant settings read from the Store (ts-05).

The Store is a real FileStore under ``tmp_path``; the policy provider is a real
``PIPELINE_SETTINGS_PLUGIN`` module loaded through the plugin loader. Only the
external boundaries are faked. The resolver runs for real.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from pipeline import persistence, server
from pipeline.policy_settings import reset_policy_settings_cache
from pipeline.settings_registry import REGISTRY
from pipeline.settings_store import effective_setting, effective_settings

NAMESPACE = "default"
REPO = "github.com/acme/app"
ENV = "PIPELINE_SETTINGS_PLUGIN"
FAKE_MODULE = "ts05_fake_settings"
SPEC = f"{FAKE_MODULE}:build"
LOGGER = "pipeline.settings_store"


@pytest.fixture(autouse=True)
def plan_dir(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(persistence, "PLAN_DIR", plans)
    monkeypatch.delenv(ENV, raising=False)
    reset_policy_settings_cache()
    yield plans
    reset_policy_settings_cache()


@pytest.fixture
def records():
    return server._store.records()


def _put(records, scope, scope_id="", repo="", values=None, allow_loosen=None):
    return records.put_settings(
        NAMESPACE,
        scope,
        scope_id,
        repo,
        values or {},
        allow_loosen or [],
        "ops",
        None,
    )


def _ask(key, team="alpha", repo=REPO, **kwargs):
    return effective_setting(key, namespace=NAMESPACE, team=team, repo=repo, **kwargs)


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and r.name == LOGGER]


# --- layers from the Store ---


def test_should_apply_deployment_record_when_no_narrower_layer_is_set(records):
    _put(records, "deployment", values={"merge_ci_timeout_s": 600})

    result = _ask("merge_ci_timeout_s")

    assert (result["value"], result["source"]) == (600, "deployment")


def test_should_return_safe_default_when_no_layer_is_set():
    result = _ask("merge_ci_timeout_s")

    assert (result["value"], result["source"]) == (900, "safe_default")


def test_should_tighten_ceiling_from_team_repo_record(records):
    _put(records, "deployment", values={"test_scope": "scoped"})
    _put(records, "team_repo", "alpha", REPO, values={"test_scope": "full"})

    result = _ask("test_scope")

    assert (result["value"], result["source"]) == ("full", "team_repo")
    assert result["lock_rejections"] == []


def test_should_refuse_team_repo_loosening_a_deployment_ceiling(records):
    _put(records, "deployment", values={"merge_ci_gate": True})
    _put(records, "team_repo", "alpha", REPO, values={"merge_ci_gate": False})

    result = _ask("merge_ci_gate")

    assert (result["value"], result["source"]) == (True, "deployment")
    assert result["lock_rejections"] == [
        {"layer": "team_repo", "requested": False, "by": "deployment"}
    ]


def test_should_accept_loosening_only_when_the_setting_layer_allows_it(records):
    _put(
        records,
        "deployment",
        values={"merge_ci_gate": True},
        allow_loosen=["merge_ci_gate"],
    )
    _put(records, "team_repo", "alpha", REPO, values={"merge_ci_gate": False})

    result = _ask("merge_ci_gate")

    assert (result["value"], result["source"]) == (False, "team_repo")
    assert result["lock_rejections"] == []


def test_should_resolve_two_teams_on_one_repo_differently(records):
    _put(records, "team_repo", "alpha", REPO, values={"merge_ci_timeout_s": 300})
    _put(records, "team_repo", "beta", REPO, values={"merge_ci_timeout_s": 1200})

    alpha = _ask("merge_ci_timeout_s", team="alpha")
    beta = _ask("merge_ci_timeout_s", team="beta")

    assert (alpha["value"], beta["value"]) == (300, 1200)


def test_should_not_apply_a_team_repo_record_to_another_repo(records):
    _put(records, "team_repo", "alpha", "github.com/acme/other", values={"test_scope": "scoped"})

    result = _ask("test_scope", repo=REPO)

    assert (result["value"], result["source"]) == ("full", "safe_default")


def test_should_apply_team_record_for_the_caller_team_only(records):
    _put(records, "team", "alpha", values={"rework_on_ci_fail": True})

    assert _ask("rework_on_ci_fail", team="alpha")["source"] == "team"
    assert _ask("rework_on_ci_fail", team="beta")["source"] == "safe_default"


def test_should_apply_org_record_when_the_plan_names_an_org(records):
    _put(records, "org", "acme", values={"grade_xdist_workers": 2})

    result = _ask("grade_xdist_workers", org="acme")

    assert (result["value"], result["source"]) == (2, "org")


def test_should_ignore_org_record_when_the_plan_names_no_org(records):
    _put(records, "org", "acme", values={"grade_xdist_workers": 2})

    result = _ask("grade_xdist_workers")

    assert (result["value"], result["source"]) == (1, "safe_default")


def test_should_echo_team_and_repo_in_the_result():
    result = _ask("merge_ci_gate")

    assert (result["team"], result["repo"]) == ("alpha", REPO)


def test_should_reject_unknown_key_with_key_error():
    with pytest.raises(KeyError):
        _ask("no_such_setting")


def test_should_return_every_registry_key_from_effective_settings(records):
    _put(records, "deployment", values={"merge_ci_timeout_s": 600})

    settings = effective_settings(NAMESPACE, "alpha", REPO)

    assert set(settings) == set(REGISTRY)
    assert settings["merge_ci_timeout_s"]["value"] == 600


# --- Store read failures never raise, and fail to the safe value ---


def test_should_return_safe_values_when_store_read_raises(monkeypatch, caplog):
    def _boom(*args, **kwargs):
        raise RuntimeError("/secret/path with value 42")

    monkeypatch.setattr(server._store.records().__class__, "get_settings", _boom)
    caplog.set_level(logging.WARNING, logger=LOGGER)

    result = _ask("merge_ci_timeout_s")

    assert (result["value"], result["source"]) == (900, "settings_error")
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert getattr(warnings[0], "error_type", None) == "RuntimeError"
    assert "secret" not in warnings[0].getMessage()
    assert "42" not in warnings[0].getMessage()


def test_should_log_one_warning_for_all_keys_when_store_read_raises(monkeypatch, caplog):
    def _boom(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(server._store.records().__class__, "get_settings", _boom)
    caplog.set_level(logging.WARNING, logger=LOGGER)

    settings = effective_settings(NAMESPACE, "alpha", REPO)

    assert {entry["source"] for entry in settings.values()} == {"settings_error"}
    assert len(_warnings(caplog)) == 1


def test_should_return_settings_error_for_an_invalid_team_key(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)

    result = _ask("merge_ci_gate", team="Not A Team!")

    assert (result["value"], result["source"]) == (True, "settings_error")
    assert len(_warnings(caplog)) == 1


# --- PIPELINE_SETTINGS_PLUGIN supplies the org and team layers ---


class _Provider:
    def __init__(self, answer=None, exc=None):
        self.answer = answer
        self.exc = exc

    def policy_settings(self, namespace, team):
        if self.exc is not None:
            raise self.exc
        return self.answer


@pytest.fixture
def provider(monkeypatch):
    module = types.ModuleType(FAKE_MODULE)
    monkeypatch.setitem(sys.modules, FAKE_MODULE, module)
    monkeypatch.setenv(ENV, SPEC)

    def _select(answer=None, exc=None):
        instance = _Provider(answer=answer, exc=exc)

        def build():
            return instance

        build.__module__ = FAKE_MODULE
        module.build = build
        return instance

    return _select


def test_should_take_org_and_team_layers_from_the_provider(provider, records):
    _put(records, "team", "alpha", values={"merge_ci_timeout_s": 300})
    provider(answer={"team": {"merge_ci_timeout_s": 450}, "org": {"merge_ci_gate": False}})

    team = _ask("merge_ci_timeout_s", team="alpha")
    org = _ask("merge_ci_gate", team="alpha")

    assert (team["value"], team["source"]) == (450, "team")
    assert (org["value"], org["source"]) == (False, "org")


def test_should_still_read_deployment_and_team_repo_from_the_store(provider, records):
    _put(records, "team_repo", "alpha", REPO, values={"test_scope": "scoped"})
    provider(answer={"team": {}})

    result = _ask("test_scope")

    assert (result["value"], result["source"]) == ("scoped", "team_repo")


def test_should_resolve_provider_failure_to_safe_values(provider, records, caplog):
    _put(records, "deployment", values={"merge_ci_timeout_s": 600})
    provider(exc=RuntimeError("provider broke"))
    caplog.set_level(logging.WARNING, logger="pipeline.policy_settings")

    result = _ask("merge_ci_timeout_s")

    assert (result["value"], result["source"]) == (900, "settings_error")


def test_should_resolve_provider_load_failure_to_safe_values(monkeypatch, records):
    monkeypatch.setenv(ENV, "no_such_module_ts05:build")

    result = _ask("merge_ci_gate")

    assert (result["value"], result["source"]) == (True, "settings_error")


# --- config view: get_effective_config lists the resolved settings ---


def test_should_list_each_settings_key_in_the_effective_config_view(records):
    from pipeline.config_provenance import settings_entries

    _put(records, "deployment", values={"reverify_full_suite": True})
    resolved = effective_settings(NAMESPACE, "local", "")

    entries = settings_entries(resolved)

    by_name = {entry["name"]: entry for entry in entries}
    assert set(by_name) == set(REGISTRY)
    assert by_name["reverify_full_suite"]["value"] is True
    assert by_name["reverify_full_suite"]["source"] == "deployment"
    assert [entry["name"] for entry in entries] == sorted(REGISTRY)
