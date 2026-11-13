"""The two rebuildable host caches resolve their roots through ``HostState``.

* ``pipeline.forge_github.ci_rollup_state_root``: an absolute
  ``PIPELINE_STATE_DIR`` wins; a relative one resolves against the host cache
  root, never the process CWD; with no override the host cache root itself.
  An explicit ``manifest_root`` argument is still honoured as the base.
* ``pipeline.dispatch_baseline._cache_dir``: ``baseline_cache`` under the host
  cache root, so the on-disk location is unchanged.
"""

from pathlib import Path

import pytest

import pipeline.server as server_mod
from pipeline import dispatch_baseline, forge_github

STATE_DIR_ENV = "PIPELINE_STATE_DIR"


@pytest.fixture
def plans_dir(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server_mod, "PLAN_DIR", plans)
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    return plans


def test_should_default_ci_rollup_root_to_the_plans_directory(plans_dir):
    assert forge_github.ci_rollup_state_root() == plans_dir


def test_should_let_an_absolute_state_dir_win_over_the_plans_directory(
    plans_dir, tmp_path, monkeypatch
):
    absolute = tmp_path / "elsewhere"
    monkeypatch.setenv(STATE_DIR_ENV, str(absolute))
    assert forge_github.ci_rollup_state_root() == absolute


def test_should_resolve_a_relative_state_dir_against_the_host_root_not_the_cwd(
    plans_dir, tmp_path, monkeypatch
):
    cwd = tmp_path / "scheduler-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv(STATE_DIR_ENV, "state")
    resolved = forge_github.ci_rollup_state_root()
    assert resolved == plans_dir / "state"
    assert not str(resolved).startswith(str(cwd))


def test_should_honour_an_explicit_manifest_root_as_the_base(plans_dir, tmp_path):
    manifests = tmp_path / "manifests"
    assert forge_github.ci_rollup_state_root(manifests) == manifests


def test_should_resolve_a_relative_state_dir_against_an_explicit_manifest_root(
    plans_dir, tmp_path, monkeypatch
):
    manifests = tmp_path / "manifests"
    monkeypatch.setenv(STATE_DIR_ENV, "state")
    assert forge_github.ci_rollup_state_root(manifests) == manifests / "state"


def test_should_keep_an_absolute_state_dir_over_an_explicit_manifest_root(
    plans_dir, tmp_path, monkeypatch
):
    absolute = tmp_path / "abs-state"
    monkeypatch.setenv(STATE_DIR_ENV, str(absolute))
    assert forge_github.ci_rollup_state_root(tmp_path / "manifests") == absolute


def test_should_place_the_baseline_cache_under_the_host_cache_root(plans_dir):
    assert dispatch_baseline._cache_dir() == plans_dir / dispatch_baseline._BASELINE_CACHE_DIRNAME


def test_should_follow_a_repatched_plans_directory_for_the_baseline_cache(
    plans_dir, tmp_path, monkeypatch
):
    other = tmp_path / "other-plans"
    monkeypatch.setattr(server_mod, "PLAN_DIR", other)
    assert dispatch_baseline._cache_dir() == other / dispatch_baseline._BASELINE_CACHE_DIRNAME


def test_should_keep_the_reference_row_saying_the_state_root_is_the_plans_base():
    reference = (Path(__file__).resolve().parents[2] / "REFERENCE.md").read_text(encoding="utf-8")
    row = next(line for line in reference.splitlines() if line.startswith("| `PIPELINE_STATE_DIR` |"))
    assert "the pipeline-owned plans/manifests root, i.e. `PLAN_DIR`'s base" in row
