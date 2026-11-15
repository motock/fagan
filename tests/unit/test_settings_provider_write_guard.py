"""Write guard for the provider-supplied settings layers (ADR 0015).

While ``PIPELINE_SETTINGS_PLUGIN`` selects a provider, the Store must not write
the two layers that provider supplies. The deployment and team_repo layers stay
writable, and with no provider configured every layer stays writable.

The refusal names the provider variable and never the value it was asked to
store. A refusal never reaches the Store.
"""

from __future__ import annotations

import pytest

from pipeline import authorization, persistence, policy_settings, settings_tools
from pipeline import server as p

ENV = "PIPELINE_SETTINGS_PLUGIN"
REPO = "github.com/acme/widgets"
KEY = "merge_ci_timeout_s"
#: A distinctive in-range value, so an echo of it is detectable in a refusal.
VALUE = 7331
PROVIDER_SUPPLIED = [("org", "acme"), ("team", "team-1")]
STORE_SIDE = [("deployment", "", ""), ("team_repo", "team-1", REPO)]


class _Allow:
    def allow(self, principal, action, resource=None):
        return True


class _Untouchable:
    """Fails the test if a refused write reaches the Store."""

    def records(self):
        raise AssertionError("the Store was touched by a refused write")


@pytest.fixture
def plans(tmp_path, monkeypatch):
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    monkeypatch.delenv(authorization.ENFORCE_ENV, raising=False)
    monkeypatch.setattr(authorization, "default_authorizer", lambda: _Allow())
    return directory


@pytest.fixture
def provider_configured(monkeypatch):
    monkeypatch.setattr(settings_tools, "provider_configured", lambda: True)


def _set(scope, scope_id, repo="", value=VALUE):
    return settings_tools.set_setting(
        scope=scope,
        scope_id=scope_id,
        repo=repo,
        key=KEY,
        value=value,
        expected_version=None,
    )


# --- refusal while a provider is configured ---------------------------------


@pytest.mark.parametrize(("scope", "scope_id"), PROVIDER_SUPPLIED)
def test_provider_supplied_layer_refusal_names_the_variable(
    plans, provider_configured, scope, scope_id
):
    result = _set(scope, scope_id)

    assert result["ok"] is False
    assert result["reason"] == "settings_plugin"
    assert ENV in result["error"]


@pytest.mark.parametrize(("scope", "scope_id"), PROVIDER_SUPPLIED)
def test_provider_supplied_layer_refusal_does_not_echo_the_value(
    plans, provider_configured, scope, scope_id
):
    result = _set(scope, scope_id)

    assert str(VALUE) not in repr(result)


@pytest.mark.parametrize(("scope", "scope_id"), PROVIDER_SUPPLIED)
def test_provider_supplied_layer_refusal_does_not_touch_the_store(
    plans, provider_configured, monkeypatch, scope, scope_id
):
    monkeypatch.setattr(settings_tools, "_store", _Untouchable())

    result = _set(scope, scope_id)

    assert result["ok"] is False


def test_a_failing_probe_refuses_a_provider_supplied_layer(plans, monkeypatch):
    def broken():
        raise RuntimeError("provider probe failed")

    monkeypatch.setattr(settings_tools, "provider_configured", broken)

    result = _set("org", "acme")

    assert result["ok"] is False
    assert result["reason"] == "settings_plugin"


# --- the store-side layers stay writable ------------------------------------


@pytest.mark.parametrize(("scope", "scope_id", "repo"), STORE_SIDE)
def test_store_side_layers_are_writable_while_a_provider_is_configured(
    plans, provider_configured, scope, scope_id, repo
):
    result = _set(scope, scope_id, repo)

    assert result["ok"] is True
    assert result["new_version"] == 1


def test_store_side_writes_are_unaffected_by_a_failing_probe(plans, monkeypatch):
    def broken():
        raise RuntimeError("provider probe failed")

    monkeypatch.setattr(settings_tools, "provider_configured", broken)

    assert _set("deployment", "")["ok"] is True


# --- no provider configured: the unchanged path -----------------------------


@pytest.fixture
def no_provider(monkeypatch):
    """Use the real probe with the provider variable unset."""
    monkeypatch.delenv(ENV, raising=False)
    policy_settings.reset_policy_settings_cache()
    yield
    policy_settings.reset_policy_settings_cache()


@pytest.mark.parametrize(
    ("scope", "scope_id", "repo"),
    [(scope, scope_id, "") for scope, scope_id in PROVIDER_SUPPLIED] + STORE_SIDE,
)
def test_every_layer_is_writable_when_no_provider_is_configured(
    plans, no_provider, scope, scope_id, repo
):
    assert settings_tools.provider_configured() is False

    result = _set(scope, scope_id, repo)

    assert result["ok"] is True


def test_unconfigured_write_returns_the_unchanged_success_shape(plans, no_provider):
    result = _set("org", "acme")

    assert result == {
        "ok": True,
        "scope": "org",
        "scope_id": "acme",
        "repo": "",
        "key": KEY,
        "value": VALUE,
        "new_version": 1,
    }
