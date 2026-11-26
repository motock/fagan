"""A credentials plugin spec that disappears between two reads must fail closed.

``credential_provider`` reads ``PIPELINE_CREDENTIALS_PLUGIN`` once to decide a
plugin is selected and again inside ``load_plugin``. When the environment
mapping is the live process environment, the second read can see the variable
gone. The loader then returns no factory, and that must surface as a
``PluginError`` rather than a ``TypeError`` from calling ``None``.
"""

from __future__ import annotations

import pytest

import app.credentials
from app.credentials import credential_provider
from app.plugin_loader import PluginError, plugin_env_name

_SPEC_ENV = plugin_env_name("credentials")


class _SpecVanishesOnSecondRead:
    """Reports the plugin spec on the first read of its key, then reports nothing."""

    def __init__(self, spec: str) -> None:
        self._spec = spec
        self._reads = 0

    def get(self, key, default=None):
        if key != _SPEC_ENV:
            return default
        self._reads += 1
        return self._spec if self._reads == 1 else default


@pytest.fixture(autouse=True)
def _clear_provider_cache():
    app.credentials._PLUGIN_PROVIDERS.clear()
    yield
    app.credentials._PLUGIN_PROVIDERS.clear()


def test_should_refuse_credentials_when_the_plugin_spec_vanishes_between_reads():
    environ = _SpecVanishesOnSecondRead("ohc09_no_such_module:factory")

    with pytest.raises(PluginError):
        credential_provider(environ)


def test_should_cache_nothing_when_the_plugin_spec_vanishes_between_reads():
    environ = _SpecVanishesOnSecondRead("ohc09_no_such_module:factory")

    with pytest.raises(PluginError):
        credential_provider(environ)

    assert app.credentials._PLUGIN_PROVIDERS == {}
