"""A plan's role_config is read through the record store.

``pipeline.persistence._plan_role_config`` used to load
``PLAN_DIR / "<plan>.manifest.json"`` itself, so under
``PIPELINE_STORE=sqlite`` a role_config set in the database (a
``set_plan_role_config`` call served by another host, or an ingest that wrote
the store) was invisible to every dispatch/review/merge role resolution on
this host. It now reads
``_store.records().get_manifest("default", plan_name)`` -- mirroring
``pipeline.checkpoint._plan_manifest`` and
``pipeline.cross_plan.neighbour_manifests`` -- and keeps the legacy
plan-directory read as the fallback for a store with no record view (an
in-process stand-in) and for a plan the record view does not know about, so
existing deployments keep working unchanged.

The read is a pure read: it never mutates the manifest, never writes the plan
directory, and never raises (a role_config read is not a gate), so an
unreadable store or file still degrades to "no override".
"""

from __future__ import annotations

import json

import pytest

from pipeline import persistence

ROLE_CONFIG = {"review": {"provider": "ollama", "model": "glm"}, "dispatch": {}}


class _FakeStore:
    """A store whose only useful surface is the single-manifest record read."""

    def __init__(self, manifests, raises: bool = False):
        self._manifests = manifests
        self._raises = raises
        self.requested = []

    def records(self):
        return self

    def get_manifest(self, namespace, plan_name):
        self.requested.append((namespace, plan_name))
        if self._raises:
            raise RuntimeError("store is down")
        return self._manifests.get(plan_name)


class _LegacyStore:
    """A store with no record view at all (an in-process stand-in)."""


@pytest.fixture
def plan_dir(monkeypatch, tmp_path):
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    return directory


def _write_manifest(plan_dir, plan_name, manifest):
    path = plan_dir / f"{plan_name}.manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def test_plan_role_config_reads_the_store_manifest_with_no_plan_file(
    plan_dir, monkeypatch
):
    """The store alone satisfies the read, and only namespace "default"."""
    from pipeline import server as p

    store = _FakeStore({"alpha": {"key": "alpha", "role_config": ROLE_CONFIG}})
    monkeypatch.setattr(p, "_store", store, raising=False)

    assert persistence._plan_role_config("alpha") == ROLE_CONFIG
    assert store.requested == [("default", "alpha")]
    assert not list(plan_dir.iterdir())


def test_plan_role_config_returns_empty_when_store_manifest_has_no_role_config(
    plan_dir, monkeypatch
):
    """A store-known manifest is the source of truth: no file fallback."""
    from pipeline import server as p

    monkeypatch.setattr(p, "_store", _FakeStore({"alpha": {"key": "alpha"}}), raising=False)
    _write_manifest(plan_dir, "alpha", {"key": "alpha", "role_config": ROLE_CONFIG})

    assert persistence._plan_role_config("alpha") == {}


def test_plan_role_config_falls_back_to_the_plan_directory_for_a_legacy_store(
    plan_dir, monkeypatch
):
    """A store with no record view keeps the legacy plan-directory read."""
    from pipeline import server as p

    monkeypatch.setattr(p, "_store", _LegacyStore(), raising=False)
    _write_manifest(plan_dir, "alpha", {"key": "alpha", "role_config": ROLE_CONFIG})

    assert persistence._plan_role_config("alpha") == ROLE_CONFIG


def test_plan_role_config_falls_back_when_the_store_does_not_know_the_plan(
    plan_dir, monkeypatch
):
    """An unknown plan falls back to the file instead of becoming {}."""
    from pipeline import server as p

    store = _FakeStore({})
    monkeypatch.setattr(p, "_store", store, raising=False)
    _write_manifest(plan_dir, "alpha", {"key": "alpha", "role_config": ROLE_CONFIG})

    assert persistence._plan_role_config("alpha") == ROLE_CONFIG
    assert store.requested == [("default", "alpha")]


def test_plan_role_config_is_empty_when_neither_source_has_the_plan(
    plan_dir, monkeypatch
):
    from pipeline import server as p

    monkeypatch.setattr(p, "_store", _FakeStore({}), raising=False)

    assert persistence._plan_role_config("missing") == {}


def test_plan_role_config_degrades_to_empty_when_the_store_raises(
    plan_dir, monkeypatch
):
    """A broken store is "no override", never an exception."""
    from pipeline import server as p

    store = _FakeStore({"alpha": {"role_config": ROLE_CONFIG}}, raises=True)
    monkeypatch.setattr(p, "_store", store, raising=False)

    assert persistence._plan_role_config("alpha") == {}
    assert store.requested == [("default", "alpha")]


def test_plan_role_config_is_empty_for_a_manifest_without_the_block(plan_dir):
    _write_manifest(plan_dir, "alpha", {"key": "alpha"})

    assert persistence._plan_role_config("alpha") == {}


def test_plan_role_config_is_empty_for_an_unreadable_manifest(plan_dir):
    (plan_dir / "alpha.manifest.json").write_text("{not json")

    assert persistence._plan_role_config("alpha") == {}


def test_plan_role_config_reads_are_repeatable_and_leave_the_file_untouched(
    plan_dir, monkeypatch
):
    """No caching, no mutation, no write: the second read sees the same thing."""
    from pipeline import server as p

    path = _write_manifest(plan_dir, "alpha", {"role_config": ROLE_CONFIG})
    before = path.read_bytes()

    assert persistence._plan_role_config("alpha") == ROLE_CONFIG
    assert path.read_bytes() == before

    store = _FakeStore({})
    monkeypatch.setattr(p, "_store", store, raising=False)
    assert persistence._plan_role_config("alpha") == ROLE_CONFIG
    assert persistence._plan_role_config("alpha") == ROLE_CONFIG
    assert store.requested == [("default", "alpha"), ("default", "alpha")]
    assert path.read_bytes() == before
