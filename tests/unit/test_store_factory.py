"""Store selection via ``PIPELINE_STORE`` (story ST-01).

Contract under test:

* ``PIPELINE_STORE`` unset, or exactly ``file`` -> today's ``FileStore``.
* Any other value -- including the empty string -- raises ``ValueError`` naming
  the variable and its accepted values. The selection fails closed, and the
  message never echoes the offending value (it could be a path or a secret).
* Selection happens *before* any filesystem work: a rejected configuration must
  not create or touch the plan directory.
* Nothing is cached: a rejected call must not poison later calls, and an
  explicit empty mapping passed by the caller means "unset" regardless of the
  process environment.
"""

from __future__ import annotations

import pytest

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server  # noqa: F401
from pipeline import store

STORE_ENV = "PIPELINE_STORE"


def _build_store(environ=None):
    """Call ``pipeline.store.build_store`` so a missing definition fails loud.

    Routing the lookup through ``getattr`` keeps the failure an ``AssertionError``
    on the contract rather than an ``ImportError`` raised at collection time.
    """
    fn = getattr(store, "build_store", None)
    assert fn is not None, "pipeline.store.build_store is not defined"
    if environ is None:
        return fn()
    return fn(environ)


@pytest.fixture(autouse=True)
def _isolated_store_env(monkeypatch):
    """Start every test from "the variable is not set"."""
    monkeypatch.delenv(STORE_ENV, raising=False)


def test_unset_selects_file_store():
    """An empty mapping means unset, and unset keeps today's FileStore."""
    assert type(_build_store({})) is store.FileStore


def test_explicit_file_selects_file_store():
    """``file`` names the only supported store."""
    assert type(_build_store({STORE_ENV: "file"})) is store.FileStore


@pytest.mark.parametrize("rejected", ["sqlite", "postgres"])
def test_unknown_value_raises_value_error_naming_the_variable(rejected):
    """Anything else fails closed, naming the variable and allowed values.

    The rejected value itself must not be echoed -- it may be a path or a
    secret.
    """
    with pytest.raises(ValueError) as excinfo:
        _build_store({STORE_ENV: rejected})

    message = str(excinfo.value)
    assert STORE_ENV in message
    assert "file" in message
    assert rejected not in message


@pytest.mark.parametrize("blank", ["", " ", "  "])
def test_blank_value_is_rejected_not_treated_as_unset(blank):
    """Only an absent key or exactly ``file`` is accepted.

    No strip/lower/empty fallback: a blank setting must not silently degrade to
    the default store.
    """
    with pytest.raises(ValueError) as excinfo:
        _build_store({STORE_ENV: blank})

    assert STORE_ENV in str(excinfo.value)


def test_explicit_mapping_wins_over_the_process_environment(monkeypatch):
    """A caller-supplied empty mapping means "unset" even when the env is set."""
    monkeypatch.setenv(STORE_ENV, "sqlite")

    assert type(_build_store({})) is store.FileStore


def test_environment_is_read_when_no_mapping_is_given(monkeypatch):
    """``build_store()`` with no argument reads the live process environment."""
    monkeypatch.setenv(STORE_ENV, "file")

    fn = getattr(store, "build_store", None)
    assert fn is not None, "pipeline.store.build_store is not defined"
    assert type(fn()) is store.FileStore


def test_rejected_selection_does_not_poison_later_calls():
    """Selection is never memoised: call 2 must not replay call 1's error."""
    with pytest.raises(ValueError):
        _build_store({STORE_ENV: "bogus"})

    assert type(_build_store({})) is store.FileStore
    assert type(_build_store({STORE_ENV: "file"})) is store.FileStore


# ---------------------------------------------------------------------------
# observation vs enforcement: reported_store_kind degrades, build_store raises
# ---------------------------------------------------------------------------
def _reported_kind(environ=None):
    """Call ``reported_store_kind`` so a missing definition fails on an assertion."""
    fn = getattr(store, "reported_store_kind", None)
    assert fn is not None, "pipeline.store.reported_store_kind is not defined"
    return fn(environ)


def test_reported_kind_reports_the_selected_kind():
    assert _reported_kind({}) == "file"
    assert _reported_kind({STORE_ENV: "file"}) == "file"


@pytest.mark.parametrize("rejected", ["sqlite", "", "SQLITE"])
def test_reported_kind_degrades_an_unsupported_selection(rejected):
    """Observation never raises, and never echoes the rejected value."""
    marker = getattr(store, "STORE_INVALID", None)
    assert marker is not None, "pipeline.store.STORE_INVALID is not defined"

    assert _reported_kind({STORE_ENV: rejected}) == marker
    assert marker == "invalid"


def test_enforcement_still_raises_for_the_value_observation_tolerates():
    """The marker belongs to the report only: ``build_store`` stays fail-closed."""
    with pytest.raises(ValueError):
        _build_store({STORE_ENV: "sqlite"})
