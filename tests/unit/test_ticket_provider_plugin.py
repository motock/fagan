"""Tests for the operator-selected ticket-provider plugin seam.

The ticket provider is one of the architectural seams selected ONLY by an
operator-controlled environment variable: ``PIPELINE_TICKET_PROVIDER_PLUGIN``
(seam ``ticket_provider``). A bad spec must raise rather than silently fall
back to the built-in provider, and combining the plugin with an explicit
``PIPELINE_TICKET_PROVIDER=plane|none|jira`` is a configuration conflict.
"""

from __future__ import annotations

import sys
import types

import pytest

from app.plugin_loader import PluginError
from pipeline import ticketing as ticketing_module
from pipeline.ticketing import NullTicketProvider, get_ticket_provider

PLUGIN_ENV = "PIPELINE_TICKET_PROVIDER_PLUGIN"
SELECTOR_ENV = "PIPELINE_TICKET_PROVIDER"
PLANE_ENVS = ("PLANE_API_KEY", "PLANE_WORKSPACE", "PLANE_PROJECT")


class FakeTicketProvider:
    """A minimal provider satisfying the TicketProvider protocol."""

    REQUIRED = ("create_epic", "create_story", "set_state", "resolve_key")

    def __init__(self, methods: tuple[str, ...] = REQUIRED):
        self.calls: list[tuple[str, tuple, dict]] = []
        for name in methods:
            setattr(self, name, self._recorder(name))

    def _recorder(self, name: str):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return f"{name}-result"

        return record


class PluginHarness:
    """The fake plugin module plus the record of how its factory was called."""

    MODULE_NAME = "fake_ticket_plugin"

    def __init__(self, monkeypatch: pytest.MonkeyPatch, provider_factory=None):
        self.factory_calls: list[tuple[tuple, dict]] = []
        self.provider = FakeTicketProvider() if provider_factory is None else None
        module = types.ModuleType(self.MODULE_NAME)
        provider_factory = provider_factory or (lambda: self.provider)
        provider_factory.__module__ = self.MODULE_NAME
        module.build_ticket_provider = self._factory(provider_factory)
        monkeypatch.setitem(sys.modules, self.MODULE_NAME, module)
        monkeypatch.setenv(PLUGIN_ENV, f"{self.MODULE_NAME}:build_ticket_provider")
        self.monkeypatch = monkeypatch

    def _factory(self, provider_factory):
        def build_ticket_provider(*args, **kwargs):
            self.factory_calls.append((args, kwargs))
            return provider_factory()

        build_ticket_provider.__module__ = self.MODULE_NAME
        return build_ticket_provider

    @property
    def spec(self) -> str:
        return f"{self.MODULE_NAME}:build_ticket_provider"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from "no ticket provider configured"."""
    for name in (PLUGIN_ENV, SELECTOR_ENV, PLUGIN_ENV + "S", *PLANE_ENVS):
        monkeypatch.delenv(name, raising=False)


def test_no_plugin_with_selector_none_returns_null_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the built-in selector keeps working when no plugin is set."""
    monkeypatch.setenv(SELECTOR_ENV, "none")

    assert isinstance(get_ticket_provider(), NullTicketProvider)


def test_plugin_factory_selects_the_provider_and_is_called_without_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = PluginHarness(monkeypatch)

    provider = get_ticket_provider()

    assert provider is harness.provider
    assert harness.factory_calls == [((), {})]


def test_plugin_wins_when_selector_is_unset_even_with_plane_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in PLANE_ENVS:
        monkeypatch.setenv(name, "configured")
    monkeypatch.setattr(ticketing_module, "PLANE_API_KEY", "key")
    monkeypatch.setattr(ticketing_module, "PLANE_WORKSPACE", "workspace")
    monkeypatch.setattr(ticketing_module, "PLANE_PROJECT", "project")
    harness = PluginHarness(monkeypatch)

    provider = get_ticket_provider()

    assert provider is harness.provider
    assert not isinstance(provider, ticketing_module.PlaneTicketProvider)


def test_plugin_wins_when_selector_is_auto_even_with_plane_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in PLANE_ENVS:
        monkeypatch.setenv(name, "configured")
    monkeypatch.setenv(SELECTOR_ENV, "auto")
    monkeypatch.setattr(ticketing_module, "PLANE_API_KEY", "key")
    monkeypatch.setattr(ticketing_module, "PLANE_WORKSPACE", "workspace")
    monkeypatch.setattr(ticketing_module, "PLANE_PROJECT", "project")
    harness = PluginHarness(monkeypatch)

    provider = get_ticket_provider()

    assert provider is harness.provider


def test_blank_selector_is_treated_as_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SELECTOR_ENV, "   ")
    harness = PluginHarness(monkeypatch)

    assert get_ticket_provider() is harness.provider


@pytest.mark.parametrize("selector", ["plane", "none", "jira"])
def test_explicit_selector_conflicting_with_plugin_raises(
    monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    monkeypatch.setenv(SELECTOR_ENV, selector)
    harness = PluginHarness(monkeypatch)

    with pytest.raises(PluginError, match="conflicts"):
        get_ticket_provider()

    assert harness.factory_calls == []


def test_provider_missing_set_state_raises_naming_the_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = PluginHarness(
        monkeypatch,
        provider_factory=lambda: FakeTicketProvider(
            methods=("create_epic", "create_story", "resolve_key")
        ),
    )

    with pytest.raises(PluginError, match="set_state"):
        get_ticket_provider()

    assert len(harness.factory_calls) == 1


def test_invalid_plugin_spec_raises_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PLUGIN_ENV, "missing.module:build")

    with pytest.raises(PluginError):
        provider = get_ticket_provider()
        assert not isinstance(provider, NullTicketProvider)
