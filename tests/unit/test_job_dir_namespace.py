"""Detached job state is keyed by namespace as well as story key.

Story keys are unique per namespace only, so two teams that use the same key
must never share a merge re-verify, merge local-checks or review job
directory: a review verdict read from another team's directory would be taken
as this story's verdict.  Only ``subprocess.Popen`` is stubbed; the job
directories are real directories under ``tmp_path``.
"""

from __future__ import annotations

import os
import stat

import pytest

import pipeline.merge_reverify as mr
import pipeline.review_job as rj
from pipeline.actor import bind_namespace

ENV = "PIPELINE_WORKTREE_NAMESPACE"
TEAM = "acme/web"
OTHER = "beta/ops"
KEY = "calc-02"
PLAN = "P"


class _Proc:
    pid = 4242


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


@pytest.fixture
def popen(monkeypatch):
    calls: list = []

    def _popen(argv, **kwargs):
        calls.append(argv)
        return _Proc()

    monkeypatch.setattr(mr.subprocess, "Popen", _popen)
    monkeypatch.setattr(rj.subprocess, "Popen", _popen)
    return calls


@pytest.fixture
def worktree(tmp_path):
    path = tmp_path / "wt"
    path.mkdir()
    return str(path)


def _merge(root, worktree, namespace=None, key=KEY, kind="merge_reverify"):
    if namespace is None:
        return mr.start_merge_reverify(PLAN, key, worktree, root, kind=kind)
    with bind_namespace(namespace):
        return mr.start_merge_reverify(PLAN, key, worktree, root, kind=kind)


def _review(root, namespace=None, key=KEY):
    if namespace is None:
        return rj.start_review_job(PLAN, key, root)
    with bind_namespace(namespace):
        return rj.start_review_job(PLAN, key, root)


# ---------- default namespace keeps today's layout ----------


def test_default_namespace_merge_reverify_dir_is_unchanged(tmp_path, worktree, popen):
    handle = _merge(tmp_path, worktree)

    assert os.path.dirname(handle["result_path"]) == str(tmp_path / "merge_reverify" / KEY)


def test_default_namespace_review_job_dir_is_unchanged(tmp_path, popen):
    handle = _review(tmp_path)

    assert os.path.dirname(handle["result_path"]) == str(tmp_path / "review_job" / KEY)


# ---------- team namespace is nested under _ns ----------


def test_team_merge_reverify_dir_is_namespaced(tmp_path, worktree, popen):
    handle = _merge(tmp_path, worktree, TEAM)

    expected = tmp_path / "merge_reverify" / "_ns" / "acme@web" / KEY
    assert os.path.dirname(handle["result_path"]) == str(expected)


def test_team_review_job_dir_is_namespaced(tmp_path, popen):
    handle = _review(tmp_path, TEAM)

    expected = tmp_path / "review_job" / "_ns" / "acme@web" / KEY
    assert os.path.dirname(handle["result_path"]) == str(expected)


def test_team_merge_local_checks_dir_is_namespaced(tmp_path, worktree, popen):
    handle = _merge(tmp_path, worktree, TEAM, kind="merge_local_checks")

    expected = tmp_path / "merge_local_checks" / "_ns" / "acme@web" / KEY
    assert os.path.dirname(handle["result_path"]) == str(expected)


# ---------- isolation between namespaces ----------


def test_same_key_in_two_namespaces_gets_two_directories(tmp_path, popen):
    first = _review(tmp_path, TEAM)
    second = _review(tmp_path, OTHER)

    assert first["result_path"] != second["result_path"]


def test_result_written_for_one_namespace_is_not_visible_to_the_other(tmp_path, popen):
    first = _review(tmp_path, TEAM)
    second = _review(tmp_path, OTHER)
    with open(first["result_path"], "w", encoding="utf-8") as handle:
        handle.write('{"state": "done"}')

    assert os.path.exists(first["result_path"])
    assert not os.path.exists(second["result_path"])


# ---------- reserved and traversal keys ----------


def test_default_namespace_refuses_reserved_review_key(tmp_path, popen):
    with pytest.raises(ValueError, match="^invalid review job directory$"):
        _review(tmp_path, key="_ns")
    assert popen == []


def test_merge_reverify_default_namespace_refuses_reserved_key(tmp_path, worktree, popen):
    with pytest.raises(ValueError, match="^invalid story_key$"):
        _merge(tmp_path, worktree, key="_ns")
    assert popen == []


@pytest.mark.parametrize("key", [".", ".."])
def test_merge_reverify_refuses_dot_key_in_team_namespace(tmp_path, worktree, popen, key):
    with pytest.raises(ValueError) as excinfo:
        _merge(tmp_path, worktree, TEAM, key=key)

    assert str(excinfo.value) == "invalid story_key"
    assert popen == []


@pytest.mark.parametrize("key", [".", ".."])
def test_review_job_refuses_dot_key_in_team_namespace(tmp_path, popen, key):
    with pytest.raises(ValueError) as excinfo:
        _review(tmp_path, TEAM, key=key)

    assert str(excinfo.value) == "invalid story_key"
    assert str(tmp_path) not in str(excinfo.value)
    assert popen == []


def test_refusal_message_never_contains_the_state_root(tmp_path, worktree, popen):
    with pytest.raises(ValueError) as excinfo:
        _merge(tmp_path, worktree, TEAM, key="..")

    assert str(tmp_path) not in str(excinfo.value)


# ---------- permissions ----------


def test_team_job_leaf_directory_is_private(tmp_path, worktree, popen):
    handle = _merge(tmp_path, worktree, TEAM)

    leaf = os.path.dirname(handle["result_path"])
    assert stat.S_IMODE(os.stat(leaf).st_mode) == 0o700


def test_team_review_job_leaf_directory_is_private(tmp_path, popen):
    handle = _review(tmp_path, TEAM)

    leaf = os.path.dirname(handle["result_path"])
    assert stat.S_IMODE(os.stat(leaf).st_mode) == 0o700


def test_team_intermediate_namespace_levels_are_private(tmp_path, popen):
    _review(tmp_path, TEAM)

    for level in (tmp_path / "review_job" / "_ns", tmp_path / "review_job" / "_ns" / "acme@web"):
        assert stat.S_IMODE(os.stat(level).st_mode) == 0o700


# ---------- worktree env var does not move job state ----------


def test_worktree_namespace_env_does_not_change_default_job_dir(tmp_path, popen, monkeypatch):
    monkeypatch.setenv(ENV, "x")

    handle = _review(tmp_path)

    assert os.path.dirname(handle["result_path"]) == str(tmp_path / "review_job" / KEY)


def test_worktree_namespace_env_does_not_change_team_job_dir(tmp_path, worktree, popen, monkeypatch):
    monkeypatch.setenv(ENV, "x")

    handle = _merge(tmp_path, worktree, TEAM)

    expected = tmp_path / "merge_reverify" / "_ns" / "acme@web" / KEY
    assert os.path.dirname(handle["result_path"]) == str(expected)
