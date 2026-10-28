"""The selected store is reported in the health ``runtime`` block (story ST-01).

``/api/health`` reports runtime selections in a cumulative ``runtime`` block,
kept deliberately separate from ``resolved_topology()`` (whose four documented
keys are pinned by ``tests/unit/test_topology_identity.py`` and must not grow).

Contract under test:

* ``resolved_runtime()["store"]`` is ``"file"`` when ``PIPELINE_STORE`` is
  unset -- the store kind, not the store object.
* ``/api/health`` exposes it under ``runtime["store"]``.
* The report is a pure read: it must not *construct* a store, or every health
  probe would touch the plan directory on disk.

Only the key this change adds is asserted -- never the whole ``runtime`` dict,
never the whole payload, never ``resolved_topology()``.
"""

from __future__ import annotations

import pytest

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server  # noqa: F401
from app import dashboard
from pipeline import store, topology_identity

STORE_ENV = "PIPELINE_STORE"


@pytest.fixture(autouse=True)
def _isolated_store_env(monkeypatch):
    """Start every test from "the variable is not set"."""
    monkeypatch.delenv(STORE_ENV, raising=False)


def _resolved_runtime():
    """Call ``resolved_runtime`` so a missing definition fails on an assertion."""
    fn = getattr(topology_identity, "resolved_runtime", None)
    assert fn is not None, "pipeline.topology_identity.resolved_runtime is not defined"
    return fn()


def test_resolved_runtime_reports_the_store_kind():
    """Unset ``PIPELINE_STORE`` reports the default store kind."""
    assert _resolved_runtime()["store"] == "file"


def test_resolved_runtime_does_not_construct_a_store(monkeypatch):
    """The report is a pure read: constructing a store would touch disk.

    ``FileStore()`` is patched to record its calls and fail, so an implementation
    that reports ``type(build_store()).__name__`` (or any other construction)
    is caught rather than silently creating the plan directory on every probe.
    """
    constructed: list[str] = []

    def factory(*_args, **_kwargs):
        constructed.append("FileStore")
        raise AssertionError("resolved_runtime must not construct a store")

    monkeypatch.setattr(store, "FileStore", factory)

    first = _resolved_runtime()
    assert constructed == [], "resolved_runtime must not construct a store"
    second = _resolved_runtime()
    assert constructed == [], "resolved_runtime must not construct a store"

    assert first["store"] == "file"
    assert second["store"] == "file"


def test_health_reports_the_store_kind():
    """``/api/health`` carries the kind under its top-level ``runtime`` block."""
    payload = dashboard.health()

    runtime = payload.get("runtime")
    assert runtime is not None, "health() has no top-level 'runtime' block"
    assert runtime.get("store") == "file"
