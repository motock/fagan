"""Tests for GET /api/plans/{plan}/stories/{story}/diff.

Driven through the real app (TestClient) so a missing registration, or one
placed after the catch-all StaticFiles mount, fails here.
"""
from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from tests.unit._dashboard_helpers import (  # noqa: F401
    _write_manifest,
    client,
    plan_dir,
)

PLAN = "diffplan"
STORY = "WTD-X"


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@pytest.fixture
def worktree_dir(tmp_path, monkeypatch):
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    monkeypatch.setattr(d, "WORKTREE_ROOT", wt_root)
    from pipeline import server as _srv

    monkeypatch.setattr(_srv, "WORKTREE_ROOT", wt_root)
    return wt_root


@pytest.fixture
def git_worktree(worktree_dir):
    """A real repo on master with one committed branch change and one edit."""
    wt = worktree_dir / STORY
    wt.mkdir()
    _git(wt, "init", "-b", "master")
    (wt / "a.txt").write_text("one\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "base")
    _git(wt, "checkout", "-b", "agent/x")
    (wt / "a.txt").write_text("one\ntwo\n")
    _git(wt, "commit", "-am", "change")
    return wt


def _manifest(plan_dir, story):
    _write_manifest(plan_dir, PLAN, {STORY: story})


def _url(path=None):
    base = f"/api/plans/{PLAN}/stories/{STORY}/diff"
    return base if path is None else f"{base}?path={path}"


def test_should_return_summary_for_real_worktree(client, plan_dir, git_worktree):
    _manifest(plan_dir, {"worktree": str(git_worktree)})

    res = client.get(_url())

    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert [f["path"] for f in body["files"]] == ["a.txt"]


def test_should_return_file_diff_for_listed_path(client, plan_dir, git_worktree):
    _manifest(plan_dir, {"worktree": str(git_worktree)})

    res = client.get(_url("a.txt"))

    assert res.status_code == 200
    assert "+two" in res.text


def test_should_reject_path_absent_from_summary(client, plan_dir, git_worktree):
    _manifest(plan_dir, {"worktree": str(git_worktree)})

    res = client.get(_url("nope.txt"))

    assert res.status_code == 400


def test_should_reject_traversal_path(client, plan_dir, git_worktree):
    _manifest(plan_dir, {"worktree": str(git_worktree)})

    res = client.get(_url("../../etc/passwd"))

    assert res.status_code == 400


def test_should_report_unavailable_when_story_has_no_worktree(client, plan_dir):
    _manifest(plan_dir, {})

    res = client.get(_url())

    assert res.status_code == 200
    assert res.json() == {"available": False, "reason": "no_worktree"}


def test_should_report_unavailable_when_worktree_deleted(client, plan_dir, worktree_dir):
    _manifest(plan_dir, {"worktree": str(worktree_dir / "gone")})

    res = client.get(_url())

    assert res.status_code == 200
    assert res.json()["available"] is False


def test_should_report_unavailable_for_non_git_worktree(client, plan_dir, worktree_dir):
    plain = worktree_dir / STORY
    plain.mkdir()
    _manifest(plan_dir, {"worktree": str(plain)})

    res = client.get(_url())

    assert res.status_code == 200
    assert res.json()["available"] is False


def test_should_return_404_for_unknown_plan(client, plan_dir):
    res = client.get("/api/plans/missing/stories/WTD-X/diff")

    assert res.status_code == 404


def test_should_return_404_for_unknown_story(client, plan_dir):
    _manifest(plan_dir, {})

    res = client.get(f"/api/plans/{PLAN}/stories/NOPE/diff")

    assert res.status_code == 404


def test_should_reject_request_without_api_key(plan_dir):
    _manifest(plan_dir, {})
    anon = TestClient(d.app)
    anon.headers.pop("X-Pipeline-Api-Key", None)

    res = anon.get(_url())

    assert res.status_code == 401


def test_should_set_no_store_on_success(client, plan_dir, git_worktree):
    _manifest(plan_dir, {"worktree": str(git_worktree)})

    assert client.get(_url()).headers["cache-control"] == "no-store"


def test_should_set_no_store_on_unavailable(client, plan_dir):
    _manifest(plan_dir, {})

    assert client.get(_url()).headers["cache-control"] == "no-store"


def test_should_set_no_store_on_400(client, plan_dir, git_worktree):
    _manifest(plan_dir, {"worktree": str(git_worktree)})

    assert client.get(_url("nope.txt")).headers["cache-control"] == "no-store"


def test_should_set_no_store_on_404(client, plan_dir):
    res = client.get("/api/plans/missing/stories/WTD-X/diff")

    assert res.headers["cache-control"] == "no-store"


def test_should_register_route_before_static_mount():
    paths = [getattr(r, "path", None) for r in d.app.routes]

    diff_idx = paths.index("/api/plans/{plan_name}/stories/{story_key}/diff")

    assert diff_idx < paths.index("/")
