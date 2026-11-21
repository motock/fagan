"""The settings team of a plan comes from its tenancy namespace (stn-01).

With ``PIPELINE_SETTINGS_TEAM_PLUGIN`` unset, the team is the manifest ``team``
(or ``DEFAULT_TEAM``), exactly as before. With the plugin set, the tenancy team
id the plugin names for the plan's namespace decides which team layer applies.
A plugin answer that is not a team slug, or a plugin that raises, falls back to
``DEFAULT_TEAM`` so an error never applies a team's looser settings.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest

from app.plugin_loader import ALLOWLIST_ENV, PluginError
from pipeline import persistence, server, settings_store, settings_team
from pipeline import test_scope_dispatch as tsd
from pipeline.actor import bind_namespace
from pipeline.authorization import DEFAULT_TEAM
from pipeline.ci import context_from_manifest
from pipeline.settings_team import SETTINGS_TEAM_PLUGIN_ENV
from pipeline.settings_team import settings_team as team_of

REPO_URL = "https://github.com/acme/app"
REPO = "github.com/acme/app"
LOGGER = "pipeline.settings_team"

# A stub plugin whose answer is decided by the body the test supplies.
_TEMPLATE = """\
class StubTeams:
    def team_for_namespace(self, namespace):
@@BODY@@


def build():
    return StubTeams()
"""

MAPPING = """
        return {"default": "default", "acme/alpha": "alpha"}.get(namespace)"""

RAISE_SECRET = """
        raise RuntimeError("secret-namespace-acme/alpha")"""

ANSWER_NONE = """
        return None"""

ANSWER_BAD_SLUG = """
        return "Bad Team\""""

ANSWER_EMPTY = """
        return \"\""""

ANSWER_INT = """
        return 42"""

NO_METHOD_STUB = """\
def build():
    return object()
"""


@pytest.fixture(autouse=True)
def _no_plugin_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SETTINGS_TEAM_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    # Each case builds its own plugin object; the per-spec cache must not carry over.
    monkeypatch.setattr(settings_team, "_PLUGIN_TEAMS", {})


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(persistence, "PLAN_DIR", plans)
    return plans


def _install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str | None, source=None
) -> None:
    """Write a stub plugin module, select it, and allowlist it."""
    name = "settings_team_" + uuid.uuid4().hex
    text = source if source is not None else _TEMPLATE.replace("@@BODY@@", body or "")
    (tmp_path / f"{name}.py").write_text(text, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(SETTINGS_TEAM_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name)


def _warnings(caplog):
    return [
        r for r in caplog.records if r.levelno == logging.WARNING and r.name == LOGGER
    ]


# --- no plugin: today's behaviour ---


def test_should_keep_manifest_team_when_no_plugin_configured():
    assert team_of({"namespace": "default", "team": "local"}) == "local"


def test_should_use_default_team_when_manifest_has_no_team():
    assert team_of({"namespace": "default"}) == DEFAULT_TEAM


def test_should_ignore_namespace_when_no_plugin_configured():
    assert team_of({"namespace": "acme/alpha", "team": "beta"}) == "beta"


# --- plugin answers ---


def test_should_use_plugin_team_for_default_namespace(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, MAPPING)

    assert team_of({"namespace": "default", "team": "local"}) == "default"


def test_should_use_plugin_team_for_org_team_namespace(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, MAPPING)

    assert team_of({"namespace": "acme/alpha", "team": "local"}) == "alpha"


def test_should_pass_default_namespace_to_plugin_when_manifest_has_none(
    tmp_path, monkeypatch
):
    _install(tmp_path, monkeypatch, MAPPING)

    assert team_of({"team": "local"}) == "default"


def test_should_ask_plugin_for_bound_namespace_when_manifest_has_none(
    tmp_path, monkeypatch
):
    _install(tmp_path, monkeypatch, MAPPING)

    with bind_namespace("acme/alpha"):
        assert team_of({"team": "local"}) == "alpha"


def test_should_fall_back_to_manifest_team_when_plugin_has_no_opinion(
    tmp_path, monkeypatch
):
    _install(tmp_path, monkeypatch, ANSWER_NONE)

    assert team_of({"namespace": "default", "team": "beta"}) == "beta"


def test_should_fall_back_to_default_team_when_plugin_has_no_opinion_and_no_team(
    tmp_path, monkeypatch
):
    _install(tmp_path, monkeypatch, ANSWER_NONE)

    assert team_of({"namespace": "default"}) == DEFAULT_TEAM


@pytest.mark.parametrize("body", [ANSWER_BAD_SLUG, ANSWER_EMPTY, ANSWER_INT])
def test_should_return_default_team_when_plugin_answer_is_not_a_team_slug(
    tmp_path, monkeypatch, caplog, body
):
    _install(tmp_path, monkeypatch, body)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = team_of({"namespace": "default", "team": "beta"})

    assert result == DEFAULT_TEAM
    assert len(_warnings(caplog)) == 1


def test_should_default_when_plugin_raises_and_log_exception_type_only(
    tmp_path, monkeypatch, caplog
):
    _install(tmp_path, monkeypatch, RAISE_SECRET)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = team_of({"namespace": "acme/alpha", "team": "beta"})

    assert result == DEFAULT_TEAM
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    text = " ".join([warnings[0].getMessage(), *map(str, warnings[0].args)])
    assert "acme/alpha" not in text
    assert "alpha" not in text
    assert "beta" not in text
    assert warnings[0].__dict__.get("error_type") == "RuntimeError"


def test_should_refuse_at_load_a_plugin_missing_team_for_namespace(
    tmp_path, monkeypatch
):
    _install(tmp_path, monkeypatch, None, source=NO_METHOD_STUB)

    with pytest.raises(PluginError, match="team_for_namespace"):
        team_of({"namespace": "default", "team": "local"})


def test_should_refuse_plugin_when_module_allowlist_is_unset(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, MAPPING)
    monkeypatch.delenv(ALLOWLIST_ENV)

    with pytest.raises(PluginError, match=ALLOWLIST_ENV):
        team_of({"namespace": "default", "team": "local"})


# --- the plan context ---


def test_should_build_plan_context_team_from_namespace_plugin(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, MAPPING)

    context = context_from_manifest({"namespace": "default", "team": "local"})

    assert context.team == "default"


def test_should_not_cache_answers_per_namespace(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, MAPPING)

    first = team_of({"namespace": "default", "team": "local"})
    second = team_of({"namespace": "acme/alpha", "team": "local"})

    assert (first, second) == ("default", "alpha")


# --- end to end: the team layer applies to a plan labelled "local" ---


def test_should_apply_team_layer_to_plan_labelled_local_through_namespace(
    tmp_path, monkeypatch, plan_dir
):
    _install(tmp_path, monkeypatch, MAPPING)
    monkeypatch.delenv(tsd.TEST_SCOPE_ENV, raising=False)
    monkeypatch.setattr(settings_store, "provider_configured", lambda: False)
    records = server._store.records()
    records.put_settings(
        "default", "team", "default", "", {"test_scope": "scoped"}, [], "ops", None
    )

    result = tsd.effective_test_scope(
        {"namespace": "default", "team": "local", "repo_url": REPO_URL}
    )

    assert (result["test_scope"], result["source"]) == ("scoped", "team")
