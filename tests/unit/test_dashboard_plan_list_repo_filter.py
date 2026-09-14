"""Tests for the optional ``repo`` filter on ``GET /api/plans`` and the
``repo_root`` field on each plan summary.

Per ``.claude/rules/testing-config-gates.md``: this route receives the repo
param *explicitly* and does NOT resolve the active workspace itself (that
resolution lives in the frontend story). So these tests exercise the FILTER
against a stubbed store/fixture with synthetic repo paths under ``tmp_path``
- never against the live active-workspace value.

The manifests are written directly to the patched ``PLAN_DIR`` (the same
seam ``tests/unit/_dashboard_helpers.py`` uses), so the store reads back
exactly the synthetic ``repo_root`` values this file controls.
"""
import inspect
import json
import os

import pytest

from app import dashboard as d
from tests.unit._dashboard_helpers import (  # noqa: F401
    client,
    plan_dir,
)


def _write_manifest(plan_dir, name, stories, repo_root=None, paused=False):
    """Write a ``<name>.manifest.json`` with an optional top-level
    ``repo_root`` (the plan-level field the summary must surface)."""
    manifest = {"epics": {}, "stories": stories}
    if repo_root is not None:
        manifest["repo_root"] = repo_root
    if paused:
        manifest["paused"] = True
    (plan_dir / f"{name}.manifest.json").write_text(json.dumps(manifest))


def _archive(plan_dir, *names):
    """Persist the dashboard's archived-plan preference file."""
    (plan_dir / ".dashboard_ui_state.json").write_text(
        json.dumps({"archived": sorted(names)})
    )


def _story(status="todo"):
    return {"summary": "s", "status": status, "dependencies": []}


@pytest.fixture
def two_repos(plan_dir):
    """Two plans at distinct synthetic repo paths, plus the paths."""
    path_a = plan_dir / "repoA"
    path_b = plan_dir / "repoB"
    path_a.mkdir()
    path_b.mkdir()
    _write_manifest(plan_dir, "plan-a", {"S1": _story()}, repo_root=str(path_a))
    _write_manifest(plan_dir, "plan-b", {"S1": _story()}, repo_root=str(path_b))
    return {"a": path_a, "b": path_b}


def _names(res):
    return sorted(p["name"] for p in res.json()["plans"])


# --------------------------------------------------------------------------
# (a) absent / (b) "all" / (f) empty -> ALL plans, exactly as today
# --------------------------------------------------------------------------

def test_no_repo_param_returns_all_plans(client, two_repos):
    res = client.get("/api/plans")
    assert res.status_code == 200
    assert _names(res) == ["plan-a", "plan-b"]


def test_repo_all_returns_all_plans(client, two_repos):
    res = client.get("/api/plans", params={"repo": "all"})
    assert res.status_code == 200
    assert _names(res) == ["plan-a", "plan-b"]


def test_repo_empty_string_returns_all_plans(client, two_repos):
    res = client.get("/api/plans", params={"repo": ""})
    assert res.status_code == 200
    assert _names(res) == ["plan-a", "plan-b"]


def test_bare_plans_endpoint_unchanged_backward_compat(client, plan_dir):
    """A bare /api/plans caller sees every non-archived plan, no repo
    filtering applied."""
    _write_manifest(plan_dir, "one", {"S1": _story()}, repo_root="/tmp/x")
    _write_manifest(plan_dir, "two", {"S1": _story()}, repo_root="/tmp/y")
    res = client.get("/api/plans")
    assert res.status_code == 200
    assert _names(res) == ["one", "two"]


# --------------------------------------------------------------------------
# (c)/(d) repo=<pathA> -> only plans at pathA
# --------------------------------------------------------------------------

def test_repo_filters_to_matching_path(client, two_repos):
    res = client.get("/api/plans", params={"repo": str(two_repos["a"])})
    assert res.status_code == 200
    assert _names(res) == ["plan-a"]


