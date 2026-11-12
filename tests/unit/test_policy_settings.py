"""Tests for pipeline.policy_settings — the read-only org/team policy seam.

The seam is operator-selected code, so it must be off unless the environment
selects it, fail closed on a provider error (never reporting it as "no policy"),
and log a provider failure by exception type only.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

from app.plugin_loader import PluginError
from pipeline.policy_settings import (
    SETTINGS_ERROR_KEY,
    SETTINGS_PROVIDER_ERROR,
    policy_settings,
    reset_policy_settings_cache,
)

ENV = "PIPELINE_SETTINGS_PLUGIN"
FAKE_MODULE = "tsc_fake_settings"
SPEC = f"{FAKE_MODULE}:build"
MARKER = {SETTINGS_ERROR_KEY: SETTINGS_PROVIDER_ERROR}
REFERENCE = Path(__file__).resolve().parents[2] / "REFERENCE.md"


class _FakeProvider:
    def __init__(self, answer=None, exc=None):
        self.answer = answer
        self.exc = exc
        self.calls = []

    def policy_settings(self, namespace, team):
        self.calls.append((namespace, team))
        if self.exc is not None:
            raise self.exc
        return self.answer


@pytest.fixture(autouse=True)
def fake_module(monkeypatch):
    reset_policy_settings_cache()
    monkeypatch.delenv(ENV, raising=False)
    module = types.ModuleType(FAKE_MODULE)
    monkeypatch.setitem(sys.modules, FAKE_MODULE, module)
    yield module
    reset_policy_settings_cache()


def _factory(build):
    # The loader only accepts attributes owned by the plugin module itself.
    build.__module__ = FAKE_MODULE
    return build


def _select(monkeypatch, fake, provider):
    fake.build = _factory(lambda: provider)
    monkeypatch.setenv(ENV, SPEC)


def _warnings(caplog):
    return [record for record in caplog.records if record.levelno == logging.WARNING]


def test_should_return_empty_dict_when_no_plugin_configured():
    assert policy_settings("default", "alpha") == {}


def test_should_return_org_and_team_layers_from_provider(monkeypatch, fake_module):
    answer = {
        "org": {"test_scope": "full", "allow_team_loosen": False},
        "team": {"test_scope": "scoped"},
    }
    _select(monkeypatch, fake_module, _FakeProvider(answer=answer))

    assert policy_settings("default", "alpha") == answer


def test_should_pass_namespace_and_team_to_provider(monkeypatch, fake_module):
    provider = _FakeProvider(answer={})
    _select(monkeypatch, fake_module, provider)

    policy_settings("default", "alpha")

    assert provider.calls == [("default", "alpha")]


def test_should_ignore_unknown_top_level_keys(monkeypatch, fake_module):
    answer = {"org": {"test_scope": "full"}, "secrets": {"token": "x"}, "other": 1}
    _select(monkeypatch, fake_module, _FakeProvider(answer=answer))

    assert policy_settings("default", None) == {"org": {"test_scope": "full"}}


def test_should_treat_empty_provider_answer_as_no_layers_not_error(
    monkeypatch, fake_module, caplog
):
    _select(monkeypatch, fake_module, _FakeProvider(answer={}))

    result = policy_settings("default", "alpha")

    assert result == {}
    assert SETTINGS_ERROR_KEY not in result
    assert _warnings(caplog) == []


def test_should_return_error_marker_and_warn_type_only_when_provider_raises(
    monkeypatch, fake_module, caplog
):
    _select(
        monkeypatch,
        fake_module,
        _FakeProvider(exc=RuntimeError("secret-token-123")),
    )

    result = policy_settings("default", "alpha")

    assert result == MARKER
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].error_type == "RuntimeError"
    assert "secret-token-123" not in str(warnings[0].__dict__)


@pytest.mark.parametrize(
    "answer",
    [["x"], None, "str", 3],
    ids=["list", "none", "str", "int"],
)
def test_should_return_error_marker_on_non_dict_result(
    monkeypatch, fake_module, caplog, answer
):
    _select(monkeypatch, fake_module, _FakeProvider(answer=answer))

    result = policy_settings("default", "alpha")

    assert result == MARKER
    assert _warnings(caplog)[0].error_type == "non_dict_result"


@pytest.mark.parametrize(
    "answer",
    [{"org": "full"}, {"team": ["scoped"]}, {"org": {"test_scope": "full"}, "team": None}],
    ids=["org_str", "team_list", "team_none"],
)
def test_should_return_error_marker_on_wrong_layer_types(
    monkeypatch, fake_module, caplog, answer
):
    _select(monkeypatch, fake_module, _FakeProvider(answer=answer))

    result = policy_settings("default", "alpha")

    assert result == MARKER
    assert _warnings(caplog)[0].error_type == "non_dict_layer"


def test_should_not_return_marker_when_no_plugin_configured(caplog):
    result = policy_settings("default", "alpha")

    assert SETTINGS_ERROR_KEY not in result
    assert _warnings(caplog) == []


def test_should_return_a_fresh_marker_each_call(monkeypatch, fake_module):
    _select(monkeypatch, fake_module, _FakeProvider(exc=RuntimeError("boom")))

    first = policy_settings("default", "alpha")
    first["org"] = {"test_scope": "full"}
    second = policy_settings("default", "alpha")

    assert second == MARKER


def test_should_raise_plugin_error_for_malformed_spec(monkeypatch):
    monkeypatch.setenv(ENV, "not a spec")

    with pytest.raises(PluginError):
        policy_settings("default", "alpha")


def test_should_raise_plugin_error_when_provider_lacks_policy_settings_method(
    monkeypatch, fake_module
):
    _select(monkeypatch, fake_module, types.SimpleNamespace())

    with pytest.raises(PluginError, match="policy_settings"):
        policy_settings("default", "alpha")


def test_should_build_provider_once_across_calls(monkeypatch, fake_module):
    built = []
    provider = _FakeProvider(answer={})

    def build():
        built.append(provider)
        return provider

    fake_module.build = _factory(build)
    monkeypatch.setenv(ENV, SPEC)

    policy_settings("default", "alpha")
    policy_settings("default", "beta")

    assert len(built) == 1


def test_should_return_a_copy_not_the_providers_dict(monkeypatch, fake_module):
    provider = _FakeProvider(answer={"org": {"test_scope": "full"}})
    _select(monkeypatch, fake_module, provider)

    policy_settings("default", "alpha")["org"]["test_scope"] = "scoped"

    assert policy_settings("default", "alpha") == {"org": {"test_scope": "full"}}


def test_should_document_settings_plugin_in_reference():
    text = REFERENCE.read_text(encoding="utf-8")

    assert "| Policy settings | `PIPELINE_SETTINGS_PLUGIN`" in text
    assert "| `PIPELINE_SETTINGS_PLUGIN` | *(unset → off)*" in text
