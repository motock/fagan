"""Tests for ``pipeline.topology_identity.resolved_topology`` and its
``/api/health`` surface (story ENT-PREP-03, dashboard half).

Contract under test:

* ``resolved_topology()`` returns exactly four keys — ``plan_dir``,
  ``repo_root``, ``branch_namespace``, ``worktree_namespace``.
* ``plan_dir`` / ``repo_root`` are read from the live module bindings
  (``pipeline.paths.PLAN_DIR`` / ``pipeline.server.REPO_ROOT``) on every
  call, not snapshotted at import time.
* The namespace fields are ``None`` when the env var is unset or empty and
  the raw value otherwise — no validation, this is observational.
* ``/api/health`` gains a ``topology`` key equal to ``resolved_topology()``
  while keeping its existing ``ok`` / ``plan_dir`` / ``scheduler`` /
  ``config_mismatch`` keys.

The scheduler-health-file half of the feature is deferred (see
``docs/specs/TOPOLOGY_HEALTH.md``), so nothing here pins a fingerprint.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import pipeline.branch_names
import pipeline.paths
import pipeline.server
from app import dashboard as d
from pipeline.topology_identity import resolved_topology

WORKTREE_NAMESPACE_ENV = "PIPELINE_WORKTREE_NAMESPACE"


@pytest.fixture
def health_client():
    """A TestClient over the dashboard app.

    Deliberately local rather than the shared ``_dashboard_helpers.client``
    fixture: importing that fixture into a non-``test_dashboard_*`` module
    trips ruff's F811 (fixture name reused as a test parameter) and this
    file's name is fixed by the story.
    """
    return TestClient(d.app)


@pytest.fixture
def clean_namespace_env(monkeypatch):
    """Start from "no namespace configured" regardless of the host env."""
    monkeypatch.delenv(pipeline.branch_names.NAMESPACE_ENV, raising=False)
    monkeypatch.delenv(WORKTREE_NAMESPACE_ENV, raising=False)


# ---------------------------------------------------------------------------
# resolved_topology(): paths
# ---------------------------------------------------------------------------


def test_resolved_topology_reports_patched_plan_dir_and_repo_root(
    monkeypatch, tmp_path, clean_namespace_env
):
    plan_dir = tmp_path / "plans"
    repo_root = tmp_path / "repo"
    monkeypatch.setattr(pipeline.paths, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(pipeline.server, "REPO_ROOT", repo_root)

    topology = resolved_topology()

    assert topology["plan_dir"] == str(plan_dir)
    assert topology["repo_root"] == str(repo_root)


def test_resolved_topology_reads_paths_at_call_time(
    monkeypatch, tmp_path, clean_namespace_env
):
    """A module-load snapshot would keep returning the first value."""
    first_plan = tmp_path / "plans-a"
    first_repo = tmp_path / "repo-a"
    monkeypatch.setattr(pipeline.paths, "PLAN_DIR", first_plan)
    monkeypatch.setattr(pipeline.server, "REPO_ROOT", first_repo)
    assert resolved_topology()["plan_dir"] == str(first_plan)
    assert resolved_topology()["repo_root"] == str(first_repo)

    second_plan = tmp_path / "plans-b"
    second_repo = tmp_path / "repo-b"
    monkeypatch.setattr(pipeline.paths, "PLAN_DIR", second_plan)
    monkeypatch.setattr(pipeline.server, "REPO_ROOT", second_repo)

    topology = resolved_topology()
    assert topology["plan_dir"] == str(second_plan)
    assert topology["repo_root"] == str(second_repo)


def test_resolved_topology_has_exactly_the_four_documented_keys(
    monkeypatch, tmp_path, clean_namespace_env
):
    monkeypatch.setattr(pipeline.paths, "PLAN_DIR", tmp_path / "plans")
    monkeypatch.setattr(pipeline.server, "REPO_ROOT", tmp_path / "repo")

    assert set(resolved_topology()) == {
        "plan_dir",
        "repo_root",
        "branch_namespace",
        "worktree_namespace",
    }


# ---------------------------------------------------------------------------
# resolved_topology(): namespaces (observational, unvalidated)
# ---------------------------------------------------------------------------


def test_namespaces_are_none_when_env_is_unset(
    monkeypatch, tmp_path, clean_namespace_env
):
    monkeypatch.setattr(pipeline.paths, "PLAN_DIR", tmp_path / "plans")
    monkeypatch.setattr(pipeline.server, "REPO_ROOT", tmp_path / "repo")

    topology = resolved_topology()

    assert topology["branch_namespace"] is None
    assert topology["worktree_namespace"] is None


def test_namespaces_are_none_when_env_is_empty_string(
    monkeypatch, tmp_path, clean_namespace_env
):
    monkeypatch.setattr(pipeline.paths, "PLAN_DIR", tmp_path / "plans")
    monkeypatch.setattr(pipeline.server, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setenv(pipeline.branch_names.NAMESPACE_ENV, "")
    monkeypatch.setenv(WORKTREE_NAMESPACE_ENV, "")

    topology = resolved_topology()

    assert topology["branch_namespace"] is None
    assert topology["worktree_namespace"] is None


def test_namespaces_report_the_raw_env_value(
    monkeypatch, tmp_path, clean_namespace_env
):
    monkeypatch.setattr(pipeline.paths, "PLAN_DIR", tmp_path / "plans")
    monkeypatch.setattr(pipeline.server, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setenv(pipeline.branch_names.NAMESPACE_ENV, "team-a")
    monkeypatch.setenv(WORKTREE_NAMESPACE_ENV, "wt-7")

    topology = resolved_topology()

    assert topology["branch_namespace"] == "team-a"
    assert topology["worktree_namespace"] == "wt-7"


def test_namespaces_are_not_validated(monkeypatch, tmp_path, clean_namespace_env):
    """Observational only: a value the branch-name validator would reject is
    still reported verbatim rather than raising or being normalised."""
    monkeypatch.setattr(pipeline.paths, "PLAN_DIR", tmp_path / "plans")
    monkeypatch.setattr(pipeline.server, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setenv(pipeline.branch_names.NAMESPACE_ENV, "Not A Namespace!")
    monkeypatch.setenv(WORKTREE_NAMESPACE_ENV, "  spaced  ")

    topology = resolved_topology()

    assert topology["branch_namespace"] == "Not A Namespace!"
    assert topology["worktree_namespace"] == "  spaced  "


# ---------------------------------------------------------------------------
# /api/health surface
# ---------------------------------------------------------------------------


def test_health_includes_topology_and_keeps_existing_keys(
    health_client, monkeypatch, tmp_path, clean_namespace_env
):
    plan_dir = tmp_path / "plans"
    repo_root = tmp_path / "repo"
    monkeypatch.setattr(pipeline.paths, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(pipeline.server, "REPO_ROOT", repo_root)
    monkeypatch.setenv(pipeline.branch_names.NAMESPACE_ENV, "team-a")
    monkeypatch.setenv(WORKTREE_NAMESPACE_ENV, "wt-7")

    res = health_client.get("/api/health")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["topology"] == resolved_topology()
    assert body["topology"] == {
        "plan_dir": str(plan_dir),
        "repo_root": str(repo_root),
        "branch_namespace": "team-a",
        "worktree_namespace": "wt-7",
    }
    # Existing keys keep their meanings (membership only: the fingerprint
    # contents depend on the host's plan dir).
    assert body["ok"] is True
    assert "plan_dir" in body
    assert "scheduler" in body
    assert "config_mismatch" in body


def test_health_topology_namespaces_are_none_when_env_is_empty(
    health_client, monkeypatch, tmp_path, clean_namespace_env
):
    monkeypatch.setattr(pipeline.paths, "PLAN_DIR", tmp_path / "plans")
    monkeypatch.setattr(pipeline.server, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setenv(pipeline.branch_names.NAMESPACE_ENV, "")
    monkeypatch.setenv(WORKTREE_NAMESPACE_ENV, "")

    body = health_client.get("/api/health").json()

    assert body["topology"]["branch_namespace"] is None
    assert body["topology"]["worktree_namespace"] is None