def test_repo_excludes_plan_at_other_path(client, two_repos):
    res = client.get("/api/plans", params={"repo": str(two_repos["a"])})
    assert "plan-b" not in _names(res)


def test_repo_returns_every_plan_at_the_same_path(client, plan_dir):
    path_a = plan_dir / "repoA"
    path_b = plan_dir / "repoB"
    path_a.mkdir()
    path_b.mkdir()
    _write_manifest(plan_dir, "plan-a1", {"S1": _story()}, repo_root=str(path_a))
    _write_manifest(plan_dir, "plan-a2", {"S1": _story()}, repo_root=str(path_a))
    _write_manifest(plan_dir, "plan-b1", {"S1": _story()}, repo_root=str(path_b))
    res = client.get("/api/plans", params={"repo": str(path_a)})
    assert _names(res) == ["plan-a1", "plan-a2"]


def test_repo_is_exact_match_not_prefix(client, plan_dir):
    """A plan rooted at <pathA>/sub must NOT match repo=<pathA>."""
    path_a = plan_dir / "repoA"
    (path_a / "sub").mkdir(parents=True)
    _write_manifest(plan_dir, "plan-a", {"S1": _story()}, repo_root=str(path_a))
    _write_manifest(plan_dir, "plan-sub", {"S1": _story()}, repo_root=str(path_a / "sub"))
    res = client.get("/api/plans", params={"repo": str(path_a)})
    assert _names(res) == ["plan-a"]


def test_repo_filter_excludes_plan_without_repo_root(client, plan_dir):
    """A plan whose manifest has no repo_root can never match an explicit
    repo path."""
    path_a = plan_dir / "repoA"
    path_a.mkdir()
    _write_manifest(plan_dir, "plan-a", {"S1": _story()}, repo_root=str(path_a))
    _write_manifest(plan_dir, "plan-null", {"S1": _story()})  # no repo_root
    res = client.get("/api/plans", params={"repo": str(path_a)})
    assert _names(res) == ["plan-a"]


# --------------------------------------------------------------------------
# (e) repo=<nonexistent-path> -> 200 with empty plans (not 404/422)
# --------------------------------------------------------------------------

def test_repo_nonexistent_path_returns_200_empty_plans(client, two_repos):
    res = client.get("/api/plans", params={"repo": "/no/such/repo/anywhere"})
    assert res.status_code == 200
    assert res.json() == {"plans": []}


def test_repo_nonexistent_path_is_not_404_or_422(client, two_repos):
    res = client.get("/api/plans", params={"repo": "/no/such/repo/anywhere"})
    assert res.status_code not in (404, 422)


# --------------------------------------------------------------------------
# (g) include_archived + repo compose (AND)
# --------------------------------------------------------------------------

def test_include_archived_and_repo_compose(client, plan_dir):
    path_a = plan_dir / "repoA"
    path_b = plan_dir / "repoB"
    path_a.mkdir()
    path_b.mkdir()
    _write_manifest(plan_dir, "plan-a-open", {"S1": _story()}, repo_root=str(path_a))
    _write_manifest(plan_dir, "plan-a-archived", {"S1": _story()}, repo_root=str(path_a))
    _write_manifest(plan_dir, "plan-b-open", {"S1": _story()}, repo_root=str(path_b))
    _archive(plan_dir, "plan-a-archived")

    res = client.get(
        "/api/plans",
        params={"include_archived": "true", "repo": str(path_a)},
    )
    assert res.status_code == 200
    assert _names(res) == ["plan-a-archived", "plan-a-open"]


def test_repo_without_include_archived_excludes_archived_at_that_path(client, plan_dir):
    path_a = plan_dir / "repoA"
    path_a.mkdir()
    _write_manifest(plan_dir, "plan-a-open", {"S1": _story()}, repo_root=str(path_a))
    _write_manifest(plan_dir, "plan-a-archived", {"S1": _story()}, repo_root=str(path_a))
    _archive(plan_dir, "plan-a-archived")

    res = client.get("/api/plans", params={"repo": str(path_a)})
    assert _names(res) == ["plan-a-open"]


