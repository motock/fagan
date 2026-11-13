"""``HostState`` exposes the roots of the two rebuildable host caches.

Stage C moves host-local state behind ``HostState``. The dispatch-baseline
cache and the CI-rollup cache live under the host cache root, and the watchdog
agent-log tail lives under the story-log root. Both roots resolve to the
plans directory on disk today, so the paths are unchanged:

* ``cache_root()`` and ``story_log_root()`` are methods of ``HostState``,
* both default to the plans directory, read through ``pipeline.server`` on
  every call (a test that patches ``PLAN_DIR`` is honoured).
"""

from pathlib import Path

import pytest

import pipeline.server as server_mod
from pipeline import host_state


@pytest.fixture
def plans_dir(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server_mod, "PLAN_DIR", plans)
    return plans


def test_should_expose_cache_root_on_the_host_state_seam():
    assert hasattr(host_state.FileHostState, "cache_root")


def test_should_expose_story_log_root_on_the_host_state_seam():
    assert hasattr(host_state.FileHostState, "story_log_root")


def test_should_resolve_cache_root_to_the_plans_directory(plans_dir):
    assert host_state.build_host_state().cache_root() == plans_dir


def test_should_resolve_story_log_root_to_the_plans_directory(plans_dir):
    assert host_state.build_host_state().story_log_root() == plans_dir


def test_should_follow_a_repatched_plans_directory_on_every_call(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    state = host_state.build_host_state()
    monkeypatch.setattr(server_mod, "PLAN_DIR", first)
    assert state.cache_root() == first
    monkeypatch.setattr(server_mod, "PLAN_DIR", second)
    assert state.cache_root() == second
    assert state.story_log_root() == second


def test_should_not_create_anything_when_resolving_the_roots(tmp_path, monkeypatch):
    # The bare seam, not build_host_state(): that factory creates the root
    # (ensure_root) on purpose, so resolving a root through it would create it.
    missing = tmp_path / "not-yet"
    monkeypatch.setattr(server_mod, "PLAN_DIR", missing)
    state = host_state.FileHostState()
    assert state.cache_root() == missing
    assert state.story_log_root() == missing
    assert not missing.exists()


def test_should_return_paths_not_strings(plans_dir):
    state = host_state.build_host_state()
    assert isinstance(state.cache_root(), Path)
    assert isinstance(state.story_log_root(), Path)
