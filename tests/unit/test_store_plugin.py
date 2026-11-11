"""``PIPELINE_STORE=plugin`` loads the record store named by PIPELINE_STORE_PLUGIN.

Contract under test (ADR 0012 decision 7):

* ``PIPELINE_STORE=plugin`` selects the factory named by
  ``PIPELINE_STORE_PLUGIN=module:attr``, loaded through
  ``app.plugin_loader.load_plugin``; the built record store must provide every
  name in ``RECORD_STORE_METHODS``.
* A missing ``PIPELINE_STORE_PLUGIN`` raises ``ValueError`` naming the variable.
* Any loader error raises (fail closed); nothing falls back to the file store.
* ``PIPELINE_STORE_PLUGIN`` alone never selects the plugin.
* The reported kind is ``plugin`` or ``STORE_INVALID`` and never echoes the spec.
"""

from __future__ import annotations

import pytest

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server  # noqa: F401
from app.plugin_loader import PluginError
from pipeline import store
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import RECORD_STORE_METHODS

STORE_ENV = "PIPELINE_STORE"
PLUGIN_ENV = "PIPELINE_STORE_PLUGIN"
GOOD_SPEC = "tests.fixtures.store_plugin:build_record_store"
INCOMPLETE_SPEC = "tests.fixtures.store_plugin:build_incomplete"


@pytest.fixture(autouse=True)
def _isolated_store_env(monkeypatch):
    """Start every test from "neither store variable is set"."""
    monkeypatch.delenv(STORE_ENV, raising=False)
    monkeypatch.delenv(PLUGIN_ENV, raising=False)


def _build(environ):
    return store.build_store(environ)


def test_plugin_factory_is_loaded_and_its_store_is_used():
    built = _build({STORE_ENV: "plugin", PLUGIN_ENV: GOOD_SPEC})

    assert isinstance(built, InMemoryRecordStore)
    built.put_manifest("default", "p", {"stories": {}})
    assert built.get_manifest("default", "p") == {"stories": {}}


def test_plugin_store_provides_every_record_store_method():
    built = _build({STORE_ENV: "plugin", PLUGIN_ENV: GOOD_SPEC})

    missing = [name for name in RECORD_STORE_METHODS if not callable(getattr(built, name, None))]
    assert missing == []


def test_missing_plugin_variable_raises_value_error_naming_it():
    with pytest.raises(ValueError) as excinfo:
        _build({STORE_ENV: "plugin"})

    assert PLUGIN_ENV in str(excinfo.value)


def test_blank_plugin_variable_raises_value_error_naming_it():
    with pytest.raises(ValueError) as excinfo:
        _build({STORE_ENV: "plugin", PLUGIN_ENV: "   "})

    assert PLUGIN_ENV in str(excinfo.value)


def test_malformed_plugin_spec_is_refused_and_not_echoed():
    spec = "/abs/path/to/thing"
    with pytest.raises(ValueError) as excinfo:
        _build({STORE_ENV: "plugin", PLUGIN_ENV: spec})

    assert PLUGIN_ENV in str(excinfo.value)
    assert spec not in str(excinfo.value)


def test_stdlib_module_is_refused_and_never_selects_the_file_store():
    with pytest.raises(PluginError):
        _build({STORE_ENV: "plugin", PLUGIN_ENV: "json:dumps"})


def test_factory_missing_a_record_store_method_is_refused():
    with pytest.raises(PluginError) as excinfo:
        _build({STORE_ENV: "plugin", PLUGIN_ENV: INCOMPLETE_SPEC})

    assert "missing required methods" in str(excinfo.value)


def test_unimportable_plugin_module_raises_rather_than_falling_back():
    with pytest.raises(PluginError):
        _build({STORE_ENV: "plugin", PLUGIN_ENV: "no_such_module_xyz:build"})


def test_plugin_variable_alone_never_selects_the_plugin():
    built = _build({PLUGIN_ENV: GOOD_SPEC})

    assert type(built) is store.FileStore


def test_reported_kind_is_plugin_for_a_well_formed_spec():
    assert store.reported_store_kind({STORE_ENV: "plugin", PLUGIN_ENV: GOOD_SPEC}) == "plugin"


@pytest.mark.parametrize("spec", ["", "/secret/path", "not a spec"])
def test_reported_kind_degrades_a_bad_spec_without_echoing_it(spec):
    reported = store.reported_store_kind({STORE_ENV: "plugin", PLUGIN_ENV: spec})

    assert reported == store.STORE_INVALID