def test_include_archived_true_without_repo_still_returns_all(client, plan_dir):
    """Backward compat: ?include_archived=true (no repo) is unchanged."""
    _write_manifest(plan_dir, "open", {"S1": _story()}, repo_root="/tmp/a")
    _write_manifest(plan_dir, "archived", {"S1": _story()}, repo_root="/tmp/b")
    _archive(plan_dir, "archived")

    res = client.get("/api/plans", params={"include_archived": "true"})
    assert res.status_code == 200
    assert _names(res) == ["archived", "open"]


# --------------------------------------------------------------------------
# Normalization: trailing slash and ~-prefix variants still match
# --------------------------------------------------------------------------

def test_repo_trailing_slash_variant_matches(client, two_repos):
    res = client.get("/api/plans", params={"repo": str(two_repos["a"]) + "/"})
    assert res.status_code == 200
    assert _names(res) == ["plan-a"]


def test_repo_tilde_prefix_variant_matches(client, plan_dir, monkeypatch):
    monkeypatch.setenv("HOME", str(plan_dir))
    path_a = plan_dir / "repoA"
    path_b = plan_dir / "repoB"
    path_a.mkdir()
    path_b.mkdir()
    _write_manifest(plan_dir, "plan-a", {"S1": _story()}, repo_root=str(path_a))
    _write_manifest(plan_dir, "plan-b", {"S1": _story()}, repo_root=str(path_b))

    res = client.get("/api/plans", params={"repo": "~/repoA"})
    assert res.status_code == 200
    assert _names(res) == ["plan-a"]


# --------------------------------------------------------------------------
# (h) repo_root on every summary; missing -> null, never an exception
# --------------------------------------------------------------------------

def test_summary_includes_repo_root_for_every_plan(client, two_repos):
    res = client.get("/api/plans")
    plans = {p["name"]: p for p in res.json()["plans"]}
    assert "repo_root" in plans["plan-a"]
    assert "repo_root" in plans["plan-b"]
    assert os.path.realpath(plans["plan-a"]["repo_root"]) == os.path.realpath(
        str(two_repos["a"])
    )
    assert os.path.realpath(plans["plan-b"]["repo_root"]) == os.path.realpath(
        str(two_repos["b"])
    )


def test_summary_repo_root_is_null_when_manifest_lacks_it(client, plan_dir):
    _write_manifest(plan_dir, "no-repo", {"S1": _story()})  # no repo_root key
    res = client.get("/api/plans")
    assert res.status_code == 200
    plans = res.json()["plans"]
    assert len(plans) == 1
    assert plans[0]["repo_root"] is None


def test_summary_repo_root_null_does_not_raise(client, plan_dir):
    """A manifest missing repo_root must not 500 the whole list."""
    _write_manifest(plan_dir, "no-repo", {"S1": _story()})
    _write_manifest(plan_dir, "has-repo", {"S1": _story()}, repo_root="/tmp/r")
    res = client.get("/api/plans")
    assert res.status_code == 200
    assert _names(res) == ["has-repo", "no-repo"]


# --------------------------------------------------------------------------
# API contract: optional repo param + documented normalization
# --------------------------------------------------------------------------

def test_list_plans_accepts_optional_repo_param():
    sig = inspect.signature(d.list_plans)
    assert "repo" in sig.parameters, "list_plans must accept a repo query param"
    assert sig.parameters["repo"].default in (None, ""), (
        "repo must default to absent/empty so existing callers are unchanged"
    )


def test_list_plans_docstring_documents_repo_normalization():
    doc = inspect.getdoc(d.list_plans) or ""
    assert doc, "list_plans must document the repo filter"
    lowered = doc.lower()
    assert "repo" in lowered
    assert any(k in lowered for k in ("realpath", "expanduser", "normaliz")), (
        "docstring must document the chosen repo normalization"
    )
